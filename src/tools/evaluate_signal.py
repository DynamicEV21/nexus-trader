"""
Evaluate Signal Tool — Run a StratForge strategy on current market data
=========================================================================

This module provides the ``evaluate_signal`` ``@agent_tool`` that:

1. Queries ``backtest_results_v2`` in the StratForge DuckDB for a strategy's
   ``source_code`` and ``params_json``
2. Loads recent OHLCV data from the local 4h parquet cache
3. Executes the strategy code in a **restricted sandbox**
4. Interprets the resulting entry/exit signals
5. Returns a structured signal dict with a 5-minute cache

Strategy Interface
------------------
All StratForge strategies must define a function with this signature::

    def strategy(df, params=None):
        ...
        return entries, exits

Where:
- ``df`` : pd.DataFrame with columns ``open, high, low, close, volume`` and a
  DatetimeIndex
- ``params`` : optional dict of strategy parameters
- ``entries`` : pd.Series of 0/1 (1 = enter long at this bar)
- ``exits`` : pd.Series of 0/1 (1 = exit long at this bar)

Some strategies name the function after the strategy (e.g.,
``def trima_swing_adx_obv_v19(df, params=None)``) instead of ``strategy``.
The runner auto-detects the function name.

Security
--------
Strategy source code is executed via ``exec()`` in a restricted namespace:

- **Allowed modules**: ``numpy`` (as ``np``), ``pandas`` (as ``pd``), ``talib``
- **Blocked modules**: ``os``, ``sys``, ``subprocess``, ``socket``, ``open``
  (builtin), ``importlib``, ``__import__`` is overridden
- **Timeout**: 10-second wall clock via ``signal.alarm``
- **All wrapped in try/except**: errors return a ``hold`` signal with
  ``confidence=0`` and the error message

Known limitation: This is NOT a security-grade sandbox. A sophisticated
attacker who controls the strategy code could potentially escape it.
For untrusted code, use subprocess isolation or a container. For
StratForge-generated code (which is machine-generated and reviewed),
this level of isolation is adequate.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Live data refresh
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    from src.data.live_data import refresh_live_data, is_data_stale
    _LIVE_DATA_AVAILABLE = True
except ImportError as _imp_err:
    logger.warning("live_data module not available: %s", _imp_err)
    _LIVE_DATA_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.environ.get(
    "NEXUS_PROJECT_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
))
_DATA_CACHE = _PROJECT_ROOT / "data" / "4h_cache"
_STRATFORGE_DB = Path(os.environ.get(
    "STRATFORGE_DB_PATH",
    os.path.expanduser("~/development/agentic-quant-os/data/quant.duckdb"),
))

# ---------------------------------------------------------------------------
# Cache (5-minute TTL)
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 300  # 5 minutes
_signal_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_key(strategy_id: str, symbol: str, timestep: str, bars: int) -> str:
    return f"{strategy_id}:{symbol}:{timestep}:{bars}"


# ---------------------------------------------------------------------------
# Restricted execution sandbox
# ---------------------------------------------------------------------------

class _TimeoutError(Exception):
    """Raised when strategy execution exceeds the time limit."""


def _timeout_handler(signum: int, frame: Any) -> None:
    raise _TimeoutError("Strategy execution exceeded 10-second timeout")


# Modules that are explicitly blocked
_BLOCKED_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "http", "urllib",
    "importlib", "builtins", "ctypes", "multiprocessing",
    "threading", "asyncio", "pickle", "shutil", "tempfile",
    "pathlib", "glob", "fcntl", "resource",
})

# Modules that are allowed (provided in the namespace)
_ALLOWED_MODULES = frozenset({"numpy", "pandas", "talib", "math", "statistics"})


def _safe_import(name: str, *args: Any, **kwargs: Any) -> Any:
    """Restricted __import__ that only allows safe modules."""
    top_level = name.split(".")[0]
    if top_level in _BLOCKED_MODULES:
        raise ImportError(f"Module '{name}' is blocked in strategy sandbox")
    if top_level not in _ALLOWED_MODULES:
        raise ImportError(f"Module '{name}' is not allowed in strategy sandbox. "
                          f"Allowed: {sorted(_ALLOWED_MODULES)}")
    return _original_import(name, *args, **kwargs)


_original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__


def _execute_strategy(source_code: str, df: Any, params: dict | None = None) -> tuple[Any, Any]:
    """Execute strategy source code in a restricted namespace.

    Returns (entries, exits) pd.Series.

    Raises on any error (timeout, import violation, runtime error, etc.)
    """
    # Set up the restricted namespace
    import numpy as np
    import pandas as pd
    import talib
    import math
    import statistics

    safe_builtins = {
        # Basic Python builtins that don't touch I/O
        "abs": abs, "min": min, "max": max, "sum": sum, "len": len,
        "range": range, "enumerate": enumerate, "zip": zip,
        "int": int, "float": float, "str": str, "bool": bool,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "sorted": sorted, "reversed": reversed, "any": any, "all": all,
        "round": round, "isinstance": isinstance, "type": type,
        "print": print,  # Allow print for debugging (goes to logs)
        "None": None, "True": True, "False": False,
        "ValueError": ValueError, "TypeError": TypeError,
        "KeyError": KeyError, "IndexError": IndexError,
        "ZeroDivisionError": ZeroDivisionError,
        "Exception": Exception, "RuntimeError": RuntimeError,
        "getattr": getattr, "hasattr": hasattr, "setattr": setattr,
        "property": property, "staticmethod": staticmethod,
        "classmethod": classmethod,
        "__import__": _safe_import,
        "__name__": "__strategy_sandbox__",
        "__build_class__": __build_class__,
    }

    restricted_globals = {
        "__builtins__": safe_builtins,
        "np": np,
        "pd": pd,
        "talib": talib,
        "math": math,
        "statistics": statistics,
    }

    # Set up timeout
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(10)  # 10 second timeout

    try:
        # Execute the source code to define the function(s)
        exec(source_code, restricted_globals)  # noqa: S102

        # Find the strategy function — try common names
        strategy_fn = None
        for name in ("strategy", "generate_signal", "run"):
            if name in restricted_globals and callable(restricted_globals[name]):
                strategy_fn = restricted_globals[name]
                break

        if strategy_fn is None:
            # Look for any function that takes (df, params) or (df)
            for name, obj in restricted_globals.items():
                if name.startswith("_"):
                    continue
                if callable(obj) and not isinstance(obj, type):
                    # Check if it looks like a strategy function
                    if name != "np" and name != "pd" and name != "talib":
                        strategy_fn = obj
                        logger.debug("Auto-detected strategy function: %s", name)
                        break

        if strategy_fn is None:
            raise ValueError("No strategy function found in source code. "
                             "Expected: def strategy(df, params=None) or similar")

        # Call the strategy
        result = strategy_fn(df, params)
        return result if isinstance(result, tuple) else (result, None)

    finally:
        signal.alarm(0)  # Cancel timeout
        signal.signal(signal.SIGALRM, old_handler)


# ---------------------------------------------------------------------------
# Signal interpretation
# ---------------------------------------------------------------------------

def _interpret_signal(entries: Any, exits: Any, lookback: int = 10) -> dict[str, Any]:
    """Interpret the last N bars of entry/exit signals.

    Returns:
        {
            "signal": "buy" | "sell" | "hold",
            "confidence": float (0.0-1.0),
            "bars_since_entry": int | None,
            "bars_since_exit": int | None,
        }
    """
    import pandas as pd
    import numpy as np

    if entries is None:
        return {"signal": "hold", "confidence": 0.0,
                "bars_since_entry": None, "bars_since_exit": None}

    # Get last `lookback` bars
    if isinstance(entries, pd.Series):
        recent_entries = entries.iloc[-lookback:].values
        recent_exits = exits.iloc[-lookback:].values if exits is not None else np.zeros(lookback)
    else:
        recent_entries = np.array(entries[-lookback:])
        recent_exits = np.array(exits[-lookback:]) if exits is not None else np.zeros(lookback)

    # Count entries and exits in the lookback window
    n_entries = int(np.sum(recent_entries > 0))
    n_exits = int(np.sum(recent_exits > 0))

    # Find bars since last entry / exit
    bars_since_entry = None
    bars_since_exit = None
    for i in range(len(recent_entries) - 1, -1, -1):
        if recent_entries[i] > 0 and bars_since_entry is None:
            bars_since_entry = len(recent_entries) - 1 - i
        if recent_exits[i] > 0 and bars_since_exit is None:
            bars_since_exit = len(recent_entries) - 1 - i

    # Determine signal
    last_entry = recent_entries[-1] > 0 if len(recent_entries) > 0 else False
    last_exit = recent_exits[-1] > 0 if len(recent_exits) > 0 else False

    if last_entry:
        signal = "buy"
        confidence = min(1.0, 0.6 + 0.1 * n_entries)
    elif last_exit:
        signal = "sell"
        confidence = min(1.0, 0.6 + 0.1 * n_exits)
    elif n_entries > 0 and bars_since_entry is not None and bars_since_entry <= 3:
        # Recent entry within last 3 bars — still holding
        signal = "buy"
        confidence = max(0.3, 0.8 - 0.1 * bars_since_entry)
    elif n_exits > 0 and bars_since_exit is not None and bars_since_exit <= 2:
        signal = "hold"
        confidence = 0.3
    else:
        signal = "hold"
        confidence = 0.2

    return {
        "signal": signal,
        "confidence": round(confidence, 3),
        "bars_since_entry": bars_since_entry,
        "bars_since_exit": bars_since_exit,
        "entries_in_lookback": n_entries,
        "exits_in_lookback": n_exits,
    }


# ---------------------------------------------------------------------------
# Strategy loading from StratForge
# ---------------------------------------------------------------------------

def _load_strategy(strategy_id: str) -> dict[str, Any]:
    """Load strategy source_code + params from backtest_results_v2.

    Returns dict with keys: source_code, params_json, composite_score,
    ticker, archetype, total_return, max_drawdown, num_trades.
    """
    import duckdb

    con = duckdb.connect(str(_STRATFORGE_DB), read_only=True)
    try:
        row = con.execute(
            """
            SELECT source_code, params_json, composite_score, ticker,
                   archetype, total_return, max_drawdown, num_trades,
                   sharpe, sortino
            FROM backtest_results_v2
            WHERE strategy_name = ?
              AND is_best_version = true
            ORDER BY composite_score DESC
            LIMIT 1
            """,
            [strategy_id],
        ).fetchone()

        if row is None:
            return {"error": f"Strategy '{strategy_id}' not found in backtest_results_v2"}

        return {
            "source_code": row[0] or "",
            "params_json": row[1] or "{}",
            "composite_score": row[2],
            "ticker": row[3],
            "archetype": row[4],
            "total_return": row[5],
            "max_drawdown": row[6],
            "num_trades": row[7],
            "sharpe": row[8],
            "sortino": row[9],
        }
    finally:
        con.close()


def _load_market_data(symbol: str, timestep: str = "4h", bars: int = 500) -> Any:
    """Load recent OHLCV data from the local parquet cache.

    Args:
        symbol: "BTC", "ETH", "SOL", etc.
        timestep: "4h" (only 4h is currently cached)
        bars: Number of most recent bars to return

    Returns pd.DataFrame with OHLCV columns.
    """
    import pandas as pd

    # Map common symbols
    symbol_map = {
        "BTCUSDT": "BTC", "BTC/USDT": "BTC", "BTC-USD": "BTC", "BTC-USD": "BTC",
        "ETHUSDT": "ETH", "ETH/USDT": "ETH",
        "SOLUSDT": "SOL", "SOL/USDT": "SOL",
    }
    canonical = symbol_map.get(symbol.upper(), symbol.upper())

    parquet_path = _DATA_CACHE / f"{canonical}_{timestep}.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"No cached data for {canonical} {timestep} at {parquet_path}. "
            f"Available: {list(_DATA_CACHE.glob('*.parquet'))}"
        )

    df = pd.read_parquet(parquet_path)
    # Take last `bars` rows
    df = df.iloc[-bars:].copy()
    return df


def _extract_indicators(df: Any, strategy_code: str) -> dict[str, float]:
    """Extract a few key indicators from the last bar for context."""
    import numpy as np

    try:
        import talib
        close = df["close"].values.astype(np.float64)
        high = df["high"].values.astype(np.float64)
        low = df["low"].values.astype(np.float64)

        rsi = talib.RSI(close, timeperiod=14)
        sma50 = talib.SMA(close, timeperiod=50)
        sma200 = talib.SMA(close, timeperiod=200)
        atr = talib.ATR(high, low, close, timeperiod=14)
        adx = talib.ADX(high, low, close, timeperiod=14)

        last_price = float(close[-1])
        result = {
            "price": last_price,
            "rsi_14": round(float(rsi[-1]), 2) if not np.isnan(rsi[-1]) else None,
            "sma_50": round(float(sma50[-1]), 2) if not np.isnan(sma50[-1]) else None,
            "sma_200": round(float(sma200[-1]), 2) if not np.isnan(sma200[-1]) else None,
            "atr_14": round(float(atr[-1]), 2) if not np.isnan(atr[-1]) else None,
            "adx_14": round(float(adx[-1]), 2) if not np.isnan(adx[-1]) else None,
            "above_sma50": bool(last_price > float(sma50[-1])) if not np.isnan(sma50[-1]) else None,
            "above_sma200": bool(last_price > float(sma200[-1])) if not np.isnan(sma200[-1]) else None,
        }
        return result
    except Exception as exc:
        logger.warning("Failed to extract indicators: %s", exc)
        return {"price": float(df["close"].iloc[-1]), "error": str(exc)}


# ---------------------------------------------------------------------------
# Main evaluate_signal function
# ---------------------------------------------------------------------------

def evaluate_signal(
    strategy_id: str,
    symbol: str = "BTC",
    timestep: str = "4h",
) -> dict[str, Any]:
    """Evaluate a StratForge strategy on current market data.

    Loads the strategy's source code from the StratForge lakehouse,
    runs it against the most recent OHLCV bars, and returns the
    current signal (buy/sell/hold) with confidence and indicator context.

    Args:
        strategy_id: The strategy name in backtest_results_v2
            (e.g., 'trima_swing_pattern_bold_sweep')
        symbol: Ticker symbol (BTC, ETH, SOL). Default BTC.
        timestep: Data timestep. Default '4h'.

    Returns:
        dict with keys:
            - strategy_id, symbol, timestep
            - signal: "buy" | "sell" | "hold"
            - confidence: 0.0-1.0
            - price: current close price
            - indicators: dict of indicator values
            - strategy_meta: dict with composite_score, archetype, etc.
            - bars_analyzed: number of bars used
            - timestamp: ISO timestamp
            - error: (only if something went wrong)
            - cached: whether result came from cache
    """
    # Check cache
    cache_k = _cache_key(strategy_id, symbol, timestep, 500)
    now = time.time()
    if cache_k in _signal_cache:
        cached_time, cached_result = _signal_cache[cache_k]
        if now - cached_time < _CACHE_TTL_SECONDS:
            cached_result["cached"] = True
            return cached_result

    try:
        # 1. Load strategy from StratForge
        strat = _load_strategy(strategy_id)
        if "error" in strat:
            return {
                "strategy_id": strategy_id, "symbol": symbol,
                "timestep": timestep, "signal": "hold",
                "confidence": 0.0, "error": strat["error"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        source_code = strat["source_code"]
        params = json.loads(strat["params_json"]) if strat["params_json"] else None

        if not source_code or len(source_code) < 50:
            return {
                "strategy_id": strategy_id, "symbol": symbol,
                "timestep": timestep, "signal": "hold",
                "confidence": 0.0,
                "error": f"Source code too short ({len(source_code)} chars)",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # 2a. Check data staleness — refresh if cache is old
        data_source = "cache"
        data_stale = False
        refresh_meta = None
        if _LIVE_DATA_AVAILABLE:
            try:
                data_stale = is_data_stale(symbol, timestep)
                if data_stale:
                    logger.info("Data stale for %s %s — refreshing from Binance Testnet...", symbol, timestep)
                    refresh_meta = refresh_live_data(symbol, timestep)
                    if refresh_meta.get("exchange_status", '').startswith('ok'):
                        data_source = "live_refresh"
                    else:
                        logger.warning("Live refresh failed: %s", refresh_meta.get("exchange_status"))
            except Exception as refresh_exc:
                logger.warning("Live refresh error (non-fatal): %s", refresh_exc)

        # 2b. Load market data (now potentially refreshed)
        df = _load_market_data(symbol, timestep, bars=500)

        # 3. Execute strategy in sandbox
        entries, exits = _execute_strategy(source_code, df, params)

        # 4. Interpret signal
        signal_info = _interpret_signal(entries, exits, lookback=10)

        # 5. Extract indicators for context
        indicators = _extract_indicators(df, source_code)

        result = {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "timestep": timestep,
            "signal": signal_info["signal"],
            "confidence": signal_info["confidence"],
            "price": indicators.get("price"),
            "indicators": indicators,
            "strategy_meta": {
                "composite_score": strat["composite_score"],
                "archetype": strat["archetype"],
                "ticker": strat["ticker"],
                "backtest_return": strat["total_return"],
                "backtest_drawdown": strat["max_drawdown"],
                "backtest_trades": strat["num_trades"],
                "backtest_sharpe": strat["sharpe"],
                "backtest_sortino": strat["sortino"],
            },
            "bars_analyzed": len(df),
            "bars_since_entry": signal_info["bars_since_entry"],
            "bars_since_exit": signal_info["bars_since_exit"],
            "entries_in_lookback": signal_info["entries_in_lookback"],
            "exits_in_lookback": signal_info["exits_in_lookback"],
            "data_source": data_source,
            "data_stale": data_stale,
            "data_last_bar": df.index[-1].strftime("%Y-%m-%dT%H:%M:%S") if len(df) > 0 else None,
            "refresh_meta": refresh_meta,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cached": False,
        }

        # Cache it
        _signal_cache[cache_k] = (now, result.copy())

        logger.info(
            "evaluate_signal(%s, %s): signal=%s confidence=%.2f",
            strategy_id, symbol, result["signal"], result["confidence"],
        )
        return result

    except _TimeoutError:
        logger.error("Strategy '%s' timed out", strategy_id)
        return {
            "strategy_id": strategy_id, "symbol": symbol, "timestep": timestep,
            "signal": "hold", "confidence": 0.0,
            "error": "Strategy execution timed out (10s limit)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except ImportError as exc:
        logger.error("Strategy '%s' import violation: %s", strategy_id, exc)
        return {
            "strategy_id": strategy_id, "symbol": symbol, "timestep": timestep,
            "signal": "hold", "confidence": 0.0,
            "error": f"Import blocked: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.exception("Strategy '%s' failed", strategy_id)
        return {
            "strategy_id": strategy_id, "symbol": symbol, "timestep": timestep,
            "signal": "hold", "confidence": 0.0,
            "error": str(exc)[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    EVALUATE_SIGNAL = agent_tool(
        name="evaluate_signal",
        description=(
            "Run a StratForge strategy on current market data and return its signal. "
            "Loads the strategy source code from the backtest_results_v2 lakehouse, "
            "executes it against recent OHLCV bars, and returns buy/sell/hold with "
            "confidence and indicator context. Results are cached for 5 minutes. "
            "Use this to check what a specific validated strategy is signaling RIGHT NOW."
        ),
    )(evaluate_signal)

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorator skipped for evaluate_signal")
    EVALUATE_SIGNAL = evaluate_signal
