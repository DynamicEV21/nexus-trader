"""
Batch Crypto Strategy Screen
=============================

Runs StratForge winner strategies through LumiBot backtesting on BTC/ETH/SOL
using PandasDataBacktesting (fastest path, no network needed).

Usage:
    cd /home/Zev/development/trading-bots/lumibot
    source .venv/bin/activate

    python /home/Zev/development/nexus-trade/src/runners/batch_crypto_screen.py
        --strategies connors_rsi_starc_v2 trix_chop_multi_indicator
        --assets BTC ETH SOL
        --start 2021-06-01 --end 2024-12-31
        --budget 10000

    # Or run all winners from DuckDB:
    python batch_crypto_screen.py --all-winners --assets BTC ETH SOL
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

# ─── Path Setup ────────────────────────────────────────────────────────

LUMIBOT_ROOT = Path("/home/Zev/development/trading-bots/lumibot")
NEXUS_ROOT = Path("/home/Zev/development/nexus-trade")
STRATFORGE_ACTIVE = Path(
    os.path.expanduser(
        "~/.hermes/profiles/herm-bot/home/agentic-quant-os/strategies/stratforge/active"
    )
)
TEST_DATA_DIR = Path("/home/Zev/development/strat-depot/test_data")
SF_DB_PATH = os.path.expanduser(
    "~/.hermes/profiles/herm-bot/home/agentic-quant-os/data/quant.duckdb"
)

# LumiBot must be importable
sys.path.insert(0, str(LUMIBOT_ROOT))

from lumibot.backtesting import PandasDataBacktesting
from lumibot.entities import Asset, Data, TradingFee
from dotenv import load_dotenv

load_dotenv(str(LUMIBOT_ROOT / ".env"))
os.environ.setdefault("LUMIBOT_CACHE_FOLDER", "/tmp/lumibot_cache")

# Nexus adapter
sys.path.insert(0, str(NEXUS_ROOT / "src"))
from strategies.stratforge_adapter import StratForgeSignalAdapter
from lakehouse.stratforge_bridge import StratForgeBridge

# ─── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("batch_screen")
logging.getLogger("lumibot").setLevel(logging.WARNING)

# ─── Strategy Loading ──────────────────────────────────────────────────


def load_strategy_from_file(name: str) -> callable | None:
    """Load a strategy function from the stratforge active/ directory."""
    # Try exact filename first
    candidate = STRATFORGE_ACTIVE / f"{name}.py"
    if not candidate.exists():
        # Search by substring
        matches = list(STRATFORGE_ACTIVE.glob(f"*{name}*.py"))
        if matches:
            candidate = matches[0]
        else:
            logger.error(f"Strategy file not found: {name}")
            return None

    import importlib.util
    spec = importlib.util.spec_from_file_location(name, candidate)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.error(f"Failed to import {candidate.name}: {e}")
        return None

    # Find the strategy function
    for func_name in [f"strategy_{name}", "strategy"]:
        if hasattr(mod, func_name):
            return getattr(mod, func_name)

    # Search for any function returning a tuple
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if callable(attr) and attr_name.startswith("strategy"):
            return attr

    logger.error(f"No strategy function found in {candidate.name}")
    return None


def load_strategy_from_db(strategy_name: str) -> tuple[callable, dict] | None:
    """Load a strategy function from the StratForge DuckDB source_code column."""
    import duckdb

    try:
        con = duckdb.connect(SF_DB_PATH, read_only=True)
    except Exception as e:
        logger.error(f"Cannot open StratForge DB: {e}")
        return None

    row = con.execute(
        """
        SELECT source_code, params_json
        FROM backtest_results_v2
        WHERE strategy_name = ?
          AND status = 'winner'
          AND source_code IS NOT NULL
          AND LENGTH(source_code) > 100
        ORDER BY is_best_version DESC NULLS LAST
        LIMIT 1
        """,
        [strategy_name],
    ).fetchone()
    con.close()

    if not row:
        logger.error(f"Strategy '{strategy_name}' not found in DB")
        return None

    code, params_json = row

    # Execute in a namespace pre-seeded with the common scientific stack.
    # Many StratForge strategy snippets reference `np`, `pd`, and `talib`
    # without importing them (they were authored inside a notebook/REPL where
    # those names were already in scope). Injecting them here lets the source
    # code exec cleanly.
    import numpy as np
    import pandas as pd
    import talib
    ns = {"np": np, "pd": pd, "talib": talib, "numpy": np, "pandas": pd}
    try:
        exec(code, ns)
    except Exception as e:
        logger.error(f"Failed to exec source_code for {strategy_name}: {e}")
        return None

    # Find the strategy function.
    # Prefer the canonical names, then fall back to any top-level def in the
    # source (some strategies define a single function with a custom name
    # like ``strategy_h1`` or ``psar_linreg_coppock_v3``).
    import re as _re

    def _top_level_defs(src: str) -> list[str]:
        """Names of functions defined at column 0 in the source."""
        return [
            m.group(1)
            for m in _re.finditer(r"^def\s+(\w+)\s*\(", src, _re.MULTILINE)
        ]

    injected = {"np", "pd", "talib", "numpy", "pandas"}
    candidate_names = ["strategy", f"strategy_{strategy_name}"]
    candidate_names += [
        d for d in _top_level_defs(code) if d not in candidate_names
    ]

    for func_name in candidate_names:
        obj = ns.get(func_name)
        if callable(obj) and func_name not in injected:
            params = json.loads(params_json) if params_json else {}
            logger.debug(f"Loaded strategy fn '{func_name}' for {strategy_name}")
            return obj, params

    logger.error(f"No strategy function in source_code for {strategy_name}")
    return None


def get_all_winners_from_db() -> list[str]:
    """Get all winner strategy names from the StratForge DB."""
    import duckdb

    con = duckdb.connect(SF_DB_PATH, read_only=True)
    rows = con.execute(
        """
        SELECT DISTINCT strategy_name
        FROM backtest_results_v2
        WHERE status = 'winner'
        ORDER BY strategy_name
        """
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


# ─── Data Loading ──────────────────────────────────────────────────────

_ASSET_CACHE: dict[str, pd.DataFrame] = {}


def load_price_data(symbol: str) -> pd.DataFrame | None:
    """Load OHLCV parquet data for a crypto symbol."""
    if symbol in _ASSET_CACHE:
        return _ASSET_CACHE[symbol]

    parquet_path = TEST_DATA_DIR / f"{symbol}_5yr.parquet"
    if not parquet_path.exists():
        logger.error(f"Price data not found: {parquet_path}")
        return None

    df = pd.read_parquet(parquet_path)

    # Normalize columns to lowercase
    rename = {c: c.lower() for c in df.columns if c.lower() in ("open", "high", "low", "close", "volume")}
    if rename:
        df = df.rename(columns=rename)

    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        for col in ["Date", "date", "timestamp"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])
                df = df.set_index(col)
                break

    # Ensure required columns
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        logger.error(f"Missing columns in {symbol} data: {missing}")
        return None

    _ASSET_CACHE[symbol] = df
    logger.info(f"Loaded {symbol}: {len(df)} bars, {df.index[0].date()} → {df.index[-1].date()}")
    return df


# ─── Backtest Runner ───────────────────────────────────────────────────


def run_single_backtest(
    strategy_fn: callable,
    strategy_name: str,
    strategy_params: dict,
    symbol: str,
    df: pd.DataFrame,
    start: datetime,
    end: datetime,
    budget: float = 10000,
) -> dict:
    """Run a single strategy backtest and return metrics.

    The entire body is wrapped so that *any* failure (bad source code,
    signal-computation error, backtest crash) returns an ``error`` result row
    instead of killing the whole batch screen.
    """
    backtest_name = f"sf_{strategy_name}_{symbol}"

    try:
        # Pre-compute signals
        signal_df = StratForgeSignalAdapter.prepare_signals(
            strategy_fn, df, strategy_params
        )

        # Build asset pair
        base = Asset(symbol, Asset.AssetType.CRYPTO)
        quote = Asset("USDT", Asset.AssetType.CRYPTO)

        # Build pandas_data dict using Data wrapper
        data_item = Data(asset=base, df=df, quote=quote, timestep="day")
        pandas_data = [data_item]

        result = StratForgeSignalAdapter.backtest(
            PandasDataBacktesting,
            backtesting_start=start,
            backtesting_end=end,
            pandas_data=pandas_data,
            benchmark_asset=base,
            # 0.2% per side = 0.1% commission + 0.1% slippage
            buy_trading_fees=[TradingFee(percent_fee=0.002)],
            sell_trading_fees=[TradingFee(percent_fee=0.002)],
            quote_asset=quote,
            budget=budget,
            name=backtest_name,
            parameters={
                "base_symbol": symbol,
                "quote_symbol": "USDT",
                "signal_df": signal_df,
                "position_size": 0.95,
            },
            save_tearsheet=False,
            save_stats_file=True,
            show_plot=False,
            quiet_logs=True,
        )

        # Extract metrics from result dict
        stats = {}
        if result and isinstance(result, dict):
            max_dd = result.get("max_drawdown", {})
            if isinstance(max_dd, dict):
                max_dd = max_dd.get("drawdown", 0)
            stats = {
                "total_return_pct": round(result.get("total_return", 0) * 100, 2),
                "max_drawdown_pct": round(float(max_dd or 0) * 100, 2),
                "sharpe": round(result.get("sharpe", 0), 3),
                "cagr": round(result.get("cagr", 0) * 100, 2),
                "volatility": round(result.get("volatility", 0) * 100, 2),
                "romad": round(result.get("romad", 0), 3),
            }

        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "status": "success",
            "num_entries": int((signal_df["entry"] == 1).sum()),
            "num_exits": int((signal_df["exit"] == 1).sum()),
            "backtest_name": backtest_name,
            **stats,
        }

    except Exception as e:
        logger.error(f"FAILED {strategy_name} on {symbol}: {e}")
        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()[-500:],
        }


# ─── Results Storage ───────────────────────────────────────────────────


def save_results(results: list[dict], output_dir: Path) -> Path:
    """Save results to JSON and CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON (full detail)
    json_path = output_dir / f"screen_results_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    # CSV (flat summary)
    csv_path = output_dir / f"screen_results_{timestamp}.csv"
    df = pd.DataFrame(results)
    df.to_csv(csv_path, index=False)

    logger.info(f"Results saved: {json_path.name}, {csv_path.name}")
    return csv_path


# ─── Main ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Batch crypto strategy screen")
    parser.add_argument(
        "--strategies", nargs="+", default=["connors_rsi_starc_v2"],
        help="Strategy names to test",
    )
    parser.add_argument(
        "--all-winners", action="store_true",
        help="Load all winners from StratForge DB",
    )
    parser.add_argument(
        "--assets", nargs="+", default=["BTC", "ETH", "SOL"],
        help="Crypto symbols to test",
    )
    parser.add_argument("--start", default="2021-06-01", help="Backtest start date")
    parser.add_argument("--end", default="2024-12-31", help="Backtest end date")
    parser.add_argument("--budget", type=float, default=10000, help="Starting budget")
    parser.add_argument(
        "--source", choices=["file", "db"], default="file",
        help="Load strategies from disk or DuckDB",
    )
    parser.add_argument(
        "--output", default=str(NEXUS_ROOT / "results" / "batch_screen"),
        help="Output directory for results",
    )
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    # Resolve strategy list
    if args.all_winners:
        strategy_names = get_all_winners_from_db()
        args.source = "db"  # Must use DB for all-winners
        logger.info(f"Loaded {len(strategy_names)} winners from DB")
    else:
        strategy_names = args.strategies

    logger.info(f"Screening {len(strategy_names)} strategies × {len(args.assets)} assets")
    logger.info(f"Date range: {start.date()} → {end.date()}")
    logger.info(f"Budget: ${args.budget:,.0f}")

    all_results = []

    for sname in strategy_names:
        # Load strategy function
        if args.source == "db":
            loaded = load_strategy_from_db(sname)
            if not loaded:
                continue
            strategy_fn, strategy_params = loaded
        else:
            strategy_fn = load_strategy_from_file(sname)
            strategy_params = {}

        if strategy_fn is None:
            continue

        for symbol in args.assets:
            df = load_price_data(symbol)
            if df is None:
                continue

            logger.info(f"▶ {sname} on {symbol}...")
            result = run_single_backtest(
                strategy_fn=strategy_fn,
                strategy_name=sname,
                strategy_params=strategy_params,
                symbol=symbol,
                df=df,
                start=start,
                end=end,
                budget=args.budget,
            )
            all_results.append(result)

            if result["status"] == "success":
                ret = result.get("total_return_pct", "N/A")
                sharpe = result.get("sharpe", "N/A")
                logger.info(
                    f"  ✅ return={ret}%, sharpe={sharpe}"
                )
                # Record to StratForge DB
                try:
                    bridge = StratForgeBridge()
                    bridge.record_backtest_result(
                        strategy_name=sname,
                        symbol=symbol,
                        total_return_pct=ret,
                        max_drawdown_pct=result.get("max_drawdown_pct"),
                        sharpe=result.get("sharpe"),
                        cagr=result.get("cagr"),
                        volatility=result.get("volatility"),
                        romad=result.get("romad"),
                        num_entries=result.get("num_entries"),
                        num_exits=result.get("num_exits"),
                        backtest_start=args.start,
                        backtest_end=args.end,
                        budget=args.budget,
                    )
                except Exception as db_err:
                    logger.warning(f"  DB record failed: {db_err}")
            else:
                logger.warning(f"  ❌ {result.get('error', 'unknown error')}")

    # Save results
    csv_path = save_results(all_results, Path(args.output))

    # Print summary
    successful = [r for r in all_results if r["status"] == "success"]
    failed = [r for r in all_results if r["status"] == "error"]

    print(f"\n{'='*60}")
    print(f"BATCH SCREEN COMPLETE")
    print(f"{'='*60}")
    print(f"Total runs:     {len(all_results)}")
    print(f"Successful:     {len(successful)}")
    print(f"Failed:         {len(failed)}")
    print(f"Results saved:  {csv_path}")

    if successful:
        # Top 5 by Sharpe
        df_res = pd.DataFrame(successful)
        if "sharpe" in df_res.columns:
            df_res["sharpe_num"] = pd.to_numeric(df_res["sharpe"], errors="coerce")
            display_cols = [c for c in ["strategy", "symbol", "total_return_pct", "sharpe", "max_drawdown_pct"] if c in df_res.columns]
            top5 = df_res.nlargest(5, "sharpe_num")[display_cols]
            print(f"\nTop 5 by Sharpe:")
            print(top5.to_string(index=False))


if __name__ == "__main__":
    main()
