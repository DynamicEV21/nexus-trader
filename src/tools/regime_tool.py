"""
Regime Detection Tool — LumiBot @agent_tool for CrabQuant regime detection
===========================================================================

Wraps ``crabquant.regime.detect_regime()`` as a LumiBot ``@agent_tool``
that an AI agent can call during committee deliberation.

The tool uses ``self.get_historical_prices()`` to fetch SPY data (with
optional VIX), then delegates to CrabQuant for regime classification.

Notes on crypto broker
----------------------
On a crypto broker (Binance, etc.) the only equity TradFi perpetuals
available are pairs like ``SPY/USDT:USDT`` (Binance) or
``SPY-PERP/USDC`` (Hyperliquid). The tool:

* Requests SPY with ``quote=USDT`` so the broker resolves the symbol
  to the right perpetuals contract.
* Treats VIX as fully optional — never blocks regime detection if
  VIX is missing (the underlying CrabQuant detector handles
  ``vix_data=None`` gracefully).
* Returns ``regime="unknown"`` with a descriptive error message if
  the broker has no SPY-equivalent at all (e.g., a pure-crypto
  exchange with no TradFi perpetuals).

Requirements
------------
* CrabQuant must be importable (``~/development/CrabQuant`` on sys.path).
* LumiBot strategy instance must implement ``get_historical_prices()``.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any

# Ensure CrabQuant is importable
_CRABQUANT_PATH = os.path.expanduser("~/development/CrabQuant")
if _CRABQUANT_PATH not in sys.path:
    sys.path.insert(0, _CRABQUANT_PATH)

logger = logging.getLogger(__name__)


def _fetch_regime_data(
    strategy,
    symbol: str,
    lookback: int,
    LumiBotAsset,
) -> "pd.DataFrame | None":
    """Fetch OHLCV bars for a regime input symbol (SPY, VIX, BTC-proxy).

    Strategy
    --------
    1. Try lumibot's ``get_historical_prices()`` with ``quote=USDT`` first
       (works for crypto pairs, like ``BTC/USDT``).
    2. If that returns nothing AND the symbol is an equity (SPY/VIX),
       try a direct ccxt call with the proper ``SYMBOL/USDT:USDT``
       perpetual-swap format that Binance uses. This bypasses lumibot's
       symbol-normalization which drops the ``:USDT`` colon suffix.
       For SPY/VIX we use 1h bars because Binance testnet only has ~10
       days of daily bars (TradFi perpetuals are new). With 1h bars we
       can get 200+ samples in the same window.
    3. Return a pandas DataFrame with a ``close`` column, or None on failure.

    Why not always use the direct ccxt path?
    ----------------------------------------
    The direct ccxt path only works if the lumibot strategy exposes a
    ``broker.api`` attribute (i.e., it's a live ccxt broker, not a
    backtest data source). For backtests we want to use the backtest
    data source to stay consistent with the rest of the simulation.
    """
    import pandas as pd

    # ── Path 1: lumibot get_historical_prices with quote=USDT ──
    # Skip this path for SPY/VIX — lumibot's symbol format is wrong on
    # Binance (drops the :USDT colon). Go straight to direct ccxt.
    if symbol not in ("SPY", "VIX"):
        try:
            kwargs = {"length": max(lookback + 10, 30), "timestep": "day"}
            if LumiBotAsset is not None:
                kwargs["quote"] = LumiBotAsset("USDT", "crypto")
            bars = strategy.get_historical_prices(symbol, **kwargs)
            if bars is not None and not (isinstance(bars, float) and bars != bars):
                df = bars.df if hasattr(bars, "df") else bars
                if df is not None and not df.empty and "close" in df.columns:
                    logger.debug("Fetched %d bars for %s via lumibot", len(df), symbol)
                    return df
        except Exception as exc:
            logger.debug("lumibot fetch for %s failed: %s", symbol, exc)

    # ── Path 2: direct ccxt call for Binance TradFi perpetuals (SPY, VIX, etc.) ──
    # Binance's symbol format is e.g. SPY/USDT:USDT. Lumibot's get_historical_prices
    # builds ``SPY/USDT`` (no colon), which doesn't match. So we go around it.
    if symbol in ("SPY", "VIX") or symbol.endswith("PERP"):
        try:
            api = getattr(getattr(strategy, "broker", None), "api", None)
            if api is not None and hasattr(api, "fetch_ohlcv"):
                ccxt_symbol = f"{symbol}/USDT:USDT"
                # SPY/VIX on Binance testnet only have ~10 daily bars. Use
                # 1h bars to get more samples. We then resample to daily
                # (close at end of UTC day) for the CrabQuant detector.
                ccxt_timeframe = "1h"
                ohlcv = api.fetch_ohlcv(
                    ccxt_symbol, ccxt_timeframe, limit=min(1000, max(lookback * 4, 200))
                )
                if ohlcv:
                    df = pd.DataFrame(
                        ohlcv, columns=["datetime", "open", "high", "low", "close", "volume"]
                    )
                    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
                    df = df.set_index("datetime").sort_index()
                    logger.info(
                        "Fetched %d %s bars for %s via direct ccxt (%s)",
                        len(df),
                        ccxt_timeframe,
                        symbol,
                        ccxt_symbol,
                    )
                    return df
        except Exception as exc:
            logger.debug("Direct ccxt fetch for %s failed: %s", symbol, exc)

    return None


def detect_regime_tool(lookback: int = 50) -> dict[str, Any]:
    """Detect the current market regime using SPY + VIX data.

    Calls CrabQuant's ``detect_regime()``, which uses SMA slopes,
    Bollinger Band width, rate of change, VIX level, and realized
    volatility to classify the market into one of five regimes:

    - **trending_up** — strong uptrend with positive momentum
    - **trending_down** — strong downtrend with negative momentum
    - **mean_reversion** — sideways / choppy, reversion opportunities
    - **high_volatility** — elevated VIX, wide bands, risk-off
    - **low_volatility** — calm market, tight bands, trending or drift

    Args:
        lookback: Number of bars to analyze (default 50).

    Returns:
        dict with keys:
        - **regime** (str) — the detected market regime
        - **confidence** (float) — 0-1 confidence score
        - **scores** (dict) — score breakdown for each regime
        - **vix_value** (float or None) — current VIX level
        - **realized_vol** (float) — annualized realized volatility
        - **bb_width** (float) — Bollinger Band width
        - **sma20_slope** (float) — normalized SMA-20 slope
        - **roc_20** (float) — 20-bar rate of change
    """
    from crabquant.regime import detect_regime
    from src.tools._strategy_context import get_strategy

    # Import Asset for proper quote-asset binding (crypto brokers need
    # Asset("USDT", "crypto") explicitly to resolve SPY/USDT:USDT)
    try:
        from lumibot.entities import Asset as LumiBotAsset
    except ImportError:
        LumiBotAsset = None  # type: ignore[assignment]

    strategy = get_strategy()
    if strategy is None:
        return {"regime": "unknown", "confidence": 0.0, "error": "No strategy registered"}

    try:
        end = strategy.get_datetime()
        start = end - timedelta(days=max(lookback * 3, 150))

        df_spy = _fetch_regime_data(strategy, "SPY", lookback, LumiBotAsset)
        if df_spy is None:
            # Try BTC as a crypto-market proxy (the broker is crypto-only).
            logger.info("SPY unavailable on this broker — falling back to BTC as crypto market proxy")
            df_spy = _fetch_regime_data(strategy, "BTC", lookback, LumiBotAsset)
            if df_spy is None:
                logger.warning("No SPY/BTC price data available — cannot detect regime")
                return {
                    "regime": "unknown",
                    "confidence": 0.0,
                    "error": "No price data for SPY or BTC (proxy)",
                }

        # Fetch VIX data (optional — regime detection handles None gracefully).
        # VIX is not available on most crypto brokers; if unavailable we proceed
        # with SPY-only regime detection (CrabQuant handles vix_data=None fine).
        df_vix = None
        try:
            vix_df = _fetch_regime_data(strategy, "VIX", lookback, LumiBotAsset)
            if vix_df is not None:
                df_vix = vix_df
        except Exception:
            logger.debug("VIX data unavailable — proceeding with SPY only", exc_info=True)

        # Run CrabQuant regime detection
        regime, metadata = detect_regime(df_spy, vix_data=df_vix, lookback=lookback)

        result = {
            "regime": regime.value,
            "confidence": metadata.get("confidence", 0.0),
            "scores": metadata.get("scores", {}),
            "vix_value": metadata.get("vix_value"),
            "realized_vol": metadata.get("realized_vol", 0.0),
            "bb_width": metadata.get("bb_width", 0.0),
            "sma20_slope": metadata.get("sma20_slope", 0.0),
            "roc_20": metadata.get("roc_20", 0.0),
        }

        logger.info(
            "Regime detected: %s (confidence=%.2f, vix=%.1f)",
            result["regime"],
            result["confidence"],
            result["vix_value"] or 0,
        )
        return result

    except Exception as exc:
        logger.exception("Failed to detect market regime")
        return {
            "regime": "unknown",
            "confidence": 0.0,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------
# The decorator is applied at import time if lumibot is available.
# In environments without lumibot (e.g., standalone testing), this is
# silently skipped.

try:
    from lumibot.components.agents.tools import agent_tool

    DETECT_REGIME_TOOL = agent_tool(
        name="detect_regime",
        description=(
            "Detect the current market regime (trending_up, trending_down, "
            "mean_reversion, high_volatility, low_volatility) using SPY price "
            "action, VIX, SMA slopes, Bollinger Bands, and realized volatility. "
            "Returns regime label, confidence, and all indicator scores."
        ),
    )(detect_regime_tool)

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorator skipped for detect_regime")
    DETECT_REGIME_TOOL = detect_regime_tool
