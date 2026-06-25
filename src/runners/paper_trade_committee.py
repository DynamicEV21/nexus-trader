"""
Paper Trading Launcher — LLM Investment Committee
==================================================

Runs the full NexusCommitteeStrategy (4 LLM agents: evidence_researcher,
bull_researcher, bear_researcher, portfolio_manager) in paper-trading mode
against Binance Testnet via the LumiBot CCXT broker.

This is the live counterpart to committee_smoke.py (which is the backtest).
Both share NexusCommitteeStrategy as the underlying class — only the data
source + broker wiring differ.

Prerequisites:
1. Binance Testnet keys in .env (BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET)
2. A model API key in lumibot venv (currently uses minimax/MiniMax-M3)
3. LumiBot installed and importable

Usage:
    cd /home/Zev/development/trading-bots/lumibot
    source venv/bin/activate

    # Paper trade BTC with the committee
    python /home/Zev/development/nexus-trade/src/runners/paper_trade_committee.py \
        --asset BTC

    # Custom universe + position cap
    python /home/Zev/development/nexus-trade/src/runners/paper_trade_committee.py \
        --asset BTC,ETH,SOL --max-position-pct 0.20

    # One-shot mode (run once and exit, useful for cron/scheduled-execution)
    LUMIBOT_SCHEDULED_EXECUTION=1 \
        python /home/Zev/development/nexus-trade/src/runners/paper_trade_committee.py \
            --asset BTC

The committee runs every ``self.sleeptime`` interval (default 1D). On each
iteration:
  1. evidence_researcher gathers regime + signals + memory
  2. bull_researcher builds the long case
  3. bear_researcher attacks the long case
  4. portfolio_manager decides and (if allow_trading=True) submits orders
  5. Decision is written to AQS nexus for cross-run analytics

Telegram notifications fire on:
  - Strategy init / shutdown
  - Each committee run start/finish
  - Order fills (filled, partial_fill, canceled)
  - Risk halts (max_daily_loss, max_drawdown)

Telegram notes (2026-06-25 audit):
- Telegram messages are sent in PLAIN TEXT (parse_mode=None) because agent
  output routinely contains underscores (mean_reversion, BTC_USDT,
  meta_cmo_alma_atr_wf_v1, etc.). Telegram's MarkdownV1 parser treats `_` as
  an italic delimiter and rejects unclosed underscores with HTTP 400 — every
  committee summary would fail. Plain text is safer and still readable.
- ``self.notify()`` returns a ``list[NotificationResult]`` (one per
  provider), NOT a single result. We use ``_send_notify()`` to iterate the
  list and log any failures — the previous ``hasattr(result, 'ok')`` check
  was a silent no-op because lists have no ``.ok`` attribute.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ─── Path Setup ────────────────────────────────────────────────────────

LUMIBOT_ROOT = Path("/home/Zev/development/trading-bots/lumibot")
NEXUS_ROOT = Path("/home/Zev/development/nexus-trade")

# LumiBot first (so lumibot.components wins over any local conflict).
# Insert NEXUS_ROOT (the parent of the top-level `src` package), not
# NEXUS_ROOT/src — Python needs the package's parent on sys.path so that
# `import src.strategies.nexus_committee` resolves.
sys.path.insert(0, str(LUMIBOT_ROOT))
sys.path.insert(0, str(NEXUS_ROOT))

from dotenv import load_dotenv

# Load nexus-trade .env first (Binance testnet keys), then lumibot .env for
# model keys (don't override nexus values).
load_dotenv(str(NEXUS_ROOT / ".env"))
load_dotenv(str(LUMIBOT_ROOT / ".env"), override=False)

# ─── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("paper_trade_committee")


# ─── Broker Config ─────────────────────────────────────────────────────


def build_ccxt_config(exchange: str = "binance") -> dict:
    """Build CCXT broker config for Binance Testnet (paper trading).

    The ``sandbox=True`` flag is what makes this paper-trading: CCXT routes
    requests to testnet.binance.vision instead of api.binance.com, and all
    orders/balances are in test funds only.
    """
    if exchange != "binance":
        raise ValueError(
            f"Exchange '{exchange}' not supported — committee paper-trade "
            "currently only ships with Binance Testnet"
        )
    return {
        "exchange_id": "binance",
        "apiKey": os.environ.get("BINANCE_TESTNET_KEY", ""),
        "secret": os.environ.get("BINANCE_TESTNET_SECRET", ""),
        "margin": False,
        "sandbox": True,
        # Fix for -1021 timestamp errors (clock skew +390ms observed).
        "options": {"adjustForTimeDifference": True},
    }


def check_credentials(config: dict) -> bool:
    if not config.get("apiKey") or not config.get("secret"):
        logger.error(
            "❌ Missing BINANCE_TESTNET_KEY/SECRET in %s/.env",
            NEXUS_ROOT,
        )
        return False
    return True


# ─── Committee Strategy Class ──────────────────────────────────────────


def make_committee_strategy_class():
    """Build the committee strategy class with paper-trading defaults.

    We subclass NexusCommitteeStrategy so we can:
      1. Set parameters programmatically (CLI args)
      2. Wire Telegram notifications before initialize() runs
      3. Add paper-trading-specific lifecycle hooks (startup/shutdown logs)

    Returns the class — caller instantiates with broker=broker.
    """
    from src.strategies.nexus_committee import NexusCommitteeStrategy

    def _send_notify(strategy, *, title, message, severity="info", parse_mode=None):
        """Send a Telegram notification and LOG any failures.

        ``strategy.notify()`` returns a ``list[NotificationResult]`` (one per
        configured provider). The previous implementation used
        ``hasattr(result, 'ok') and not result.ok`` to check for failures,
        which silently no-op'd because lists have no ``.ok`` attribute. This
        helper iterates the list and logs every failure with the actual
        message_id (on success) or reason (on failure).

        ``parse_mode`` is ONLY forwarded to the provider when it's a non-empty
        string. Passing ``parse_mode=None`` causes LumiBot's
        TelegramNotificationProvider to include ``\"parse_mode\": null`` in
        the JSON payload, which Telegram rejects with HTTP 400. So when
        callers want plain text (no parse_mode), we omit the kwarg entirely.
        """
        kwargs = {}
        if parse_mode:  # truthy check: only forward when non-empty string
            kwargs["parse_mode"] = parse_mode
        try:
            results = strategy.notify(
                title=title,
                message=message,
                severity=severity,
                **kwargs,
            )
        except Exception as exc:
            logger.warning("Notify raised for %r: %s", title, exc)
            return
        # results is list[NotificationResult] (possibly empty)
        if not results:
            logger.warning("Notify returned no results for %r (no providers?)", title)
            return
        for r in results:
            if r.ok:
                mid = None
                if r.payload and isinstance(r.payload, dict):
                    mid = r.payload.get("result", {}).get("message_id")
                logger.info(
                    "Telegram sent: %r (provider=%s, message_id=%s)",
                    title, r.provider, mid,
                )
            else:
                logger.warning(
                    "Telegram FAILED: %r (provider=%s, skipped=%s, reason=%s)",
                    title, r.provider, r.skipped, r.reason,
                )

    class PaperTradeCommitteeStrategy(NexusCommitteeStrategy):
        parameters = {
            # CLI-overridable (see __init__ kwargs)
            "universe": ["BTC"],
            "max_position_pct": 0.25,
            "max_new_positions_per_run": 1,
            "enable_notifications": True,
            "use_memory_bridge": True,
            "lakehouse_enabled": True,
            # History: 6→10 (Tristan 2026-06-23); 10→30 (full toolset
            # can run >10 LLM calls per agent; comfortable for 5 iterations).
            "agent_max_model_calls": 30,
            # B1 attribution (2026-06-25): auto-write a lesson to LanceDB
            # when a closed position lost more than 2% of position value.
            "attribution_loss_threshold_pct": 2.0,
            # Whether to invoke the subprocess bridge on every fill (heavy)
            # or only on closes above the loss threshold (light). Default
            # "light" — bridge is expensive (Qwen3-Embedding on full JSONL).
            "attribution_bridge_mode": "light",
        }
        # Run immediately on startup rather than waiting an hour for the
        # first cron tick. Without this, the first committee run is delayed
        # by up to one hour after launch (because LumiBot's cron fires every
        # hour with cron_count_target=4 for 4H sleeptime).
        force_start_immediately = True

        # ── B1 attribution state ───────────────────────────────────────
        # Lumibot 4.5.53 has NO on_position_closed hook, so we synthesize
        # one in on_filled_order by diffing pre- and post-fill quantities
        # for each asset in our universe. ``_pre_fill_qty`` records the
        # position state BEFORE the fill arrives; ``on_filled_order`` reads
        # it to detect transitions (open / partial close / full close).
        _pre_fill_qty: dict[str, float] = {}
        _pending_outcomes: dict[str, dict] = {}  # symbol → open-trade record
        _fills_processed: int = 0
        _closes_processed: int = 0

        def initialize(self) -> None:
            """LumiBot lifecycle hook — runs once at strategy start.

            Delegates to the parent (which sets up agents + tools) then
            configures Telegram notifications if creds are present.
            """
            super().initialize()

            # Honor 4H bar like the algo paper trader (rather than the
            # default 1D set by NexusCommitteeStrategy.initialize) so the
            # committee and algo runners evaluate at the same time. We
            # MUST set this AFTER super().initialize() because the parent
            # overrides sleeptime to "1D" on every call.
            self.sleeptime = "4H"

            # ── Telegram notifications ──────────────────────────────
            # Wire the built-in LumiBot Telegram provider if creds exist.
            # It self-skips if TELEGRAM_BOT_TOKEN/CHAT_ID are missing.
            #
            # parse_mode=None (plain text) is intentional. Telegram's
            # MarkdownV1 parser treats `_` as an italic delimiter and
            # rejects messages with unclosed underscores (HTTP 400).
            # Agent output routinely contains underscores (mean_reversion,
            # BTC_USDT, strategy names, etc.) — using Markdown there
            # would silently fail every notify. Plain text is safer and
            # still readable. Emoji in titles still render correctly.
            #
            # IMPORTANT: NexusCommitteeStrategy.initialize() (called via
            # super() above) also calls configure_telegram() with no args
            # if enable_notifications=True. That would leave us with TWO
            # Telegram providers → every notify fires twice. Clear the
            # list first, then add ours.
            self.notifications.providers.clear()
            try:
                self.notifications.configure_telegram(
                    bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
                    chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
                    parse_mode=None,
                )
                logger.info(
                    "Telegram notifications configured (chat_id=%s, plain_text)",
                    os.environ.get("TELEGRAM_CHAT_ID", "(missing)"),
                )
            except Exception as exc:
                logger.warning("Telegram setup skipped: %s", exc)

            # Startup notification
            _send_notify(
                self,
                title="🟢 Committee paper-trade started",
                message=(
                    f"Asset: {self.parameters.get('universe')}\n"
                    f"Sleeptime: {self.sleeptime}\n"
                    f"Max position: "
                    f"{self.parameters.get('max_position_pct', 0):.0%}\n"
                    f"Started: {datetime.now().isoformat()}"
                ),
                severity="info",
            )

        def on_trading_iteration(self) -> None:
            """LumiBot lifecycle hook — runs every self.sleeptime.

            Wraps the parent's full committee flow with Telegram
            start/end notifications so you get a push per bar.
            """
            # B1 attribution: snapshot positions BEFORE the committee runs
            # so the next on_filled_order has accurate pre-fill quantities
            # even if the strategy fills an order during on_trading_iteration.
            self._capture_pre_fill_state()

            run = (self.vars.committee_run_count or 0) + 1
            _send_notify(
                self,
                title=f"📊 Committee run {run} starting",
                message=(
                    f"Time: {self.get_datetime().isoformat()}\n"
                    f"Universe: {self.parameters.get('universe')}"
                ),
                severity="info",
            )

            try:
                super().on_trading_iteration()
            except Exception as exc:
                logger.exception("Committee run %d failed", run)
                # Plain text — Markdown escaping is unreliable for dynamic
                # agent output and tracebacks (contains _, *, [, ], etc.).
                _send_notify(
                    self,
                    title=f"❌ Committee run {run} FAILED",
                    message=str(exc)[:800],
                    severity="error",
                )
                raise

            # ── Build a useful summary from the parent's state vars ──
            # The parent sets several self.vars.* fields during the run;
            # we surface the most actionable ones in the notification.
            regime = getattr(self.vars, "current_regime", "unknown") or "unknown"
            decision = getattr(self.vars, "last_decision_action", "") or ""
            confidence = getattr(self.vars, "last_decision_confidence", "")
            summary = (
                f"Action: {decision or 'HOLD'}\n"
                f"Regime: {regime}\n"
                f"Confidence: {confidence}\n"
                f"Run: {run}\n"
                f"Time: {datetime.now().isoformat()}"
            )
            _send_notify(
                self,
                title=f"✅ Committee run {run} complete — {decision or 'HOLD'}",
                message=summary,
                severity="info",
            )

        def on_finish(self, *args, **kwargs) -> None:
            """LumiBot lifecycle hook — runs at strategy shutdown."""
            _send_notify(
                self,
                title="🔴 Committee paper-trade stopped",
                message=f"Finished: {datetime.now().isoformat()}",
                severity="warning",
            )
            super().on_finish(*args, **kwargs)

        # ──────────────────────────────────────────────────────────────
        # B1 — Attribution loop (2026-06-25)
        #
        # Lumibot 4.5.53 has NO ``on_position_closed`` lifecycle hook
        # (verified by grepping lumibot/strategies/strategy.py). The
        # closest hook is ``on_filled_order``, which fires on every
        # filled order — entry, exit, and partial. We synthesize position
        # closes by diffing pre- and post-fill quantities for each asset.
        #
        # Three cases we detect:
        #   1. qty_pre == 0 → qty_post > 0   : OPEN (new position)
        #   2. qty_pre >  0 → qty_post == 0  : CLOSE (full exit)  ← attr write
        #   3. qty_pre >  0 → qty_post < qty_pre: PARTIAL CLOSE   ← attr write
        #   4. qty_pre >  0 → qty_post > qty_pre: ADD / RE-ENTRY   (skip)
        #
        # On any close (case 2 or 3), we:
        #   - Update LanceDB ``nexus_decisions`` outcome field (via subprocess
        #     bridge — same pattern as ``_run_memory_bridge``).
        #   - If realized loss > ``attribution_loss_threshold_pct``, auto-write
        #     a lesson to the ecosystem experience bank so future committees
        #     can avoid repeating the mistake.
        #
        # The subprocess bridge is heavy (Qwen3-Embedding + LanceDB); to keep
        # the on_fill hot path fast we defer it via ``attribution_bridge_mode``
        # — "light" (default) only bridges on threshold-crossing losses,
        # "heavy" bridges on every close.
        # ──────────────────────────────────────────────────────────────

        def _capture_pre_fill_state(self) -> None:
            """Snapshot current positions BEFORE the next fill arrives.

            Called from ``on_filled_order`` (with best-effort lookup of the
            pre-state via the broker's already-cached positions) and from
            the main iteration loop.  We use a best-effort dict snapshot —
            ``self.get_position(symbol)`` could be slow on ccxt broker.
            """
            try:
                positions = self.get_positions()
                snap: dict[str, float] = {}
                for p in positions:
                    sym = getattr(p, "symbol", None)
                    if sym is None and hasattr(p, "asset"):
                        sym = getattr(p.asset, "symbol", None)
                    if sym is None:
                        continue
                    snap[str(sym).upper()] = float(getattr(p, "quantity", 0) or 0)
                self._pre_fill_qty = snap
            except Exception as exc:
                logger.debug("Failed to capture pre-fill state: %s", exc)

        def _attribution_bridge_call(
            self,
            decision_id: str,
            outcome: str,
            pnl_pct: float,
            symbol: str,
        ) -> None:
            """Update LanceDB outcome for *decision_id* via AQOS subprocess.

            The lumibot venv does NOT have lancedb / sentence_transformers.
            We invoke the AQOS venv (which has them) through a tiny
            subprocess script that calls the existing
            ``src.memory.bridge:update_decision_outcome()`` function once
            we add it there. Until that helper lands, the bridge CLI is a
            no-op for outcome updates — the local JSONL ``decisions.jsonl``
            is the source of truth and will be re-bridged on next sync.
            """
            try:
                from src.runners.attribution_bridge import update_outcome_via_subprocess
                update_outcome_via_subprocess(
                    decision_id=decision_id,
                    outcome=outcome,
                    pnl_pct=pnl_pct,
                    symbol=symbol,
                    strategy_name=self.__class__.__name__,
                )
            except Exception as exc:
                logger.debug("Outcome bridge call failed (non-fatal): %s", exc)

        def _attribution_write_loss_lesson(
            self,
            *,
            symbol: str,
            side: str,
            entry_price: float,
            exit_price: float,
            realized_pnl_pct: float,
            bars_held: int,
            regime: str,
            decision_id: str,
        ) -> None:
            """Auto-write a 'mistake' lesson when a closed position lost > threshold.

            Two writes:
              1. Subprocess → LanceDB ``lessons`` table (via AQOS bridge so
                 future ``query_trade_memory`` calls surface the lesson).
              2. In-process → ``remember_lesson_tool`` so the in-memory
                 tool registry sees it this run (no embedding needed).
            """
            threshold = float(
                self.parameters.get("attribution_loss_threshold_pct", 2.0)
            )
            if abs(realized_pnl_pct) < threshold:
                return

            # Truncate each side; a verbose traceback isn't actionable.
            side_short = "long" if side.upper() == "LONG" else "short"
            loss_text = (
                f"Auto-loss on {symbol}: closed {side_short} after {bars_held} bars "
                f"with realized PnL={realized_pnl_pct:+.2f}% (entry=${entry_price:.2f}, "
                f"exit=${exit_price:.2f}, regime={regime}). "
                f"Decision id={decision_id}. Threshold was {threshold:.1f}%. "
                f"Lesson: review what triggered entry — likely a stale or weak thesis."
            )

            # ── 1. Subprocess bridge to LanceDB ──
            try:
                from src.runners.attribution_bridge import write_lesson_via_subprocess
                write_lesson_via_subprocess(
                    text=loss_text,
                    category="mistake",
                    severity="warning",
                    symbol=symbol,
                    regime=regime,
                    tags=["attribution", "auto-loss", side_short],
                )
                logger.info(
                    "Attribution: wrote loss lesson for %s (pnl=%.2f%%)",
                    symbol, realized_pnl_pct,
                )
            except Exception as exc:
                logger.debug("Attribution lesson bridge failed: %s", exc)

            # ── 2. In-process remember_lesson (best-effort, fails silent) ──
            try:
                from src.tools.trade_memory_tool import remember_lesson_tool
                remember_lesson_tool(
                    text=loss_text[:1500],
                    category="mistake",
                    severity="warning",
                    symbol=symbol,
                    regime=regime,
                    tags='["attribution", "auto-loss", "' + side_short + '"]',
                )
            except Exception as exc:
                logger.debug("In-process remember_lesson failed: %s", exc)

        def on_filled_order(
            self,
            position,
            order,
            price: float,
            quantity: float,
            multiplier: float,
        ) -> None:
            """LumiBot lifecycle hook — runs when an order is filled.

            Synthesizes position-close detection (Lumibot 4.5.53 has no
            ``on_position_closed`` hook). The diff is pre-fill vs post-fill
            quantity for the order's asset symbol. See module docstring of
            ``_attribution_*`` helpers for the full logic.
            """
            try:
                # ── Extract order metadata ──
                order_sym = ""
                try:
                    if order is not None and getattr(order, "asset", None) is not None:
                        order_sym = str(getattr(order.asset, "symbol", "") or "")
                except Exception:
                    order_sym = ""
                if not order_sym:
                    order_sym = (
                        str(getattr(position, "symbol", "") or "")
                        if position is not None
                        else ""
                    )
                order_sym = order_sym.upper()
                is_buy = bool(order and getattr(order, "is_buy_order", lambda: False)())

                # ── Capture PRE-fill quantity ──
                # We don't always have it (first fill of the day, restart).
                # Default to 0 so an open on the first fill works correctly.
                pre_qty = float(self._pre_fill_qty.get(order_sym, 0.0))

                # ── POST-fill quantity: prefer the Position object passed
                #    in by Lumibot (already has the post-fill quantity).
                post_qty = float(getattr(position, "quantity", 0.0) or 0.0)
                if position is None:
                    # Fallback: query the broker directly.
                    try:
                        post_pos = self.get_position(order_sym)
                        post_qty = float(getattr(post_pos, "quantity", 0.0) or 0.0)
                    except Exception:
                        pass

                # Refresh the cache so the NEXT on_filled_order sees the
                # post-state for this asset.
                self._pre_fill_qty[order_sym] = post_qty
                self._fills_processed += 1

                side = "LONG" if is_buy else "SHORT"
                bars_held = 0
                entry_price = 0.0

                # ── Decide: open / close / partial / add ──
                if pre_qty == 0.0 and post_qty > 0.0:
                    # OPEN — record entry context for later outcome computation
                    # Pull the decision_id for this symbol from the
                    # per-symbol mapping populated by the AQS sync path.
                    ids = getattr(self.vars, "last_decision_ids_by_symbol", {}) or {}
                    sym_decision_id = ids.get(order_sym, "") or getattr(self.vars, "last_decision_id", "") or ""
                    self._pending_outcomes[order_sym] = {
                        "side": side,
                        "entry_price": float(price or 0.0),
                        "entry_sim_time": (
                            self.get_datetime().isoformat()
                            if hasattr(self, "get_datetime")
                            else ""
                        ),
                        "decision_id": sym_decision_id,
                    }
                    logger.info(
                        "[B1] Position OPEN: %s side=%s qty=%.6f price=%.2f",
                        order_sym, side, post_qty, price,
                    )
                    return

                if pre_qty > 0.0 and post_qty == 0.0:
                    # FULL CLOSE — compute realized PnL and update LanceDB
                    pending = self._pending_outcomes.pop(
                        order_sym,
                        {"side": side, "entry_price": 0.0, "entry_sim_time": "", "decision_id": ""},
                    )
                    entry_price = float(pending.get("entry_price", 0.0) or 0.0)
                    side = pending.get("side", side)
                    # If pending is the default empty dict (no entry recorded
                    # this run — e.g., position inherited at startup), try
                    # the per-symbol decision_ids_by_symbol mapping AND fall
                    # back to the broker's ``avg_fill_price`` for entry.
                    if not pending.get("decision_id"):
                        ids = getattr(self.vars, "last_decision_ids_by_symbol", {}) or {}
                        pending["decision_id"] = (
                            ids.get(order_sym, "")
                            or getattr(self.vars, "last_decision_id", "")
                            or ""
                        )
                    if entry_price <= 0.0:
                        # Try the position's avg_fill_price as a fallback
                        # (Lumibot stores the open cost basis on the position).
                        try:
                            fallback_entry = float(getattr(position, "avg_fill_price", 0.0) or 0.0)
                            if fallback_entry > 0.0:
                                entry_price = fallback_entry
                                # Inherited position: if pending_outcomes
                                # doesn't have a side, infer from the
                                # pre-fill quantity sign — positive means
                                # we were LONG (selling closes), negative
                                # means SHORT (buying covers).
                                if pre_qty > 0.0 and not pending.get("side_was_set"):
                                    side = "LONG"
                                elif pre_qty < 0.0:
                                    side = "SHORT"
                        except Exception:
                            pass
                    # Long close: (exit - entry)/entry; short close: (entry - exit)/entry
                    if entry_price > 0:
                        if side == "LONG":
                            realized_pnl_pct = (float(price) - entry_price) / entry_price * 100.0
                        else:
                            realized_pnl_pct = (entry_price - float(price)) / entry_price * 100.0
                    else:
                        realized_pnl_pct = 0.0

                    self._closes_processed += 1
                    outcome = "win" if realized_pnl_pct > 0 else "loss"
                    logger.info(
                        "[B1] Position CLOSE: %s side=%s entry=%.2f exit=%.2f pnl=%.2f%% outcome=%s",
                        order_sym, side, entry_price, float(price), realized_pnl_pct, outcome,
                    )

                    # Update LanceDB outcome + maybe auto-write loss lesson
                    decision_id = pending.get("decision_id", "")
                    if decision_id:
                        bridge_mode = self.parameters.get(
                            "attribution_bridge_mode", "light",
                        )
                        # "light" → bridge only on losses > threshold;
                        # "heavy" → bridge every close.
                        threshold = float(
                            self.parameters.get("attribution_loss_threshold_pct", 2.0)
                        )
                        should_bridge = (
                            bridge_mode == "heavy"
                            or (outcome == "loss" and abs(realized_pnl_pct) > threshold)
                        )
                        if should_bridge:
                            self._attribution_bridge_call(
                                decision_id=decision_id,
                                outcome=outcome,
                                pnl_pct=realized_pnl_pct,
                                symbol=order_sym,
                            )

                    # Auto-write lesson on threshold-crossing loss
                    if outcome == "loss" and abs(realized_pnl_pct) > float(
                        self.parameters.get("attribution_loss_threshold_pct", 2.0)
                    ):
                        regime = getattr(self.vars, "current_regime", "unknown") or "unknown"
                        self._attribution_write_loss_lesson(
                            symbol=order_sym,
                            side=side,
                            entry_price=entry_price,
                            exit_price=float(price),
                            realized_pnl_pct=realized_pnl_pct,
                            bars_held=bars_held,
                            regime=regime,
                            decision_id=decision_id,
                        )
                    return

                if pre_qty > 0.0 and 0.0 < post_qty < pre_qty:
                    # PARTIAL CLOSE — treat as a CLOSE for the closed slice
                    pending = self._pending_outcomes.get(
                        order_sym,
                        {"side": side, "entry_price": 0.0, "entry_sim_time": "", "decision_id": ""},
                    )
                    entry_price = float(pending.get("entry_price", 0.0) or 0.0)
                    side = pending.get("side", side)
                    # If pending is the default empty dict (no entry recorded
                    # this run — e.g., position inherited at startup), try
                    # the per-symbol decision_ids_by_symbol mapping AND fall
                    # back to the broker's ``avg_fill_price`` for entry.
                    if not pending.get("decision_id"):
                        ids = getattr(self.vars, "last_decision_ids_by_symbol", {}) or {}
                        pending["decision_id"] = (
                            ids.get(order_sym, "")
                            or getattr(self.vars, "last_decision_id", "")
                            or ""
                        )
                    if entry_price <= 0.0:
                        try:
                            fallback_entry = float(getattr(position, "avg_fill_price", 0.0) or 0.0)
                            if fallback_entry > 0.0:
                                entry_price = fallback_entry
                        except Exception:
                            pass
                    if entry_price > 0:
                        if side == "LONG":
                            realized_pnl_pct = (float(price) - entry_price) / entry_price * 100.0
                        else:
                            realized_pnl_pct = (entry_price - float(price)) / entry_price * 100.0
                    else:
                        realized_pnl_pct = 0.0
                    self._closes_processed += 1
                    outcome = "win" if realized_pnl_pct > 0 else "loss"
                    logger.info(
                        "[B1] Position PARTIAL CLOSE: %s side=%s entry=%.2f exit=%.2f pnl=%.2f%% outcome=%s",
                        order_sym, side, entry_price, float(price), realized_pnl_pct, outcome,
                    )
                    # Update pending entry_price for the remaining slice
                    if order_sym in self._pending_outcomes:
                        self._pending_outcomes[order_sym]["entry_price"] = (
                            entry_price  # keep the original entry for the rest
                        )
                    decision_id = pending.get("decision_id", "")
                    if decision_id:
                        bridge_mode = self.parameters.get("attribution_bridge_mode", "light")
                        threshold = float(
                            self.parameters.get("attribution_loss_threshold_pct", 2.0)
                        )
                        should_bridge = (
                            bridge_mode == "heavy"
                            or (outcome == "loss" and abs(realized_pnl_pct) > threshold)
                        )
                        if should_bridge:
                            self._attribution_bridge_call(
                                decision_id=decision_id,
                                outcome=outcome,
                                pnl_pct=realized_pnl_pct,
                                symbol=order_sym,
                            )
                    if outcome == "loss" and abs(realized_pnl_pct) > float(
                        self.parameters.get("attribution_loss_threshold_pct", 2.0)
                    ):
                        regime = getattr(self.vars, "current_regime", "unknown") or "unknown"
                        self._attribution_write_loss_lesson(
                            symbol=order_sym,
                            side=side,
                            entry_price=entry_price,
                            exit_price=float(price),
                            realized_pnl_pct=realized_pnl_pct,
                            bars_held=bars_held,
                            regime=regime,
                            decision_id=decision_id,
                        )
                    return

                if pre_qty > 0.0 and post_qty > pre_qty:
                    # ADD / RE-ENTRY — keep prior entry context as the
                    # weighted-avg lives on the broker. Just refresh.
                    logger.debug(
                        "[B1] Position ADD: %s pre=%.6f post=%.6f",
                        order_sym, pre_qty, post_qty,
                    )
                    return

                # Fallback: pre_qty == 0 and post_qty == 0 → likely a rejected
                # or immediately-cancelled fill. Ignore.
                logger.debug(
                    "[B1] Fill no-op: %s pre=%.6f post=%.6f buy=%s",
                    order_sym, pre_qty, post_qty, is_buy,
                )

            except Exception as exc:
                # Never crash the strategy on an attribution bug.
                logger.exception("on_filled_order attribution failed: %s", exc)

    return PaperTradeCommitteeStrategy


# ─── Main ──────────────────────────────────────────────────────────────


def parse_universe(value: str) -> list[str]:
    """Parse comma-separated assets like 'BTC,ETH,SOL' into ['BTC', ...]."""
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Paper-trade the LLM investment committee on Binance Testnet",
    )
    parser.add_argument(
        "--asset",
        type=str,
        default="BTC",
        help="Comma-separated universe, e.g. 'BTC' or 'BTC,ETH,SOL' (default: BTC)",
    )
    parser.add_argument(
        "--max-position-pct",
        type=float,
        default=0.25,
        help="Max % of portfolio per asset (default: 0.25 = 25%%)",
    )
    parser.add_argument(
        "--max-new-positions-per-run",
        type=int,
        default=1,
        help="Max new positions opened per committee run (default: 1)",
    )
    parser.add_argument(
        "--no-memory-bridge",
        action="store_true",
        help="Disable LanceDB memory bridge sync",
    )
    parser.add_argument(
        "--no-lakehouse",
        action="store_true",
        help="Disable lakehouse writes",
    )
    parser.add_argument(
        "--exchange",
        type=str,
        default="binance",
        choices=["binance"],
        help="Exchange for paper trading (default: binance testnet)",
    )
    args = parser.parse_args()

    universe = parse_universe(args.asset)
    if not universe:
        logger.error("--asset must specify at least one symbol")
        return 1

    # ── Build broker ───────────────────────────────────────────────
    config = build_ccxt_config(args.exchange)
    if not check_credentials(config):
        return 1

    try:
        from lumibot.brokers import Ccxt
        from lumibot.traders import Trader
    except ImportError as exc:
        logger.error(
            "Cannot import LumiBot. Activate the venv first:\n"
            "  cd %s && source venv/bin/activate",
            LUMIBOT_ROOT,
        )
        return 1

    broker = Ccxt(config)

    # ── Build strategy ─────────────────────────────────────────────
    StrategyClass = make_committee_strategy_class()
    StrategyClass.parameters = {
        **StrategyClass.parameters,
        "universe": universe,
        "max_position_pct": args.max_position_pct,
        "max_new_positions_per_run": args.max_new_positions_per_run,
        "enable_notifications": True,
        "use_memory_bridge": not args.no_memory_bridge,
        "lakehouse_enabled": not args.no_lakehouse,
    }

    logger.info("=" * 60)
    logger.info("STARTING COMMITTEE PAPER-TRADE")
    logger.info("  Universe: %s", universe)
    logger.info("  Max position: %.0f%%", args.max_position_pct * 100)
    logger.info("  Max new/run: %d", args.max_new_positions_per_run)
    logger.info("  Memory bridge: %s", "ON" if not args.no_memory_bridge else "OFF")
    logger.info("  Lakehouse: %s", "ON" if not args.no_lakehouse else "OFF")
    logger.info("  Exchange: %s (sandbox)", args.exchange)
    logger.info("=" * 60)

    strategy = StrategyClass(broker=broker)

    # ── Run ────────────────────────────────────────────────────────
    trader = Trader()
    trader.add_strategy(strategy)
    trader.run_all()

    return 0


if __name__ == "__main__":
    sys.exit(main())