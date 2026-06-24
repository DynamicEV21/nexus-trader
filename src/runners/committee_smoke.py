#!/usr/bin/env python
"""
Committee Smoke Test — minimal 1-day BTC backtest with MiniMax-M3
==================================================================

Runs a NexusCommitteeStrategy backtest on BTC 4h data for a short date range
to validate the full stack: MiniMax-M3 model calls, tool registration,
memory writes, and agent traces.

Usage:
    python -m src.runners.committee_smoke --symbol BTC --start 2025-06-01 --end 2025-06-02 --budget 10000
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

# Ensure src is importable
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT = os.path.dirname(_SRC)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pandas as pd
from lumibot.backtesting import PandasDataBacktesting
from lumibot.entities import Asset, Data, TradingFee

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# DETERMINISTIC GUARD — fail loud if lancedb or sentence_transformers
# were installed into the LUMIBOT venv.
#
# Architecture: the vector memory stack (lancedb + sentence_transformers +
# Qwen3-Embedding-0.6B) lives in the AQOS venv and is invoked from
# nexus-trade via a subprocess bridge (src/memory/bridge.py). The lumibot
# venv must stay LIGHT. If something tries to install these packages
# in the lumibot venv again, this guard fails fast and refuses to run.
#
# This is RUNTIME enforcement; the matching docs are in AGENTS.md
# "Vector memory stack — single source of truth" section.
# ----------------------------------------------------------------------
def _check_vector_memory_venv_isolation() -> None:
    """Hard-fail if lancedb or sentence_transformers are importable here.

    These packages belong in the AQOS venv, not the lumibot venv. If they
    are importable from the current Python (i.e. this smoke runner's
    interpreter), something has been installed in the wrong venv.
    """
    forbidden = []
    try:
        import lancedb  # noqa: F401
        forbidden.append("lancedb")
    except ImportError:
        pass
    try:
        import sentence_transformers  # noqa: F401
        forbidden.append("sentence_transformers")
    except ImportError:
        pass
    if forbidden:
        banner = (
            "\n"
            "+--------------------------------------------------------------+\n"
            "| FATAL: vector memory stack found in LUMIBOT venv              |\n"
            "+--------------------------------------------------------------+\n"
            f"| Detected: {', '.join(forbidden):<54}|\n"
            "| The vector memory stack (lancedb + sentence_transformers +\n"
            "| Qwen3-Embedding-0.6B) must live in the AQOS venv. Nexus calls\n"
            "| it via subprocess bridge (src/memory/bridge.py), NOT in-process.\n"
            "|\n"
            "| If you ran `pip install lancedb sentence_transformers` in the\n"
            "| lumibot venv, UNINSTALL it:\n"
            "|   <lumibot-venv>/bin/pip uninstall -y lancedb sentence_transformers\n"
            "|\n"
            "| See nexus-trade/AGENTS.md -> 'Vector memory stack' for context.\n"
            "+--------------------------------------------------------------+\n"
        )
        print(banner, file=sys.stderr)
        raise SystemExit(
            "Vector memory stack (lancedb/sentence_transformers) must not be "
            "installed in the lumibot venv. See stderr banner for fix."
        )


_check_vector_memory_venv_isolation()


def main() -> int:
    parser = argparse.ArgumentParser(description="Nexus Committee smoke test (1-day BTC)")
    parser.add_argument("--symbol", default="BTC", help="Ticker symbol (default: BTC)")
    parser.add_argument("--start", default="2025-06-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2025-06-02", help="End date YYYY-MM-DD")
    parser.add_argument("--budget", type=float, default=10000, help="Starting budget (default: 10000)")
    parser.add_argument("--refresh-data", action="store_true",
                        help="Refresh OHLCV data from Binance Testnet before backtest")
    parser.add_argument("--no-lakehouse", action="store_true", help="Disable lakehouse tools")
    parser.add_argument("--no-memory-bridge", action="store_true", help="Skip memory bridge")
    parser.add_argument("--quiet", action="store_true", help="Reduce log output")
    args = parser.parse_args()

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Optionally refresh live data before loading parquet
    if args.refresh_data:
        try:
            from src.data.live_data import refresh_live_data
            logger.info("Refreshing live data for %s 4h from Binance Testnet...", args.symbol)
            refresh_result = refresh_live_data(args.symbol, "4h")
            logger.info(
                "Live data refresh: %d new bars fetched, last_bar=%s, status=%s, latency=%dms",
                refresh_result["bars_fetched"],
                refresh_result["last_bar_timestamp"],
                refresh_result["exchange_status"],
                refresh_result["latency_ms"],
            )
            print(f"  Live data refresh: {refresh_result['bars_fetched']} new bars, "
                  f"last={refresh_result['last_bar_timestamp']}, "
                  f"{refresh_result['latency_ms']}ms")
        except Exception as exc:
            logger.warning("Live data refresh failed (non-fatal): %s", exc)
            print(f"  Live data refresh: FAILED ({exc})")

    # Load BTC 4h parquet data
    parquet_path = os.path.join(_PROJECT, "data", "4h_cache", f"{args.symbol}_4h.parquet")
    if not os.path.exists(parquet_path):
        logger.error("Parquet data not found: %s", parquet_path)
        return 1

    df = pd.read_parquet(parquet_path)
    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        logger.error("Parquet index is not DatetimeIndex: %s", type(df.index))
        return 1

    logger.info("Loaded %s: %d rows, %s → %s", parquet_path, len(df), df.index[0], df.index[-1])

    # Set up backtesting data source — pass class + kwargs to .backtest()
    crypto_asset = Asset(args.symbol, Asset.AssetType.CRYPTO)
    quote_asset = Asset("USDT", Asset.AssetType.FOREX)
    data_item = Data(asset=crypto_asset, df=df, quote=quote_asset, timestep="hour")

    # Import strategy (after sys.path setup)
    from src.strategies.nexus_committee import NexusCommitteeStrategy

    NexusCommitteeStrategy.parameters = {
        "universe": [args.symbol],
        "max_position_pct": 0.25,
        "max_new_positions_per_run": 1,
        "enable_notifications": False,
        "use_memory_bridge": not args.no_memory_bridge,
        "lakehouse_enabled": not args.no_lakehouse,
        # Raise default agent call cap so committee can complete all 5 runs.
        # History: 6 → 10 (2026-06-23 Tristan); 10 → 30 (2026-06-23 — see
        # non-blockers-fix-report: bull/bear cases w/ full toolset can run >10 LLM calls
        # per agent; 30 is comfortable headroom for 5 iterations).
        "agent_max_model_calls": 30,
    }

    trading_fee = TradingFee(percent_fee=0.001)

    logger.info(
        "Starting committee smoke test: %s %s→%s, budget=$%.0f",
        args.symbol, args.start, args.end, args.budget,
    )

    result = NexusCommitteeStrategy.backtest(
        PandasDataBacktesting,
        backtesting_start=datetime.fromisoformat(args.start),
        backtesting_end=datetime.fromisoformat(args.end),
        pandas_data=[data_item],
        buy_trading_fees=[trading_fee],
        sell_trading_fees=[trading_fee],
        quote_asset=quote_asset,
        budget=args.budget,
        quiet_logs=False,
    )

    print("\n" + "=" * 60)
    print("COMMITTEE SMOKE TEST RESULTS")
    print("=" * 60)
    print(f"  Symbol: {args.symbol}")
    print(f"  Period: {args.start} → {args.end}")
    print(f"  Budget: ${args.budget:,.0f}")
    if result:
        for key, value in result.items():
            if isinstance(value, (int, float)):
                print(f"  {key}: {value:.4f}")
            else:
                print(f"  {key}: {value}")
    else:
        print("  (no result returned)")
    print("=" * 60)

    # ── Post-backtest sync ──
    # Project parquet artifacts into nexus_results.duckdb for cross-run analysis.
    sync_summary = None
    try:
        from src.runners.post_backtest_sync import sync_latest
        sync_summary = sync_latest()
        print(f"\n  Post-backtest sync: runs={sync_summary['runs_inserted']}, "
              f"trades={sync_summary['trades_inserted']}, "
              f"observations={sync_summary['observations_inserted']}")
        if sync_summary.get("errors"):
            print(f"  Sync errors: {sync_summary['errors']}")
    except Exception as exc:
        logger.warning("Post-backtest sync failed (non-fatal): %s", exc)
        print(f"\n  Post-backtest sync: FAILED ({exc})")

    # ── Memory bridge sync (JSONL → LanceDB) ──
    if not args.no_memory_bridge:
        try:
            from src.memory.bridge import MemoryBridge
            bridge = MemoryBridge(strategy_name="Nexus_Trader")
            bridge_stats = bridge.sync_all()
            total = sum(bridge_stats.get(k, {}).get("embedded", 0)
                        for k in ("decisions", "lessons", "theses", "memories"))
            print(f"  Memory bridge: {total} entries synced")
        except Exception as exc:
            logger.warning("Memory bridge sync failed (non-fatal): %s", exc)

        # ── AQS write-back (JSONL → quant.duckdb via QuantClient) ──
        try:
            from src.memory.aqs_sync import sync_from_jsonl, count_nexus_entries

            print("\n  Syncing decisions/lessons to AQS lakehouse...")
            aqs_stats = sync_from_jsonl(strategy_name="Nexus_Trader")
            print(f"  AQS sync: {aqs_stats}")

            counts = count_nexus_entries()
            print(f"  AQS nexus entries: {counts}")
        except Exception as exc:
            logger.warning("AQS sync failed (non-fatal): %s", exc)
            print(f"  AQS sync: FAILED ({exc})")

    print("=" * 60)
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
