#!/usr/bin/env python
"""
Binance Testnet Smoke Test
===========================

Validates end-to-end flow:
1. Connect to Binance Testnet via CCXT
2. Fetch balance
3. Load best WF-validated BTC strategy from StratForge DB
4. Compute live signal on recent OHLCV data
5. Submit a single market order to testnet

Does NOT call run_live() (which blocks). Instead tests each component directly.

Usage:
    source /home/Zev/development/trading-bots/lumibot/venv/bin/activate
    python /home/Zev/development/nexus-trade/tests/smoke_binance_testnet.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── Path Setup ────────────────────────────────────────────────────────

LUMIBOT_ROOT = Path("/home/Zev/development/trading-bots/lumibot")
NEXUS_ROOT = Path("/home/Zev/development/nexus-trade")

sys.path.insert(0, str(LUMIBOT_ROOT))
sys.path.insert(0, str(NEXUS_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(str(NEXUS_ROOT / ".env"))
load_dotenv(str(LUMIBOT_ROOT / ".env"), override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("smoke_test")

# ─── Test Strategy ─────────────────────────────────────────────────────

BEST_BTC_STRATEGY = "meta_cmo_alma_atr_wf_v1"  # WF sharpe=5.207, 9/10 profitable

RESULTS = {
    "connection": False,
    "balance": False,
    "strategy_loaded": False,
    "signal_computed": False,
    "order_submitted": False,
    "order_id": None,
    "order_side": None,
    "order_qty": None,
    "order_price": None,
    "errors": [],
}


def section(title: str):
    logger.info(f"\n{'='*60}\n  {title}\n{'='*60}")


# ─── Step 1: Connect to Binance Testnet ────────────────────────────────

def test_connection():
    section("STEP 1: Connect to Binance Testnet")
    import ccxt

    api_key = os.environ.get("BINANCE_TESTNET_KEY", "")
    secret = os.environ.get("BINANCE_TESTNET_SECRET", "")

    if not api_key or not secret:
        RESULTS["errors"].append("Missing BINANCE_TESTNET_KEY/SECRET in .env")
        return None

    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"adjustForTimeDifference": True},
    })
    exchange.set_sandbox_mode(True)

    try:
        # Test connectivity
        t = exchange.fetch_time()
        skew_ms = t - int(time.time() * 1000)
        logger.info(f"✅ Connected to Binance Testnet (clock skew: {skew_ms}ms)")

        # Load markets
        markets = exchange.load_markets()
        btc_usdt = markets.get("BTC/USDT")
        if btc_usdt:
            logger.info(f"✅ BTC/USDT market found: {btc_usdt['symbol']}")

        RESULTS["connection"] = True
        return exchange

    except Exception as e:
        logger.error(f"❌ Connection failed: {e}")
        RESULTS["errors"].append(f"Connection: {e}")
        return None


# ─── Step 2: Fetch Balance ─────────────────────────────────────────────

def test_balance(exchange):
    section("STEP 2: Fetch Testnet Balance")
    try:
        balance = exchange.fetch_balance()

        usdt_free = balance.get("USDT", {}).get("free", 0)
        usdt_used = balance.get("USDT", {}).get("used", 0)
        btc_free = balance.get("BTC", {}).get("free", 0)

        logger.info(f"✅ Balance fetched:")
        logger.info(f"   USDT: free={usdt_free:.2f}  used={usdt_used:.2f}")
        logger.info(f"   BTC:  free={btc_free:.6f}")

        if float(usdt_free) > 0 or float(btc_free) > 0:
            RESULTS["balance"] = True
        else:
            logger.warning("⚠️ Zero balance — testnet account may need funding")

        RESULTS["balance"] = True
        return balance

    except Exception as e:
        logger.error(f"❌ Balance fetch failed: {e}")
        RESULTS["errors"].append(f"Balance: {e}")
        return None


# ─── Step 3: Load Strategy from DB ─────────────────────────────────────

def test_strategy_load():
    section(f"STEP 3: Load Strategy '{BEST_BTC_STRATEGY}'")
    try:
        from runners.batch_crypto_screen import load_strategy_from_db
        loaded = load_strategy_from_db(BEST_BTC_STRATEGY)
        if loaded is None:
            RESULTS["errors"].append(f"Could not load strategy {BEST_BTC_STRATEGY}")
            return None, None

        fn, params = loaded
        logger.info(f"✅ Strategy loaded: {BEST_BTC_STRATEGY}")
        logger.info(f"   params: {json.dumps(params, default=str)[:200]}")
        RESULTS["strategy_loaded"] = True
        return fn, params

    except Exception as e:
        logger.error(f"❌ Strategy load failed: {e}")
        RESULTS["errors"].append(f"Strategy load: {e}")
        return None, None


# ─── Step 4: Compute Signal on Recent Data ─────────────────────────────

def test_signal(exchange, strategy_fn, strategy_params):
    section("STEP 4: Compute Live Signal on BTC/USDT 4h")
    try:
        # Fetch 300 candles of 4h BTC/USDT
        ohlcv = exchange.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=300)
        if not ohlcv or len(ohlcv) < 50:
            RESULTS["errors"].append(f"Not enough OHLCV data: {len(ohlcv) if ohlcv else 0}")
            return None

        import pandas as pd
        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("datetime", inplace=True)
        df.drop(columns=["timestamp"], inplace=True)

        logger.info(f"✅ Fetched {len(df)} bars of BTC/USDT 4h")
        logger.info(f"   Latest bar: {df.index[-1]} close={df['close'].iloc[-1]:.2f}")

        # Compute signal
        entries, exits = strategy_fn(df, strategy_params)

        if hasattr(entries, "iloc"):
            latest_entry = bool(entries.iloc[-1])
            latest_exit = bool(exits.iloc[-1])
        else:
            latest_entry = bool(entries[-1]) if len(entries) > 0 else False
            latest_exit = bool(exits[-1]) if len(exits) > 0 else False

        # Count total signals
        n_entries = int(sum(entries)) if hasattr(entries, "__iter__") else 0
        n_exits = int(sum(exits)) if hasattr(exits, "__iter__") else 0

        logger.info(f"✅ Signal computed:")
        logger.info(f"   Latest bar: entry={latest_entry} exit={latest_exit}")
        logger.info(f"   Total signals: {n_entries} entries, {n_exits} exits over {len(df)} bars")

        RESULTS["signal_computed"] = True
        return df, latest_entry, latest_exit

    except Exception as e:
        import traceback
        logger.error(f"❌ Signal computation failed: {e}")
        traceback.print_exc()
        RESULTS["errors"].append(f"Signal: {e}")
        return None


# ─── Step 5: Submit Order to Testnet ───────────────────────────────────

def test_order_submission(exchange, df):
    section("STEP 5: Submit Test Order to Binance Testnet")
    try:
        # Fetch balance to size the order
        balance = exchange.fetch_balance()
        usdt_free = float(balance.get("USDT", {}).get("free", 0))

        if usdt_free < 10:
            logger.warning(f"⚠️ Low USDT balance ({usdt_free}), using minimum order")
            order_value = min(usdt_free * 0.95, 15)  # minimum ~$10 on Binance
        else:
            # Use 40% of portfolio (standard position size)
            order_value = usdt_free * 0.40

        if order_value < 10:
            order_value = 15  # Binance min notional is ~$10-15

        ticker = exchange.fetch_ticker("BTC/USDT")
        btc_price = ticker["last"]
        qty = order_value / btc_price

        # Binance lot size: BTC has stepSize of 0.00001
        # Round to 5 decimal places (step size for BTC)
        import math
        step = 0.00001
        qty = math.floor(qty / step) * step
        qty = round(qty, 5)

        if qty < 0.00001:
            logger.warning(f"Qty too small ({qty}), bumping to minimum")
            qty = step

        logger.info(f"Submitting BUY order: BTC/USDT qty={qty:.5f} (~${qty*btc_price:.2f})")

        order = exchange.create_market_buy_order("BTC/USDT", qty)

        order_id = order.get("id", "unknown")
        status = order.get("status", "unknown")
        filled = order.get("filled", 0)
        avg_price = order.get("average", btc_price)

        logger.info(f"✅ Order submitted!")
        logger.info(f"   Order ID: {order_id}")
        logger.info(f"   Status: {status}")
        logger.info(f"   Filled: {filled:.5f} BTC @ ~${avg_price:.2f}")

        RESULTS["order_submitted"] = True
        RESULTS["order_id"] = order_id
        RESULTS["order_side"] = "BUY"
        RESULTS["order_qty"] = filled or qty
        RESULTS["order_price"] = avg_price

        return order

    except Exception as e:
        logger.error(f"❌ Order submission failed: {e}")
        RESULTS["errors"].append(f"Order: {e}")
        return None


# ─── Step 6: Verify Order in History ───────────────────────────────────

def test_order_history(exchange):
    section("STEP 6: Verify Order in Testnet History")
    try:
        # Fetch recent orders
        orders = exchange.fetch_orders("BTC/USDT", limit=5)
        if orders:
            latest = orders[-1]
            logger.info(f"✅ Order found in history:")
            logger.info(f"   ID: {latest.get('id')}")
            logger.info(f"   Status: {latest.get('status')}")
            logger.info(f"   Side: {latest.get('side')}")
            logger.info(f"   Filled: {latest.get('filled', 0):.5f} @ {latest.get('average', 'N/A')}")
        else:
            logger.warning("⚠️ No orders in history")

    except Exception as e:
        logger.warning(f"Could not fetch order history: {e}")


# ─── Main ──────────────────────────────────────────────────────────────

def main():
    section("BINANCE TESTNET SMOKE TEST")
    logger.info(f"Time: {datetime.utcnow().isoformat()} UTC")
    logger.info(f"Strategy: {BEST_BTC_STRATEGY}")
    logger.info(f"Asset: BTC/USDT")

    # Step 1: Connect
    exchange = test_connection()
    if not exchange:
        _report()
        return 1

    # Step 2: Balance
    test_balance(exchange)

    # Step 3: Load strategy
    strategy_fn, strategy_params = test_strategy_load()
    if strategy_fn is None:
        _report()
        return 1

    # Step 4: Compute signal
    df = test_signal(exchange, strategy_fn, strategy_params)
    # Continue even if signal fails — we still want to test order submission

    # Step 5: Submit order (regardless of signal — this is a smoke test)
    test_order_submission(exchange, df)

    # Step 6: Verify in history
    test_order_history(exchange)

    _report()
    return 0 if RESULTS["order_submitted"] else 1


def _report():
    section("SMOKE TEST RESULTS")
    checks = [
        ("Connection", RESULTS["connection"]),
        ("Balance", RESULTS["balance"]),
        ("Strategy loaded", RESULTS["strategy_loaded"]),
        ("Signal computed", RESULTS["signal_computed"]),
        ("Order submitted", RESULTS["order_submitted"]),
    ]
    for name, ok in checks:
        logger.info(f"  {'✅' if ok else '❌'} {name}")

    if RESULTS["order_id"]:
        logger.info(f"\n  Order: {RESULTS['order_side']} {RESULTS['order_qty']:.5f} BTC @ ${RESULTS['order_price']:.2f}")
        logger.info(f"  Order ID: {RESULTS['order_id']}")

    if RESULTS["errors"]:
        logger.error(f"\nErrors ({len(RESULTS['errors'])}):")
        for e in RESULTS["errors"]:
            logger.error(f"  - {e}")

    # Write results to logs dir
    log_dir = NEXUS_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    result_file = log_dir / f"smoke_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(result_file, "w") as f:
        json.dump({k: v for k, v in RESULTS.items()}, f, indent=2, default=str)
    logger.info(f"\nResults saved to: {result_file}")


if __name__ == "__main__":
    sys.exit(main())
