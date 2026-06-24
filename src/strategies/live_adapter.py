"""
Live Trading Adapter for StratForge Strategies
================================================

Extends StratForgeSignalAdapter with:
1. Live signal computation (needed for paper/live trading)
2. Risk overlays (position caps, daily loss limit, kill switch)
3. ATR-based stop-loss (optional)

In backtest mode, the parent class pre-computes signals on the full dataset
and replays them bar-by-bar. In live mode, we can't pre-compute signals for
bars that haven't happened yet. This adapter fetches recent OHLCV each
iteration and calls strategy_fn() to compute the current bar's signal.

Usage (paper trading):

    from lumibot.brokers import Ccxt
    from lumibot.entities import Asset
    from strategies.live_adapter import LiveStratForgeAdapter

    broker = Ccxt(KRAKEN_CONFIG)

    strategy = LiveStratForgeAdapter(
        broker=broker,
        parameters={
            "strategy_fn": strategy_accel_band_ppo_multi,
            "strategy_params": {},
            "base_symbol": "BTC",
            "quote_symbol": "USD",      # Kraken uses USD, not USDT
            "lookback_bars": 250,        # fetch 250 bars for indicator warmup
            "position_size": 0.40,       # max 40% of portfolio per asset
            "timestep": "4H",            # match WF validation timeframe
            # Risk overlays
            "max_daily_loss_pct": 5.0,   # halt if down 5% in 24h
            "max_drawdown_pct": 15.0,    # halt if total drawdown > 15%
            "use_atr_stop": True,        # ATR-based stop loss
            "atr_multiplier": 2.0,       # stop = entry - 2×ATR
            "atr_period": 14,
        },
    )
    strategy.run_live()
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np
import pandas as pd

from lumibot.entities import Asset, Order
from lumibot.strategies import Strategy

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────

DEFAULT_LOOKBACK = 250          # bars of history to fetch for signal computation
DEFAULT_POSITION_SIZE = 0.40    # 40% max per asset (was 0.95 — too aggressive)
DEFAULT_MAX_DAILY_LOSS = 5.0    # halt if down 5% in 24h
DEFAULT_MAX_DRAWDOWN = 15.0     # halt if total drawdown > 15%


class LiveStratForgeAdapter(Strategy):
    """Live/paper trading adapter for StratForge strategies with risk overlays.

    Each iteration:
    1. Fetch `lookback_bars` of recent OHLCV from the broker
    2. Call strategy_fn(df, params) to compute signals
    3. Check the LATEST bar's entry/exit signal
    4. Apply risk overlays (position cap, daily loss, drawdown)
    5. Execute if all checks pass

    Parameters:
        strategy_fn:     Callable (df, params) -> (entries, exits)
        strategy_params: Dict of params for strategy_fn
        base_symbol:     Base asset (e.g. "BTC")
        quote_symbol:    Quote asset (e.g. "USD" for Kraken, "USDT" for Binance)
        lookback_bars:   Bars of history to fetch (default 250)
        timestep:        Bar timestep ("1 day", "4 hour", "1 hour")
        position_size:   Max fraction of portfolio per position (default 0.40)
        max_daily_loss_pct:  Daily loss threshold for kill switch (default 5.0)
        max_drawdown_pct:    Total drawdown threshold (default 15.0)
        use_atr_stop:    Enable ATR-based stop loss (default True)
        atr_multiplier:  ATR multiplier for stop distance (default 2.0)
        atr_period:      ATR lookback period (default 14)
    """

    parameters = {
        "strategy_fn": None,
        "strategy_params": {},
        "base_symbol": "BTC",
        "quote_symbol": "USD",
        "lookback_bars": DEFAULT_LOOKBACK,
        "timestep": "4 hour",
        "position_size": DEFAULT_POSITION_SIZE,
        # Risk overlays
        "max_daily_loss_pct": DEFAULT_MAX_DAILY_LOSS,
        "max_drawdown_pct": DEFAULT_MAX_DRAWDOWN,
        "use_atr_stop": True,
        "atr_multiplier": 2.0,
        "atr_period": 14,
    }

    # ── Lifecycle ───────────────────────────────────────────────────────

    def initialize(self):
        self.sleeptime = self.parameters.get("timestep", "4 hour")

        # Build asset pair
        self.base_asset = Asset(
            self.parameters["base_symbol"], Asset.AssetType.CRYPTO
        )
        self.quote_asset = Asset(
            self.parameters["quote_symbol"], Asset.AssetType.CRYPTO
        )

        # ── Telegram notifications (opt-in) ──
        # Auto-wires if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are in env.
        # TelegramNotificationProvider self-skips when creds are missing,
        # so this is safe to call unconditionally.
        if self.parameters.get("telegram_enabled", False):
            try:
                self.notifications.configure_telegram(
                    bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
                    chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
                    parse_mode="Markdown",
                )
                logger.info(
                    "Telegram notifications enabled (chat_id=%s)",
                    os.environ.get("TELEGRAM_CHAT_ID", "(missing)"),
                )
                self.notify(
                    title="🟢 Algo paper-trade started",
                    message=(
                        f"Asset: {self.base_asset.symbol}/{self.quote_asset.symbol}\n"
                        f"Sleeptime: {self.sleeptime}\n"
                        f"Position size: "
                        f"{self.parameters['position_size']:.0%}\n"
                        f"Max daily loss: "
                        f"{self.parameters['max_daily_loss_pct']}%\n"
                        f"Max drawdown: "
                        f"{self.parameters['max_drawdown_pct']}%"
                    ),
                    severity="info",
                )
            except Exception as exc:
                logger.warning("Telegram setup skipped: %s", exc)

        # State tracking
        self._in_position = False
        self._entry_price = 0.0
        self._stop_price = 0.0
        self._last_bar_time = None

        # Risk state
        self._peak_value = 0.0
        self._daily_start_value = 0.0
        self._last_daily_reset = None
        self._halted = False
        self._halt_reason = ""

        # Track initial portfolio value for drawdown calc
        self._initial_value = float(self.portfolio_value) if self.portfolio_value else 10000.0
        self._peak_value = self._initial_value

        logger.info(
            f"LiveStratForgeAdapter initialized: "
            f"{self.base_asset.symbol}/{self.quote_asset.symbol} "
            f"timestep={self.sleeptime} "
            f"pos_size={self.parameters['position_size']} "
            f"max_daily_loss={self.parameters['max_daily_loss_pct']}% "
            f"max_dd={self.parameters['max_drawdown_pct']}%"
        )

    # ── Main Trading Loop ──────────────────────────────────────────────

    def on_trading_iteration(self):
        now = self.get_datetime()

        # ── Reset daily tracking at start of each UTC day ──
        today = now.date() if now else datetime.utcnow().date()
        if self._last_daily_reset != today:
            self._daily_start_value = float(self.portfolio_value)
            self._last_daily_reset = today
            if self._halted:
                # Reset halt at start of new day (give it another chance)
                self._halted = False
                self._halt_reason = ""
                logger.info(f"Kill switch reset for new day {today}")

        # ── Check kill switches first ──
        if self._check_kill_switches():
            return

        # ── Update peak value for drawdown tracking ──
        current_value = float(self.portfolio_value)
        if current_value > self._peak_value:
            self._peak_value = current_value

        # ── Sync position state ──
        pos = self.get_position(self.base_asset)
        actually_holding = pos is not None and pos.quantity > 0
        if self._in_position and not actually_holding:
            # Position was closed (stop loss hit)
            self._in_position = False
            logger.info(f"Position closed externally (likely stop loss hit)")
        elif not self._in_position and actually_holding:
            self._in_position = True

        # ── Check stop loss if in position ──
        if self._in_position and self.parameters.get("use_atr_stop"):
            if self._check_stop_loss():
                return

        # ── Fetch recent OHLCV and compute live signal ──
        df = self._fetch_recent_data()
        if df is None or len(df) < 50:
            logger.warning(
                f"Insufficient data for signal computation ({len(df) if df is not None else 0} bars)"
            )
            return

        # Compute signals using the strategy function
        try:
            entries, exits = self._compute_live_signal(df)
        except Exception as e:
            logger.error(f"Signal computation failed: {e}")
            return

        # Only look at the LATEST bar's signal
        latest_entry = bool(entries.iloc[-1]) if len(entries) > 0 else False
        latest_exit = bool(exits.iloc[-1]) if len(exits) > 0 else False

        # ── Execute ──
        if latest_entry and not self._in_position:
            self._execute_entry(df)
        elif latest_exit and self._in_position:
            self._execute_exit()
        elif latest_entry and self._in_position:
            # Already in position — ignore duplicate entry signal
            pass

    def on_abrupt_closing(self):
        """Called if the bot crashes or is force-stopped."""
        logger.warning("Abrupt closing — flattening position if any")
        if self._in_position:
            try:
                self.sell_all()
            except Exception:
                pass

    # ── Signal Computation ──────────────────────────────────────────────

    def _fetch_recent_data(self) -> Optional[pd.DataFrame]:
        """Fetch recent OHLCV bars from the broker."""
        lookback = self.parameters.get("lookback_bars", DEFAULT_LOOKBACK)
        try:
            bars = self.get_historical_prices(
                self.base_asset,
                self.quote_asset,
                lookback,
                timestep=self.sleeptime,
            )
            if bars is None or len(bars) == 0:
                return None

            # Convert to DataFrame
            df = bars.df if hasattr(bars, "df") else pd.DataFrame(bars)

            # Normalize column names to lowercase
            rename = {
                c: c.lower()
                for c in df.columns
                if c.lower() in ("open", "high", "low", "close", "volume")
            }
            if rename:
                df = df.rename(columns=rename)

            return df

        except Exception as e:
            logger.error(f"Failed to fetch historical prices: {e}")
            return None

    def _compute_live_signal(self, df: pd.DataFrame) -> tuple:
        """Run the strategy function on the current data window."""
        strategy_fn = self.parameters.get("strategy_fn")
        if strategy_fn is None:
            raise ValueError("strategy_fn not set")

        params = self.parameters.get("strategy_params", {})
        entries, exits = strategy_fn(df, params)

        if not isinstance(entries, pd.Series):
            entries = pd.Series(entries, index=df.index)
        if not isinstance(exits, pd.Series):
            exits = pd.Series(exits, index=df.index)

        return entries, exits

    # ── Order Execution ─────────────────────────────────────────────────

    def _execute_entry(self, df: pd.DataFrame) -> None:
        """Execute a buy order with risk checks."""
        price = self.get_last_price(self.base_asset, quote=self.quote_asset)
        if price is None or float(price) <= 0:
            logger.warning(f"No price for {self.base_asset.symbol}, skipping entry")
            return

        # Cap position size
        max_size = self.parameters.get("position_size", DEFAULT_POSITION_SIZE)
        target_value = float(self.portfolio_value) * max_size
        qty = target_value / float(price)

        if qty <= 0:
            return

        # Calculate ATR-based stop loss
        stop_price = 0.0
        if self.parameters.get("use_atr_stop", True):
            stop_price = self._compute_atr_stop(df, float(price))

        order = self.create_order(
            self.base_asset,
            qty,
            "buy",
            quote=self.quote_asset,
        )
        self.submit_order(order)
        self._in_position = True
        self._entry_price = float(price)
        self._stop_price = stop_price

        stop_str = f" stop={stop_price:.2f}" if stop_price > 0 else ""
        logger.info(
            f"ENTRY {self.base_asset.symbol} @ ~{price:.2f} "
            f"qty={qty:.6f} ({max_size:.0%} of portfolio){stop_str}"
        )
        # Telegram notification on entry
        if self.parameters.get("telegram_enabled", False):
            try:
                self.notify(
                    title=f"🟢 BUY {self.base_asset.symbol}",
                    message=(
                        f"Price: ~{price:.2f} {self.quote_asset.symbol}\n"
                        f"Qty: {qty:.6f} ({max_size:.0%} of portfolio)\n"
                        f"Value: ~{target_value:.2f} {self.quote_asset.symbol}"
                        f"{stop_str}"
                    ),
                    severity="info",
                )
            except Exception as exc:
                logger.debug("Telegram ENTRY notify failed: %s", exc)

    def _execute_exit(self) -> None:
        """Execute a sell order."""
        self.sell_all()
        self._in_position = False
        pnl = 0.0
        if self._entry_price > 0:
            current = float(self.get_last_price(self.base_asset, quote=self.quote_asset) or 0)
            if current > 0:
                pnl = (current - self._entry_price) / self._entry_price * 100
        logger.info(
            f"EXIT {self.base_asset.symbol} "
            f"(entry={self._entry_price:.2f}, pnl={pnl:+.2f}%)"
        )
        # Telegram notification on exit
        if self.parameters.get("telegram_enabled", False):
            try:
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                self.notify(
                    title=f"{pnl_emoji} SELL {self.base_asset.symbol} (PnL {pnl:+.2f}%)",
                    message=(
                        f"Entry: {self._entry_price:.2f}\n"
                        f"PnL: {pnl:+.2f}%"
                    ),
                    severity="info" if pnl >= 0 else "warning",
                )
            except Exception as exc:
                logger.debug("Telegram EXIT notify failed: %s", exc)
        self._entry_price = 0.0
        self._stop_price = 0.0

    # ── Risk Management ─────────────────────────────────────────────────

    def _check_kill_switches(self) -> bool:
        """Check daily loss and drawdown kill switches. Returns True if halted."""
        if self._halted:
            return True

        current_value = float(self.portfolio_value)

        # ── Daily loss check ──
        if self._daily_start_value > 0:
            daily_pnl_pct = (
                (current_value - self._daily_start_value) / self._daily_start_value * 100
            )
            max_daily = self.parameters.get("max_daily_loss_pct", DEFAULT_MAX_DAILY_LOSS)
            if daily_pnl_pct <= -max_daily:
                self._halted = True
                self._halt_reason = f"Daily loss {daily_pnl_pct:.2f}% exceeded -{max_daily}%"
                logger.warning(f"🛑 KILL SWITCH: {self._halt_reason}")
                if self._in_position:
                    self._execute_exit()
                return True

        # ── Total drawdown check ──
        if self._peak_value > 0:
            drawdown_pct = (self._peak_value - current_value) / self._peak_value * 100
            max_dd = self.parameters.get("max_drawdown_pct", DEFAULT_MAX_DRAWDOWN)
            if drawdown_pct >= max_dd:
                self._halted = True
                self._halt_reason = f"Drawdown {drawdown_pct:.2f}% exceeded {max_dd}%"
                logger.warning(f"🛑 KILL SWITCH: {self._halt_reason}")
                if self._in_position:
                    self._execute_exit()
                return True

        return False

    def _check_stop_loss(self) -> bool:
        """Check if ATR stop loss has been hit. Returns True if stopped out."""
        if self._stop_price <= 0:
            return False

        current = float(self.get_last_price(self.base_asset, quote=self.quote_asset) or 0)
        if current <= 0:
            return False

        if current <= self._stop_price:
            logger.warning(
                f"⛔ STOP LOSS HIT: {self.base_asset.symbol} @ {current:.2f} "
                f"(stop={self._stop_price:.2f}, entry={self._entry_price:.2f})"
            )
            self._execute_exit()
            return True

        return False

    def _compute_atr_stop(self, df: pd.DataFrame, entry_price: float) -> float:
        """Compute ATR-based stop loss price."""
        try:
            period = self.parameters.get("atr_period", 14)
            multiplier = self.parameters.get("atr_multiplier", 2.0)

            high = df["high"].values
            low = df["low"].values
            close = df["close"].values

            # Simple ATR calculation
            tr = np.zeros(len(df))
            tr[0] = high[0] - low[0]
            for i in range(1, len(df)):
                tr[i] = max(
                    high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]),
                )

            # Simple moving average of TR
            if len(tr) >= period:
                atr = np.mean(tr[-period:])
            else:
                atr = np.mean(tr)

            stop_distance = atr * multiplier
            stop_price = entry_price - stop_distance

            logger.debug(
                f"ATR stop: entry={entry_price:.2f} ATR={atr:.2f} "
                f"distance={stop_distance:.2f} stop={stop_price:.2f}"
            )
            return max(stop_price, 0.0)

        except Exception as e:
            logger.warning(f"ATR computation failed, no stop: {e}")
            return 0.0

    # ── Status Reporting ────────────────────────────────────────────────

    def on_stats_record(self):
        """Called periodically for stats recording."""
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
            "stop_price": self._stop_price,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "peak_value": self._peak_value,
            "daily_start": self._daily_start_value,
        }
