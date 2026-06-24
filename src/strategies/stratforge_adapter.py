"""
StratForge Signal Adapter for LumiBot
======================================

Wraps any StratForge ``strategy(df, params) -> (entries, exits)`` function as a
LumiBot ``Strategy`` for crypto backtesting.

Works with both ``CcxtBacktesting`` and ``PandasDataBacktesting`` data sources.
Signals are pre-computed on the full price dataset *before* the event loop, then
replayed through LumiBot's execution engine one bar at a time. This gives us:

- Point-in-time safety (no look-ahead in execution)
- Realistic fills, commissions, and slippage from LumiBot
- The exact same signal logic as CrabQuant (same function, same params)
- Fast batch screening (signal computation is O(n), not O(n²))

Usage (PandasDataBacktesting — fastest for batch screens):

    import pandas as pd
    from lumibot.backtesting import PandasDataBacktesting
    from lumibot.entities import Asset
    from nexus_trade.strategies.stratforge_adapter import StratForgeSignalAdapter

    # 1. Load price data
    df = pd.read_parquet("strat-depot/test_data/BTC_5yr.parquet")

    # 2. Load strategy function
    import sys; sys.path.insert(0, "path/to/stratforge/active")
    from connors_rsi_starc_v2 import strategy_connors_rsi_starc_v2

    # 3. Pre-compute signals
    signal_df = StratForgeSignalAdapter.prepare_signals(
        strategy_connors_rsi_starc_v2, df, params={}
    )

    # 4. Build pandas_data dict for LumiBot
    base = Asset("BTC", "crypto")
    quote = Asset("USDT", "crypto")
    pandas_data = {(base, quote): df}

    # 5. Run backtest
    result = StratForgeSignalAdapter.backtest(
        PandasDataBacktesting,
        backtesting_start=datetime(2021, 6, 1),
        backtesting_end=datetime(2024, 12, 31),
        pandas_data=pandas_data,
        benchmark_asset=base,
        buy_trading_fees=[TradingFee(percent_fee=0.001)],
        sell_trading_fees=[TradingFee(percent_fee=0.001)],
        quote_asset=quote,
        budget=10000,
        name="sf_connors_rsi_btc",
        parameters={
            "strategy_fn": strategy_connors_rsi_starc_v2,
            "base_symbol": "BTC",
            "quote_symbol": "USDT",
            "signal_df": signal_df,
        },
    )
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from lumibot.entities import Asset, TradingFee
from lumibot.strategies import Strategy

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────

DEFAULT_POSITION_SIZE = 0.95  # 95% of portfolio per trade
DEFAULT_COMMISSION = 0.001     # 0.1% per side (Binance spot)
DEFAULT_SLIPPAGE = 0.001       # 0.1% slippage per side (crypto spread estimate)
# Total round-trip cost: (commission + slippage) × 2 sides = 0.4%


class StratForgeSignalAdapter(Strategy):
    """LumiBot Strategy that replays pre-computed StratForge signals.

    Parameters (passed via ``parameters=`` dict in ``backtest()``):
        strategy_fn:   Callable ``(df, params) -> (entries, exits)``.
        strategy_params: Dict of parameters for ``strategy_fn``.
        base_symbol:   Base asset symbol, e.g. ``"BTC"``.
        quote_symbol:  Quote asset symbol, e.g. ``"USDT"``.
        signal_df:     Pre-computed signals (from ``prepare_signals()``).
                       If ``None``, computed on first iteration from live data.
        position_size: Fraction of portfolio to allocate per entry (default 0.95).
    """

    parameters = {
        "strategy_fn": None,
        "strategy_params": {},
        "base_symbol": "BTC",
        "quote_symbol": "USDT",
        "signal_df": None,
        "position_size": DEFAULT_POSITION_SIZE,
    }

    # ── Lifecycle ───────────────────────────────────────────────────────

    def initialize(self):
        self.sleeptime = self.parameters.get("sleeptime", "1D")

        # Build asset pair
        self.base_asset = Asset(
            self.parameters["base_symbol"], Asset.AssetType.CRYPTO
        )
        self.quote_asset = Asset(
            self.parameters["quote_symbol"], Asset.AssetType.CRYPTO
        )

        # State tracking
        self._in_position = False
        self._last_bar_dt = None
        self._signal_df: pd.DataFrame | None = None  # Full signal df for lookup

        # Load pre-computed signals if available
        signal_df = self.parameters.get("signal_df")
        if signal_df is not None:
            self._load_signals(signal_df)

    def on_trading_iteration(self):
        now = self.get_datetime()
        current_ts = pd.Timestamp(now)
        # Strip timezone for comparison (signals are tz-naive UTC)
        if current_ts.tz is not None:
            current_ts = current_ts.tz_convert("UTC").tz_localize(None)
        current_ts = current_ts.floor("s")

        # Skip duplicate iterations (same bar)
        if current_ts == self._last_bar_dt:
            return
        self._last_bar_dt = current_ts

        # Look up signal by finding the nearest bar at or before current_ts
        is_entry, is_exit = self._lookup_signal(current_ts)

        # Sync position state with reality (safety check)
        pos = self.get_position(self.base_asset)
        actually_holding = pos is not None and pos.quantity > 0
        if self._in_position and not actually_holding:
            # Position was closed (stop loss, margin call, etc.)
            self._in_position = False
        elif not self._in_position and actually_holding:
            # Position opened externally
            self._in_position = True

        # ── Entry signal ──
        if is_entry and not self._in_position:
            price = self.get_last_price(self.base_asset, quote=self.quote_asset)
            if price is None or float(price) <= 0:
                logger.warning(f"No price for {self.base_asset.symbol}, skipping entry")
                return

            target_value = float(self.portfolio_value) * self.parameters["position_size"]
            qty = target_value / float(price)
            if qty > 0:
                order = self.create_order(
                    self.base_asset,
                    qty,
                    "buy",
                    quote=self.quote_asset,
                )
                self.submit_order(order)
                self._in_position = True
                logger.info(
                    f"ENTRY {self.base_asset.symbol} @ ~{price:.2f} "
                    f"qty={qty:.6f} (signal ts: {current_ts})"
                )

        # ── Exit signal ──
        elif is_exit and self._in_position:
            self.sell_all()
            self._in_position = False
            logger.info(f"EXIT {self.base_asset.symbol} (signal ts: {current_ts})")

    # ── Signal Management ───────────────────────────────────────────────

    def _load_signals(self, signal_df: pd.DataFrame) -> None:
        """Store the signal DataFrame for timestamp-based lookup.

        The signal_df has 'entry' and 'exit' columns (0/1) indexed by
        timestamp. We keep it as a DataFrame for efficient .loc lookups.
        """
        if signal_df is None or signal_df.empty:
            logger.warning("Empty signal_df — no signals loaded")
            return

        # Ensure tz-naive index (matching our backtest clock normalization)
        if hasattr(signal_df.index, 'tz') and signal_df.index.tz is not None:
            signal_df = signal_df.copy()
            signal_df.index = signal_df.index.tz_convert("UTC").tz_localize(None)

        self._signal_df = signal_df
        n_entries = int((signal_df["entry"] == 1).sum())
        n_exits = int((signal_df["exit"] == 1).sum())
        logger.info(
            f"Loaded signals: {n_entries} entries, {n_exits} exits across "
            f"{len(signal_df)} bars"
        )

    def _lookup_signal(self, current_ts: pd.Timestamp) -> tuple[bool, bool]:
        """Look up the signal for the current timestamp.

        Finds the bar at or immediately before current_ts and returns its
        entry/exit flags. This handles cases where the backtest clock
        doesn't exactly match the signal timestamps (timezone differences,
        sub-bar iterations, etc.).
        """
        if self._signal_df is None or self._signal_df.empty:
            return False, False

        # Use searchsorted to find the bar at or before current_ts
        idx = self._signal_df.index
        pos = idx.searchsorted(current_ts, side="right") - 1
        if pos < 0:
            return False, False

        row = self._signal_df.iloc[pos]
        return bool(row["entry"] == 1), bool(row["exit"] == 1)

    # ── Static Helpers ──────────────────────────────────────────────────

    @staticmethod
    def prepare_signals(
        strategy_fn: Callable,
        df: pd.DataFrame,
        params: Optional[dict] = None,
    ) -> pd.DataFrame:
        """Run a StratForge strategy function and return a signal DataFrame.

        Args:
            strategy_fn: Function with signature ``(df, params) -> (entries, exits)``.
            df: Price DataFrame with columns open/high/low/close/volume.
            params: Strategy parameters dict.

        Returns:
            DataFrame with 'entry' and 'exit' columns (0/1), same index as ``df``.
        """
        entries, exits = strategy_fn(df, params or {})

        # Ensure pd.Series with matching index
        if not isinstance(entries, pd.Series):
            entries = pd.Series(entries, index=df.index)
        if not isinstance(exits, pd.Series):
            exits = pd.Series(exits, index=df.index)

        # ── Next-bar execution: shift signals by 1 bar ──
        # A signal that fires on bar N is executed on bar N+1. This eliminates
        # the same-bar optimism bias (you can't actually fill at the exact close
        # price where the signal triggered — you'd place the order and it fills
        # on the next bar's open/close).
        entries = entries.shift(1).fillna(0).astype(int)
        exits = exits.shift(1).fillna(0).astype(int)

        signal_df = pd.DataFrame(
            {
                "entry": entries.values,
                "exit": exits.values,
            },
            index=df.index,
        )
        return signal_df

    @staticmethod
    def load_price_data(parquet_path: str) -> pd.DataFrame:
        """Load a price parquet file and normalize column names.

        StratForge strategies expect lowercase: open, high, low, close, volume.
        """
        df = pd.read_parquet(parquet_path)
        # Normalize column names
        rename_map = {}
        for col in df.columns:
            lower = col.lower()
            if lower in ("open", "high", "low", "close", "volume"):
                rename_map[col] = lower
        if rename_map:
            df = df.rename(columns=rename_map)

        # Ensure DatetimeIndex
        if not isinstance(df.index, pd.DatetimeIndex):
            # Try common index column names
            for col in ["Date", "date", "timestamp", "datetime"]:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col])
                    df = df.set_index(col)
                    break

        return df
