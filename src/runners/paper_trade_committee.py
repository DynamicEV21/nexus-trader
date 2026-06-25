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
        }

        def initialize(self) -> None:
            """LumiBot lifecycle hook — runs once at strategy start.

            Delegates to the parent (which sets up agents + tools) then
            configures Telegram notifications if creds are present.
            """
            # Honor 4H bar like the algo paper trader (rather than the
            # default 1D in the backtest variant) so the committee and
            # algo runners evaluate at the same time. This makes
            # side-by-side comparison trivial.
            self.sleeptime = "4H"

            super().initialize()

            # ── Telegram notifications ──────────────────────────────
            # Wire the built-in LumiBot Telegram provider if creds exist.
            # It self-skips if TELEGRAM_BOT_TOKEN/CHAT_ID are missing.
            try:
                self.notifications.configure_telegram(
                    bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
                    chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
                    parse_mode="Markdown",
                )
                logger.info(
                    "Telegram notifications configured (chat_id=%s)",
                    os.environ.get("TELEGRAM_CHAT_ID", "(missing)"),
                )
            except Exception as exc:
                logger.warning("Telegram setup skipped: %s", exc)

            # Startup notification
            try:
                self.notify(
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
            except Exception as exc:
                logger.debug("Startup notify skipped: %s", exc)

        def on_trading_iteration(self) -> None:
            """LumiBot lifecycle hook — runs every self.sleeptime.

            Wraps the parent's full committee flow with Telegram
            start/end notifications so you get a push per bar.
            """
            run = (self.vars.committee_run_count or 0) + 1
            try:
                self.notify(
                    title=f"📊 Committee run {run} starting",
                    message=(
                        f"Time: {self.get_datetime().isoformat()}\n"
                        f"Universe: {self.parameters.get('universe')}"
                    ),
                    severity="info",
                )
            except Exception:
                pass

            try:
                super().on_trading_iteration()
                # If parent didn't raise, the run completed cleanly.
                summary = (self.vars.last_decision_summary or "")[:500]
                try:
                    self.notify(
                        title=f"✅ Committee run {run} complete",
                        message=summary or "(no summary)",
                        severity="info",
                    )
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("Committee run %d failed", run)
                try:
                    self.notify(
                        title=f"❌ Committee run {run} FAILED",
                        message=f"```\n{str(exc)[:1000]}\n```",
                        severity="error",
                    )
                except Exception:
                    pass
                raise

        def on_finish(self, *args, **kwargs) -> None:
            """LumiBot lifecycle hook — runs at strategy shutdown."""
            try:
                self.notify(
                    title="🔴 Committee paper-trade stopped",
                    message=f"Finished: {datetime.now().isoformat()}",
                    severity="warning",
                )
            except Exception:
                pass
            super().on_finish(*args, **kwargs)

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