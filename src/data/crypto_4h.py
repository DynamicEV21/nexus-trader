"""
4H Data Pipeline
================

Resamples the 1-minute Kraken OHLCV data from the agentic-quant-os DuckDB
lakehouse into 4-hour bars. This gives us 10x more bars than daily data,
which is critical for robust walk-forward validation.

Daily 5yr:  ~1,827 bars per asset
4H 5yr:    ~10,950 bars per asset (6x more)
4H 8yr:    ~17,520 bars (BTC goes back to 2013)

Usage:
    from nexus_trade.data.crypto_4h import load_4h_data

    df = load_4h_data("XBTUSD", start="2021-01-01", end="2024-12-31")
    # Returns DataFrame with open/high/low/close/volume, DatetimeIndex
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────

LAKEHOUSE_DB = os.path.expanduser(
    "~/development/agentic-quant-os/data/quant.duckdb"
)

# Alternative: direct parquet files
CRYPTO_1MIN_DIR = Path("/home/Zev/development/quant-projects/financial-data/crypto/1 min")

# Cache for resampled 4H data
CACHE_DIR = Path("/home/Zev/development/nexus-trade/data/4h_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Symbol mapping: LumiBot/trading symbols → Kraken tickers
SYMBOL_MAP = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "ADA": "ADAUSD",
    "DOT": "DOTUSD",
    "LINK": "LINKUSD",
    "LTC": "LTCUSD",
    "XRP": "XRPUSD",
    "AVAX": "AVAXUSD",
    "ATOM": "ATOMUSD",
    "BCH": "BCHUSD",
}


def load_1min_from_duckdb(
    ticker: str, start: str = None, end: str = None
) -> pd.DataFrame:
    """Load 1-minute OHLCV from the lakehouse DuckDB table.

    Args:
        ticker: Kraken ticker (e.g. 'XBTUSD')
        start: Optional start date string
        end: Optional end date string

    Returns:
        DataFrame with columns: open, high, low, close, volume.
        DatetimeIndex (UTC), tz-naive.
    """
    import duckdb

    where_clauses = [f"ticker = '{ticker}'"]
    if start:
        where_clauses.append(f"timestamp >= '{start}'")
    if end:
        where_clauses.append(f"timestamp <= '{end}'")

    query = f"""
        SELECT timestamp, open, high, low, close, volume
        FROM ohlcv_crypto_1min
        WHERE {' AND '.join(where_clauses)}
        ORDER BY timestamp
    """

    con = duckdb.connect(LAKEHOUSE_DB, read_only=True)
    df = con.execute(query).fetchdf()
    con.close()

    if df.empty:
        logger.warning(f"No 1-min data for {ticker}")
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    df.index.name = "Date"
    return df


def resample_to_4h(df_1min: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-minute OHLCV to 4-hour bars.

    Aggregation rules:
    - open: first
    - high: max
    - low: min
    - close: last
    - volume: sum
    """
    if df_1min.empty:
        return df_1min

    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }

    df_4h = df_1min.resample("4h", closed="left", label="left").agg(agg)

    # Drop bars with no data (gaps in the 1-min feed)
    df_4h = df_4h.dropna(subset=["open", "high", "low", "close"])

    # Normalize to tz-naive UTC for consistent comparisons
    if df_4h.index.tz is not None:
        df_4h.index = df_4h.index.tz_convert("UTC").tz_localize(None)

    return df_4h


def resample_to_1h(df_1min: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-minute OHLCV to 1-hour bars."""
    if df_1min.empty:
        return df_1min

    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }

    df_1h = df_1min.resample("1h", closed="left", label="left").agg(agg)
    df_1h = df_1h.dropna(subset=["open", "high", "low", "close"])
    return df_1h


def load_4h_data(
    symbol: str,
    start: str = "2021-01-01",
    end: str = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Load 4-hour OHLCV data for a crypto symbol.

    Args:
        symbol: Trading symbol (BTC, ETH, SOL, etc.)
        start: Start date string
        end: End date string (None = latest available)
        use_cache: If True, use cached parquet file

    Returns:
        DataFrame with open/high/low/close/volume columns, DatetimeIndex.
    """
    ticker = SYMBOL_MAP.get(symbol, f"{symbol}USD")

    # Check cache first
    cache_file = CACHE_DIR / f"{symbol}_4h.parquet"
    if use_cache and cache_file.exists():
        cached = pd.read_parquet(cache_file)
        # Normalize cache index to tz-naive UTC (handles older caches with LA tz)
        if hasattr(cached.index, 'tz') and cached.index.tz is not None:
            cached.index = cached.index.tz_convert("UTC").tz_localize(None)
        # Filter by date range
        mask = cached.index >= pd.Timestamp(start)
        if end:
            mask &= cached.index <= pd.Timestamp(end)
        result = cached[mask]
        if not result.empty:
            logger.info(
                f"Loaded {symbol} 4H from cache: {len(result)} bars, "
                f"{result.index[0].date()} → {result.index[-1].date()}"
            )
            return result

    # Load from lakehouse and resample
    logger.info(f"Resampling {ticker} 1-min → 4H from lakehouse...")
    df_1min = load_1min_from_duckdb(ticker, start=start, end=end)

    if df_1min.empty:
        logger.error(f"No data available for {ticker}")
        return pd.DataFrame()

    df_4h = resample_to_4h(df_1min)

    # Cache the full range for future use
    df_4h.to_parquet(cache_file)
    logger.info(
        f"{symbol} 4H: {len(df_4h)} bars, "
        f"{df_4h.index[0].date()} → {df_4h.index[-1].date()} (cached)"
    )

    return df_4h


def build_all_4h_cache(symbols: list[str] = None):
    """Pre-build 4H cache for all supported symbols.

    Call this once to populate the cache, then subsequent loads are instant.
    """
    symbols = symbols or list(SYMBOL_MAP.keys())
    results = {}

    for sym in symbols:
        try:
            df = load_4h_data(sym, start="2018-01-01", use_cache=False)
            results[sym] = {
                "bars": len(df),
                "start": str(df.index[0].date()) if not df.empty else "N/A",
                "end": str(df.index[-1].date()) if not df.empty else "N/A",
            }
            print(f"  {sym}: {len(df)} bars ({results[sym]['start']} → {results[sym]['end']})")
        except Exception as e:
            logger.error(f"  {sym}: FAILED - {e}")
            results[sym] = {"error": str(e)}

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build 4H crypto data cache")
    parser.add_argument(
        "--symbols", nargs="+", default=["BTC", "ETH", "SOL"],
        help="Symbols to process",
    )
    parser.add_argument(
        "--build-all", action="store_true",
        help="Build cache for all available symbols",
    )
    args = parser.parse_args()

    if args.build_all:
        syms = list(SYMBOL_MAP.keys())
    else:
        syms = args.symbols

    print(f"Building 4H cache for: {', '.join(syms)}")
    results = build_all_4h_cache(syms)

    total_bars = sum(r.get("bars", 0) for r in results.values())
    print(f"\nTotal: {total_bars} bars across {len(results)} symbols")
