"""
Live Data Ingestion — Fetch fresh OHLCV bars from Binance Testnet
==================================================================

This module bridges the gap between stale parquet caches (ending 2025-12-31)
and live market data from the Binance Testnet. It is used by
``evaluate_signal`` to ensure strategies always run against the most
recent bars available.

Usage:
    from src.data.live_data import refresh_live_data

    result = refresh_live_data("BTC", "4h")
    # → {"bars_fetched": 42, "last_bar_timestamp": "2026-06-23T16:00:00",
    #    "cache_path": "...", "exchange_status": "ok", "latency_ms": 180}

Environment variables (from .env):
    BINANCE_TESTNET_KEY      — API key for Binance Testnet
    BINANCE_TESTNET_SECRET   — API secret for Binance Testnet
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.environ.get(
    "NEXUS_PROJECT_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
))
_DATA_CACHE = _PROJECT_ROOT / "data" / "4h_cache"
_METADATA_FILE = _DATA_CACHE / "metadata.json"

# ---------------------------------------------------------------------------
# Symbol mapping: internal → ccxt unified pair
# ---------------------------------------------------------------------------

_SYMBOL_TO_PAIR = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "SOL": "SOL/USDT",
    "ADA": "ADA/USDT",
    "DOT": "DOT/USDT",
    "LINK": "LINK/USDT",
    "LTC": "LTC/USDT",
    "XRP": "XRP/USDT",
    "AVAX": "AVAX/USDT",
    "ATOM": "ATOM/USDT",
    "BNB": "BNB/USDT",
}

# Timeframe → seconds (for staleness checks)
_TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
}


def _get_exchange(exchange: str = "binance_testnet"):
    """Create a ccxt exchange instance.

    For market data (OHLCV) we use Binance **mainnet public endpoints** —
    no authentication required and prices are real.  The testnet has
    synthetic/erratic price data that is unsuitable for strategy evaluation.

    The ``binance_testnet`` option is kept for completeness (e.g. testing
    order placement) but should NOT be used for live OHLCV refresh.
    """
    import ccxt

    if exchange == "binance_mainnet":
        # Public market data — no API key needed
        return ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })

    if exchange == "binance_testnet":
        api_key = os.environ.get("BINANCE_TESTNET_KEY", "")
        api_secret = os.environ.get("BINANCE_TESTNET_SECRET", "")
        if not api_key or not api_secret:
            raise RuntimeError(
                "Binance Testnet credentials not found. "
                "Set BINANCE_TESTNET_KEY and BINANCE_TESTNET_SECRET in .env"
            )
        return ccxt.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "sandbox": True,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })

    # Allow direct ccxt exchange name (e.g. 'kraken', 'bybit')
    ex_cls = getattr(ccxt, exchange)
    return ex_cls({"enableRateLimit": True})


def _validate_bar(bar: list, prev_close: float | None = None) -> bool:
    """Validate a single OHLCV bar from the exchange.

    Drops bars with:
    - Zero volume
    - NaN close
    - >50% bar-over-bar move (almost certainly bad data)
    - Non-positive prices
    """
    try:
        _ts, o, h, l, c, v = bar
        if any(pd_na(x) for x in (o, h, l, c, v)):
            return False
        if v <= 0:
            return False
        if c <= 0 or o <= 0 or h <= 0 or l <= 0:
            return False
        if h < l or h < max(o, c) or l > min(o, c):
            logger.warning("Inconsistent OHLC bar dropped: O=%s H=%s L=%s C=%s", o, h, l, c)
            return False
        if prev_close is not None and prev_close > 0:
            pct_move = abs(c - prev_close) / prev_close
            if pct_move > 0.50:
                logger.warning("Extreme move dropped: %.1f%% (close=%s, prev=%s)",
                               pct_move * 100, c, prev_close)
                return False
        return True
    except (ValueError, TypeError, IndexError):
        return False


def pd_na(val) -> bool:
    """Check if a value is NaN/None without importing pandas."""
    import math
    if val is None:
        return True
    try:
        return math.isnan(val)
    except (TypeError, ValueError):
        return False


def _convert_ohlcv_to_rows(ohlcv: list[list], timeframe: str = "4h") -> list[dict]:
    """Convert ccxt OHLCV [[ts, o, h, l, c, v], ...] to row dicts.

    Timestamps are converted to UTC tz-naive DatetimeIndex-compatible strings.
    ccxt returns timestamps in milliseconds.
    """
    import pandas as pd

    rows = []
    prev_close = None

    for bar in ohlcv:
        ts_ms, o, h, l, c, v = bar
        if _validate_bar(bar, prev_close):
            dt = pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_localize(None)
            rows.append({
                "Date": dt,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            })
            prev_close = float(c)
        else:
            logger.debug("Bar dropped by validation: ts=%s", ts_ms)

    return rows


def _update_metadata(symbol: str, timeframe: str, bars_fetched: int,
                     last_bar_ts: str, cache_path: Path) -> dict:
    """Update metadata.json with latest refresh info."""
    meta = {}
    if _METADATA_FILE.exists():
        try:
            meta = json.loads(_METADATA_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            meta = {}

    key = f"{symbol}_{timeframe}"
    meta[key] = {
        "last_refresh": datetime.now(timezone.utc).isoformat(),
        "bars_fetched": bars_fetched,
        "last_bar_timestamp": last_bar_ts,
        "cache_path": str(cache_path),
    }
    meta["_last_global_refresh"] = datetime.now(timezone.utc).isoformat()

    try:
        _METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        _METADATA_FILE.write_text(json.dumps(meta, indent=2, default=str))
    except OSError as exc:
        logger.warning("Failed to write metadata.json: %s", exc)

    return meta.get(key, {})


def refresh_live_data(
    symbol: str = "BTC",
    timeframe: str = "4h",
    exchange: str = "binance_testnet",
) -> dict:
    """Fetch latest bars from Binance Testnet and append to parquet cache.

    Args:
        symbol: Canonical symbol (BTC, ETH, SOL, etc.)
        timeframe: Bar timeframe (4h, 1h, 1d, etc.)
        exchange: Exchange identifier (default: binance_testnet)

    Returns:
        dict with keys:
            - bars_fetched: int — new bars added to cache
            - last_bar_timestamp: str — ISO timestamp of the most recent bar
            - cache_path: Path — location of the updated parquet file
            - exchange_status: str — "ok" or error description
            - latency_ms: int — round-trip API latency
    """
    import pandas as pd

    # Use mainnet public endpoints for market data by default.
    # The testnet has synthetic prices that are useless for strategy evaluation.
    data_exchange = exchange if exchange != "binance_testnet" else "binance_mainnet"

    pair = _SYMBOL_TO_PAIR.get(symbol.upper(), f"{symbol.upper()}/USDT")
    cache_path = _DATA_CACHE / f"{symbol.upper()}_{timeframe}.parquet"

    # 1. Load existing parquet to find the last bar timestamp
    if cache_path.exists():
        existing_df = pd.read_parquet(cache_path)
        if not isinstance(existing_df.index, pd.DatetimeIndex):
            return {
                "bars_fetched": 0,
                "last_bar_timestamp": None,
                "cache_path": str(cache_path),
                "exchange_status": "error: parquet index is not DatetimeIndex",
                "latency_ms": 0,
            }
        # Ensure tz-naive
        if existing_df.index.tz is not None:
            existing_df.index = existing_df.index.tz_convert("UTC").tz_localize(None)

        last_bar_dt = existing_df.index[-1]
        # Convert to ms timestamp for ccxt
        since_ms = int(last_bar_dt.timestamp() * 1000)
        logger.info(
            "Existing cache: %d bars, last=%s. Fetching since %s ms.",
            len(existing_df), last_bar_dt, since_ms,
        )
    else:
        # No existing cache — fetch from far back
        existing_df = pd.DataFrame()
        # Default: 500 bars back from now
        tf_seconds = _TIMEFRAME_SECONDS.get(timeframe, 14400)
        since_ms = int((time.time() - 500 * tf_seconds) * 1000)
        logger.info("No existing cache for %s. Fetching 500 bars from %s ms.", pair, since_ms)

    # 2. Connect to exchange and fetch OHLCV
    try:
        ex = _get_exchange(data_exchange)
    except Exception as exc:
        logger.error("Exchange connection failed: %s", exc)
        return {
            "bars_fetched": 0,
            "last_bar_timestamp": str(last_bar_dt) if not existing_df.empty else None,
            "cache_path": str(cache_path),
            "exchange_status": f"error: {exc}",
            "latency_ms": 0,
        }

    # 3. Fetch bars — paginate if needed (ccxt limit: 1000 per call)
    t0 = time.monotonic()
    try:
        tf_seconds = _TIMEFRAME_SECONDS.get(timeframe, 14400)
        now_ms = int(time.time() * 1000)
        raw_ohlcv: list[list] = []
        cursor_ms = since_ms
        max_pages = 5  # safety cap: 5 × 1000 = 5000 bars max

        for page in range(max_pages):
            page_data = ex.fetch_ohlcv(pair, timeframe, since=cursor_ms, limit=1000)
            if not page_data:
                break
            raw_ohlcv.extend(page_data)
            # Advance cursor past the last bar received
            last_ts = page_data[-1][0]
            cursor_ms = last_ts + int(tf_seconds * 1000)  # next bar after this one
            logger.debug("Page %d: %d bars (total %d), cursor=%s",
                         page + 1, len(page_data), len(raw_ohlcv), cursor_ms)
            # Stop if we're caught up to now or got fewer than 1000 (end of data)
            if len(page_data) < 1000 or cursor_ms >= now_ms:
                break
            # Be nice to the API
            time.sleep(0.2)
    except Exception as exc:
        logger.error("fetch_ohlcv failed for %s %s: %s", pair, timeframe, exc)
        return {
            "bars_fetched": 0,
            "last_bar_timestamp": str(last_bar_dt) if not existing_df.empty else None,
            "cache_path": str(cache_path),
            "exchange_status": f"error: {exc}",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }

    latency_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "Fetched %d raw OHLCV bars for %s %s from %s in %dms",
        len(raw_ohlcv), pair, timeframe, data_exchange, latency_ms,
    )

    # 4. Convert and validate
    new_rows = _convert_ohlcv_to_rows(raw_ohlcv, timeframe)
    if not new_rows:
        logger.warning("No valid bars after validation")
        return {
            "bars_fetched": 0,
            "last_bar_timestamp": str(last_bar_dt) if not existing_df.empty else None,
            "cache_path": str(cache_path),
            "exchange_status": "ok (no new valid bars)",
            "latency_ms": latency_ms,
        }

    new_df = pd.DataFrame(new_rows).set_index("Date")

    # 5. Merge with existing, deduplicate by index, sort
    if not existing_df.empty:
        combined = pd.concat([existing_df, new_df])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()
    else:
        combined = new_df.sort_index()

    # Determine how many are genuinely new
    if not existing_df.empty:
        new_count = len(combined) - len(existing_df)
    else:
        new_count = len(combined)

    # 6. Atomic write (write to .tmp, rename)
    tmp_path = cache_path.with_suffix(".parquet.tmp")
    try:
        combined.to_parquet(tmp_path, engine="pyarrow")
        tmp_path.rename(cache_path)
        logger.info("Wrote %d bars to %s (%d new)", len(combined), cache_path, new_count)
    except Exception as exc:
        logger.error("Failed to write parquet: %s", exc)
        # Clean up tmp
        if tmp_path.exists():
            tmp_path.unlink()
        return {
            "bars_fetched": 0,
            "last_bar_timestamp": str(combined.index[-1]),
            "cache_path": str(cache_path),
            "exchange_status": f"error writing parquet: {exc}",
            "latency_ms": latency_ms,
        }

    last_bar_iso = combined.index[-1].strftime("%Y-%m-%dT%H:%M:%S")

    # 7. Update metadata.json
    _update_metadata(symbol, timeframe, new_count, last_bar_iso, cache_path)

    return {
        "bars_fetched": new_count,
        "last_bar_timestamp": last_bar_iso,
        "cache_path": str(cache_path),
        "exchange_status": "ok",
        "latency_ms": latency_ms,
    }


def is_data_stale(symbol: str = "BTC", timeframe: str = "4h",
                  staleness_multiplier: float = 2.0) -> bool:
    """Check if cached data is stale (last bar older than multiplier * timeframe).

    Args:
        symbol: Canonical symbol (BTC, ETH, etc.)
        timeframe: Bar timeframe (4h, 1h, etc.)
        staleness_multiplier: How many timeframes old before considered stale.

    Returns:
        True if data is stale or missing.
    """
    import pandas as pd

    cache_path = _DATA_CACHE / f"{symbol.upper()}_{timeframe}.parquet"
    if not cache_path.exists():
        return True

    try:
        df = pd.read_parquet(cache_path, columns=["close"])
        if df.empty:
            return True
        last_bar = df.index[-1]
        if isinstance(last_bar, str):
            last_bar = pd.Timestamp(last_bar)

        tf_seconds = _TIMEFRAME_SECONDS.get(timeframe, 14400)
        threshold = tf_seconds * staleness_multiplier
        age = (datetime.now(timezone.utc) - last_bar.tz_localize("UTC")).total_seconds()
        return age > threshold
    except Exception as exc:
        logger.warning("Staleness check failed: %s", exc)
        return True
