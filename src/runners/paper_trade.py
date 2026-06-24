"""
Paper Trading Launcher
======================

Runs WF-validated StratForge strategies in paper trading mode via CCXT sandbox.

Prerequisites:
1. Binance Testnet keys in .env (BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET)
2. WF validation completed (strategies selected per asset)
3. LumiBot installed and importable

Usage:
    cd /home/Zev/development/trading-bots/lumibot
    source venv/bin/activate

    # Paper trade BTC with best strategy
    python /home/Zev/development/nexus-trade/src/runners/paper_trade.py \
        --asset BTC --strategy meta_cmo_alma_atr_wf_v1

    # Paper trade all 3 assets (one strategy each)
    python /home/Zev/development/nexus-trade/src/runners/paper_trade.py \
        --portfolio

Get Binance Testnet keys: https://testnet.binance.vision/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ─── Path Setup ────────────────────────────────────────────────────────

LUMIBOT_ROOT = Path("/home/Zev/development/trading-bots/lumibot")
NEXUS_ROOT = Path("/home/Zev/development/nexus-trade")

sys.path.insert(0, str(LUMIBOT_ROOT))
sys.path.insert(0, str(NEXUS_ROOT / "src"))

from dotenv import load_dotenv
# Load nexus-trade .env first (Binance testnet keys), then lumibot .env for model keys
load_dotenv(str(NEXUS_ROOT / ".env"))
load_dotenv(str(LUMIBOT_ROOT / ".env"), override=False)

# ─── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("paper_trade")

# ─── Portfolio Config ──────────────────────────────────────────────────

# Allocation by asset (volatility-weighted: SOL is ~2x BTC vol)
# BTC only for smoke test — expand after validation
PORTFOLIO = {
    "BTC": {"weight": 1.0, "default_strategy": "meta_cmo_alma_atr_wf_v1"},
    # "ETH": {"weight": 0.35, "default_strategy": "stc_bop_momentum"},
    # "SOL": {"weight": 0.25, "default_strategy": "stc_bop_momentum"},
}

# Timestep for live trading
# NOTE: LumiBot parses this with `int(sleeptime[:-1])` then strips a unit suffix.
# "4 hour" → ValueError. "4H" works. Keep in sync with live_adapter.LIVE_TIMESTEP.
DEFAULT_TIMESTEP = "4H"

# ─── Strategy Loading ──────────────────────────────────────────────────

SF_DB_PATH = os.path.expanduser(
    "~/.hermes/profiles/herm-bot/home/agentic-quant-os/data/quant.duckdb"
)


def load_strategy_fn(strategy_name: str):
    """Load a strategy function from the StratForge DB."""
    from runners.batch_crypto_screen import load_strategy_from_db

    loaded = load_strategy_from_db(strategy_name)
    if loaded is None:
        raise ValueError(f"Could not load strategy '{strategy_name}' from DB")
    return loaded  # (fn, params)


def get_wf_validated_strategies() -> dict:
    """Get the best WF-validated strategy per asset from nexus_results.duckdb."""
    import duckdb

    db_path = str(NEXUS_ROOT / "data" / "nexus_results.duckdb")
    if not Path(db_path).exists():
        logger.warning("nexus_results.duckdb not found — using defaults")
        return {asset: cfg["default_strategy"] for asset, cfg in PORTFOLIO.items()}

    con = duckdb.connect(db_path, read_only=True)
    try:
        # Get best strategy per asset by avg Sharpe
        rows = con.execute("""
            SELECT symbol, strategy_name
            FROM (
                SELECT
                    strategy_name, symbol,
                    AVG(sharpe) as avg_sharpe,
                    AVG(total_return_pct) as avg_return,
                    SUM(CASE WHEN profitable THEN 1 ELSE 0 END) as n_profitable,
                    COUNT(*) as n_windows,
                    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY AVG(sharpe) DESC) as rn
                FROM walk_forward_results
                GROUP BY strategy_name, symbol
            ) ranked
            WHERE rn = 1
              AND n_profitable >= 3
              AND avg_sharpe > 0
            ORDER BY symbol
        """).fetchall()
    except Exception:
        # Table might not exist yet
        return {asset: cfg["default_strategy"] for asset, cfg in PORTFOLIO.items()}
    finally:
        con.close()

    result = {}
    for symbol, strategy_name in rows:
        result[symbol] = strategy_name

    # Fill defaults for missing assets
    for asset, cfg in PORTFOLIO.items():
        if asset not in result:
            result[asset] = cfg["default_strategy"]

    return result


# ─── Broker Config ─────────────────────────────────────────────────────


def build_ccxt_config(exchange: str = "binance") -> dict:
    """Build CCXT broker configuration for sandbox/paper trading.

    Defaults to Binance Testnet (testnet.binance.vision).
    Kraken sandbox is broken in ccxt 4.5.56 (urls['test'] is None).
    """
    if exchange == "binance":
        return {
            "exchange_id": "binance",
            "apiKey": os.environ.get("BINANCE_TESTNET_KEY", ""),
            "secret": os.environ.get("BINANCE_TESTNET_SECRET", ""),
            "margin": False,
            "sandbox": True,
            # Fix for -1021 timestamp errors (clock skew +390ms)
            "options": {"adjustForTimeDifference": True},
        }
    elif exchange == "kraken":
        # NOTE: Kraken sandbox is broken in ccxt 4.5.56 — set_sandbox_mode()
        # crashes because urls['test'] is None. Kept for compatibility only.
        logger.warning(
            "⚠️ Kraken sandbox is broken in ccxt 4.5.56 — use Binance Testnet instead"
        )
        return {
            "exchange_id": "kraken",
            "apiKey": os.environ.get("KRAKEN_API_KEY", ""),
            "secret": os.environ.get("KRAKEN_API_SECRET", ""),
            "margin": False,
            "sandbox": True,
        }
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")


def check_credentials(config: dict) -> bool:
    """Verify that API credentials are present."""
    if not config.get("apiKey") or not config.get("secret"):
        logger.error(
            "❌ Missing API credentials. Set them in "
            f"{LUMIBOT_ROOT}/.env"
        )
        return False
    return True


# ─── Single-Asset Paper Trading ────────────────────────────────────────


def run_single_asset(
    asset: str,
    strategy_name: str,
    exchange: str = "binance",
    telegram_enabled: bool = False,
):
    """Run paper trading for a single asset/strategy pair."""
    from lumibot.brokers import Ccxt
    from strategies.live_adapter import LiveStratForgeAdapter

    # Load strategy
    logger.info(f"Loading strategy '{strategy_name}' from StratForge DB...")
    strategy_fn, strategy_params = load_strategy_fn(strategy_name)

    # Build broker config
    config = build_ccxt_config(exchange)
    if not check_credentials(config):
        return

    # Quote currency mapping (Binance uses USDT, Kraken uses USD)
    quote = "USD" if exchange == "kraken" else "USDT"

    logger.info(f"Starting paper trading:")
    logger.info(f"  Asset: {asset}/{quote}")
    logger.info(f"  Strategy: {strategy_name}")
    logger.info(f"  Exchange: {exchange} (sandbox)")
    logger.info(f"  Timestep: {DEFAULT_TIMESTEP}")

    broker = Ccxt(config)
    weight = PORTFOLIO.get(asset, {}).get("weight", 0.33)

    strategy = LiveStratForgeAdapter(
        broker=broker,
        parameters={
            "strategy_fn": strategy_fn,
            "strategy_params": strategy_params,
            "base_symbol": asset,
            "quote_symbol": quote,
            "lookback_bars": 250,
            "timestep": DEFAULT_TIMESTEP,
            "position_size": weight,
            "max_daily_loss_pct": 5.0,
            "max_drawdown_pct": 15.0,
            "use_atr_stop": True,
            "atr_multiplier": 2.0,
            # Telegram notifications — requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
            # in nexus-trade/.env or lumibot venv. Default OFF. Pass --telegram to enable.
            "telegram_enabled": telegram_enabled,
        },
    )

    # Run live (blocks)
    strategy.run_live()


# ─── Portfolio Paper Trading ───────────────────────────────────────────


def run_portfolio(exchange: str = "binance"):
    """Run paper trading for the full BTC/ETH/SOL portfolio.

    Each asset gets its best WF-validated strategy, weighted by volatility.
    """
    # Get best strategy per asset
    strategies = get_wf_validated_strategies()

    logger.info("Portfolio configuration:")
    for asset, sname in strategies.items():
        weight = PORTFOLIO.get(asset, {}).get("weight", 0.33)
        logger.info(f"  {asset}: {sname} ({weight:.0%} allocation)")

    # Note: LumiBot doesn't natively support multi-strategy in one process.
    # For paper trading, run each asset as a separate process:
    #
    #   python paper_trade.py --asset BTC --strategy <btc_strategy> &
    #   python paper_trade.py --asset ETH --strategy <eth_strategy> &
    #   python paper_trade.py --asset SOL --strategy <sol_strategy> &
    #
    # Or use a process manager / tmux to run all three.

    print("\nTo run the full portfolio, launch each asset in a separate terminal:")
    quote = "USD" if exchange == "kraken" else "USDT"
    for asset, sname in strategies.items():
        print(
            f"  python {__file__} --asset {asset} "
            f"--strategy {sname} --exchange {exchange}"
        )


# ─── Main ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Paper trading launcher for StratForge strategies"
    )
    parser.add_argument(
        "--asset", type=str, choices=["BTC", "ETH", "SOL"],
        default="BTC",
        help="Asset to trade (default: BTC)",
    )
    parser.add_argument(
        "--strategy", type=str,
        help="Strategy name (from StratForge DB)",
    )
    parser.add_argument(
        "--portfolio", action="store_true",
        help="Show portfolio configuration and launch instructions",
    )
    parser.add_argument(
        "--exchange", type=str, default="binance", choices=["kraken", "binance"],
        help="Exchange for paper trading (default: binance testnet)",
    )
    parser.add_argument(
        "--timestep", type=str, default=DEFAULT_TIMESTEP,
        help="Bar timestep (e.g. '4H', '1D') — LumiBot format, not '4 hour'",
    )
    parser.add_argument(
        "--telegram", action="store_true",
        help="Enable Telegram notifications (requires TELEGRAM_BOT_TOKEN and "
             "TELEGRAM_CHAT_ID in env)",
    )
    args = parser.parse_args()

    if args.portfolio:
        run_portfolio(exchange=args.exchange)
        return

    if not args.asset or not args.strategy:
        parser.error("--asset and --strategy are required (or use --portfolio)")

    run_single_asset(
        args.asset,
        args.strategy,
        exchange=args.exchange,
        telegram_enabled=args.telegram,
    )


if __name__ == "__main__":
    main()
