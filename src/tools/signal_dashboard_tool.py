"""
Signal Dashboard Tool — Pre-computed indicator snapshot in one call
====================================================================

Provides AI agents with a comprehensive signal dashboard for a given
symbol.  Computes RSI, MACD, SMA crossovers, ATR, momentum, trend
alignment, and a risk-on/off recommendation in a single call.

This reduces the number of indicator-fetching tool calls the AI needs
to make, saving tokens and latency.

Requirements
------------
* LumiBot strategy instance with ``get_historical_prices()``.
* ``pandas`` and ``numpy`` for indicator calculations.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def signal_dashboard_tool(
    symbol: str,
    lookback: int = 100,
    ma_periods: str = "20,50,200",
) -> dict[str, Any]:
    """Compute a comprehensive signal dashboard for a single symbol.

    Calculates RSI, MACD, SMA crossovers, ATR, momentum, Bollinger Bands,
    trend alignment, and provides a risk-on/risk-off recommendation.

    This gives the AI agent a complete technical picture in one call.

    Args:
        symbol: Stock symbol to analyze (e.g., 'AAPL', 'SPY')
        lookback: Number of bars to analyze (default 100)
        ma_periods: Comma-separated SMA periods (default '20,50,200')

    Returns:
        dict with keys:
        - **symbol** (str)
        - **current_price** (float)
        - **rsi** (float) — 14-period RSI (0-100)
        - **rsi_signal** (str) — 'oversold', 'overbought', 'neutral'
        - **macd** (dict) — MACD line, signal line, histogram
        - **macd_signal** (str) — 'bullish_cross', 'bearish_cross', 'above', 'below', 'neutral'
        - **sma_values** (dict) — current SMA values for each period
        - **sma_position** (str) — price relative to SMAs
        - **sma_crossovers** (list) — recent SMA crossover events
        - **atr** (float) — 14-period Average True Range
        - **atr_pct** (float) — ATR as percentage of price
        - **momentum** (dict) — 5, 10, 20-bar rate of change
        - **bollinger_bands** (dict) — upper, middle, lower bands
        - **bb_position** (str) — price position within bands
        - **trend_alignment** (str) — 'bullish', 'bearish', 'mixed', 'neutral'
        - **volume_trend** (str) — 'increasing', 'decreasing', 'flat', 'unknown'
        - **risk_recommendation** (str) — 'risk_on', 'risk_off', 'neutral'
        - **summary** (str) — human-readable technical summary
    """
    from src.tools._strategy_context import get_strategy

    strategy = get_strategy()
    if strategy is None:
        return {"symbol": symbol, "error": "No strategy registered"}

    try:
        df = strategy.get_historical_prices(symbol, length=lookback, timestep="day")
        if df is None:
            return {"symbol": symbol, "error": "No price data available"}

        data = df.df if hasattr(df, "df") else df
        if data is None or data.empty:
            return {"symbol": symbol, "error": "Empty price data"}

        close = data["close"]
        high = data.get("high", close)
        low = data.get("low", close)
        volume = data.get("volume", None)

        current_price = float(close.iloc[-1])
        result: dict[str, Any] = {
            "symbol": symbol,
            "current_price": round(current_price, 2),
        }

        # ── RSI (14-period) ──
        rsi = _compute_rsi(close, period=14)
        rsi_value = round(rsi, 1) if not np.isnan(rsi) else 0.0
        result["rsi"] = rsi_value
        if rsi_value > 70:
            result["rsi_signal"] = "overbought"
        elif rsi_value < 30:
            result["rsi_signal"] = "oversold"
        else:
            result["rsi_signal"] = "neutral"

        # ── MACD (12, 26, 9) ──
        macd_data = _compute_macd(close)
        result["macd"] = macd_data
        macd_line = macd_data.get("macd", 0)
        signal_line = macd_data.get("signal", 0)
        if macd_line > signal_line and macd_data.get("prev_macd", macd_line) <= macd_data.get("prev_signal", signal_line):
            result["macd_signal"] = "bullish_cross"
        elif macd_line < signal_line and macd_data.get("prev_macd", macd_line) >= macd_data.get("prev_signal", signal_line):
            result["macd_signal"] = "bearish_cross"
        elif macd_line > signal_line:
            result["macd_signal"] = "above"
        elif macd_line < signal_line:
            result["macd_signal"] = "below"
        else:
            result["macd_signal"] = "neutral"

        # ── SMA values ──
        periods = [int(p.strip()) for p in ma_periods.split(",") if p.strip().isdigit()]
        sma_values: dict[str, float] = {}
        price_vs_sma: list[str] = []
        for period in periods:
            if len(close) >= period:
                sma = close.rolling(period).mean().iloc[-1]
                sma_values[str(period)] = round(float(sma), 2)
                pct_diff = (current_price - sma) / sma * 100
                if pct_diff > 1:
                    price_vs_sma.append(f"above_{period}d")
                elif pct_diff < -1:
                    price_vs_sma.append(f"below_{period}d")
                else:
                    price_vs_sma.append(f"near_{period}d")
        result["sma_values"] = sma_values

        # Price position relative to SMAs
        if all("above" in p for p in price_vs_sma):
            result["sma_position"] = "above_all"
        elif all("below" in p for p in price_vs_sma):
            result["sma_position"] = "below_all"
        elif any("above" in p for p in price_vs_sma) and any("below" in p for p in price_vs_sma):
            result["sma_position"] = "mixed"
        else:
            result["sma_position"] = "neutral"

        # SMA crossovers (recent)
        crossovers: list[str] = []
        if len(close) >= max(periods):
            for i in range(len(periods)):
                for j in range(i + 1, len(periods)):
                    fast_period = min(periods[i], periods[j])
                    slow_period = max(periods[i], periods[j])
                    fast_sma = close.rolling(fast_period).mean()
                    slow_sma = close.rolling(slow_period).mean()
                    if fast_sma.iloc[-1] > slow_sma.iloc[-1] and fast_sma.iloc[-2] <= slow_sma.iloc[-2]:
                        crossovers.append(f"golden_cross_{fast_period}d_above_{slow_period}d")
                    elif fast_sma.iloc[-1] < slow_sma.iloc[-1] and fast_sma.iloc[-2] >= slow_sma.iloc[-2]:
                        crossovers.append(f"death_cross_{fast_period}d_below_{slow_period}d")
        result["sma_crossovers"] = crossovers[-5:] if crossovers else []

        # ── ATR (14-period) ──
        if high is not None and low is not None:
            atr = _compute_atr(high, low, close, period=14)
            atr_value = round(float(atr), 2) if not np.isnan(atr) else 0.0
            result["atr"] = atr_value
            result["atr_pct"] = round(atr_value / current_price * 100, 2) if current_price else 0.0

        # ── Momentum (ROC) ──
        momentum: dict[str, float] = {}
        for period in [5, 10, 20]:
            if len(close) > period:
                roc = (close.iloc[-1] - close.iloc[-period - 1]) / close.iloc[-period - 1] * 100
                momentum[str(period)] = round(float(roc), 2)
        result["momentum"] = momentum

        # ── Bollinger Bands (20, 2) ──
        bb = _compute_bollinger_bands(close, period=20, std_dev=2.0)
        result["bollinger_bands"] = bb
        upper = bb.get("upper", current_price)
        lower = bb.get("lower", current_price)
        if current_price >= upper * 0.995:
            result["bb_position"] = "at_upper"
        elif current_price <= lower * 1.005:
            result["bb_position"] = "at_lower"
        elif current_price > bb.get("middle", current_price):
            result["bb_position"] = "above_middle"
        else:
            result["bb_position"] = "below_middle"

        # ── Trend alignment ──
        trend_signals: list[str] = []
        # Price above 20SMA
        if "20" in sma_values and current_price > sma_values["20"]:
            trend_signals.append("bull")
        elif "20" in sma_values:
            trend_signals.append("bear")

        # 20-period momentum
        if momentum.get("20", 0) > 2:
            trend_signals.append("bull")
        elif momentum.get("20", 0) < -2:
            trend_signals.append("bear")

        # MACD
        if macd_line > signal_line:
            trend_signals.append("bull")
        elif macd_line < signal_line:
            trend_signals.append("bear")

        # RSI
        if rsi_value > 50:
            trend_signals.append("bull")
        elif rsi_value < 50:
            trend_signals.append("bear")

        bulls = trend_signals.count("bull")
        bears = trend_signals.count("bear")
        if bulls >= 3:
            result["trend_alignment"] = "bullish"
        elif bears >= 3:
            result["trend_alignment"] = "bearish"
        elif bulls > 0 or bears > 0:
            result["trend_alignment"] = "mixed"
        else:
            result["trend_alignment"] = "neutral"

        # ── Volume trend ──
        if volume is not None and len(volume) >= 20:
            vol_short = volume.iloc[-5:].mean()
            vol_long = volume.iloc[-20:].mean()
            if vol_long > 0:
                vol_ratio = vol_short / vol_long
                if vol_ratio > 1.5:
                    result["volume_trend"] = "increasing"
                elif vol_ratio < 0.5:
                    result["volume_trend"] = "decreasing"
                else:
                    result["volume_trend"] = "flat"
            else:
                result["volume_trend"] = "unknown"
        else:
            result["volume_trend"] = "unknown"

        # ── Risk-on / Risk-off recommendation ──
        risk_score = 0
        # Trend bullish → risk-on
        if result["trend_alignment"] == "bullish":
            risk_score += 2
        elif result["trend_alignment"] == "bearish":
            risk_score -= 2

        # RSI not extreme → risk-on
        if 40 <= rsi_value <= 60:
            risk_score += 1
        elif rsi_value > 70:
            risk_score -= 1

        # Price above 50SMA → risk-on
        if "50" in sma_values and current_price > sma_values["50"]:
            risk_score += 1
        elif "50" in sma_values:
            risk_score -= 1

        # MACD bullish → risk-on
        if result["macd_signal"] in ("bullish_cross", "above"):
            risk_score += 1
        elif result["macd_signal"] in ("bearish_cross", "below"):
            risk_score -= 1

        if risk_score >= 3:
            result["risk_recommendation"] = "risk_on"
        elif risk_score <= -3:
            result["risk_recommendation"] = "risk_off"
        else:
            result["risk_recommendation"] = "neutral"

        # ── Summary ──
        summary_parts = [
            f"{symbol} @ ${current_price:.2f}",
            f"RSI={rsi_value:.0f} ({result['rsi_signal']})",
            f"MACD={result['macd_signal']}",
            f"Trend={result['trend_alignment']}",
            f"Position={result['sma_position']}",
            f"Risk={result['risk_recommendation']}",
        ]
        result["summary"] = " | ".join(summary_parts)

        logger.debug("Signal dashboard for %s: %s", symbol, result["summary"])
        return result

    except Exception as exc:
        logger.exception("Failed to compute signal dashboard for %s", symbol)
        return {"symbol": symbol, "error": str(exc)}


# ---------------------------------------------------------------------------
# Indicator calculation helpers
# ---------------------------------------------------------------------------

def _compute_rsi(close, period: int = 14) -> float:
    """Compute RSI (Relative Strength Index)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    # Use Wilder's smoothing for subsequent periods
    for i in range(period, len(avg_gain)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0


def _compute_macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """Compute MACD line, signal line, and histogram."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return {
        "macd": round(float(macd_line.iloc[-1]), 4),
        "signal": round(float(signal_line.iloc[-1]), 4),
        "histogram": round(float(histogram.iloc[-1]), 4),
        "prev_macd": round(float(macd_line.iloc[-2]), 4) if len(macd_line) > 1 else 0,
        "prev_signal": round(float(signal_line.iloc[-2]), 4) if len(signal_line) > 1 else 0,
    }


def _compute_atr(high, low, close, period: int = 14) -> float:
    """Compute Average True Range."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = np.maximum(np.maximum(tr1, tr2), tr3)
    atr = true_range.rolling(window=period, min_periods=period).mean()
    # Wilder's smoothing
    for i in range(period, len(atr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + true_range.iloc[i]) / period
    return float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else 0.0


def _compute_bollinger_bands(close, period: int = 20, std_dev: float = 2.0):
    """Compute Bollinger Bands."""
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    return {
        "upper": round(float(sma.iloc[-1] + std_dev * std.iloc[-1]), 2),
        "middle": round(float(sma.iloc[-1]), 2),
        "lower": round(float(sma.iloc[-1] - std_dev * std.iloc[-1]), 2),
        "width": round(float((2 * std_dev * std.iloc[-1]) / sma.iloc[-1]), 4) if sma.iloc[-1] else 0.0,
    }


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    SIGNAL_DASHBOARD = agent_tool(
        name="signal_dashboard",
        description=(
            "Get a comprehensive technical signal dashboard for a symbol in one call. "
            "Includes RSI (with overbought/oversold signal), MACD (with crossover detection), "
            "SMA values and crossovers (golden/death crosses), ATR (volatility), "
            "multi-period momentum, Bollinger Bands with position, trend alignment, "
            "volume trend, and a risk-on/risk-off recommendation. "
            "Saves the AI from making multiple separate indicator calls."
        ),
    )(signal_dashboard_tool)

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorator skipped for signal_dashboard")
    SIGNAL_DASHBOARD = signal_dashboard_tool
