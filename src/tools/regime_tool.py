"""
Regime Detection Tool — LumiBot @agent_tool for CrabQuant regime detection
===========================================================================

Wraps ``crabquant.regime.detect_regime()`` as a LumiBot ``@agent_tool``
that an AI agent can call during committee deliberation.

The tool uses ``self.get_historical_prices()`` to fetch SPY and VIX data,
then delegates to CrabQuant for regime classification.

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

    strategy = get_strategy()
    if strategy is None:
        return {"regime": "unknown", "confidence": 0.0, "error": "No strategy registered"}

    try:
        # Fetch SPY historical data
        end = strategy.get_datetime()
        start = end - timedelta(days=max(lookback * 3, 150))
        spy_bars = strategy.get_historical_prices("SPY", length=lookback, timestep="day")
        if spy_bars is None:
            logger.warning("No SPY price data available — cannot detect regime")
            return {
                "regime": "unknown",
                "confidence": 0.0,
                "error": "No SPY price data available",
            }

        df_spy = spy_bars.df if hasattr(spy_bars, "df") else spy_bars
        if df_spy is None or df_spy.empty or "close" not in df_spy.columns:
            logger.warning("SPY data missing 'close' column or empty — cannot detect regime")
            return {
                "regime": "unknown",
                "confidence": 0.0,
                "error": "SPY data empty or missing close column",
            }

        # Fetch VIX data (optional — regime detection handles None gracefully)
        df_vix = None
        try:
            vix_bars = strategy.get_historical_prices("VIX", length=lookback, timestep="day")
            if vix_bars is not None:
                df_vix = vix_bars.df if hasattr(vix_bars, "df") else vix_bars
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
