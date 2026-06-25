"""
Replay Subprocess Runner — A3 (2026-06-25)
==========================================

Subprocess entry point invoked by ``src.runners.post_backtest_sync``
when projecting backtest round-trips into ``nexus_decisions.lance``.

This module runs in the **AQOS venv** (which has lancedb + sentence-transformers).
The Lumibot venv intentionally does NOT include them — it calls this runner
as a subprocess instead of importing lancedb directly.

Invocation::

    /home/Zev/development/agentic-quant-os/.venv/bin/python \\
        -m src.runners._replay_subprocess_runner \\
        --round-trips-json /tmp/replay_*.json \\
        --strategy-name NexusCommitteeStrategy

The runner:
1. Loads the JSON-serialized decision records (already built by the
   Lumibot-venv caller).
2. Embeds them in batches via the local Qwen3-Embedding-0.6B model.
3. Writes them into the ``nexus_decisions`` LanceDB table via
   ``NexusVectorMemory.batch_store_decisions``.
4. Prints a JSON summary on stdout.

The Lumibot-venv caller parses the last JSON block and propagates the
stats back to the calling ``sync_backtest_results`` summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ensure nexus-trade/src is importable (in case PYTHONPATH wasn't honored)
_NEXUS_SRC = "/home/Zev/development/nexus-trade/src"
if _NEXUS_SRC not in sys.path:
    sys.path.insert(0, _NEXUS_SRC)
_AQOS_SRC = "/home/Zev/development/agentic-quant-os/src"
if _AQOS_SRC not in sys.path:
    sys.path.insert(0, _AQOS_SRC)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay backtest round-trip decisions into nexus_decisions.lance"
    )
    parser.add_argument(
        "--round-trips-json", required=True,
        help="Path to JSON file with serialized decision records (built by post_backtest_sync)",
    )
    parser.add_argument(
        "--strategy-name", default="NexusCommitteeStrategy",
        help="Strategy name to stamp onto each decision record",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    json_path = Path(args.round_trips_json)
    if not json_path.exists():
        result = {"errors": 1, "error_detail": f"file not found: {json_path}"}
        print(json.dumps(result))
        return 1

    try:
        with open(json_path) as f:
            decision_records: list[dict[str, Any]] = json.load(f)
    except Exception as exc:
        result = {"errors": 1, "error_detail": f"failed to parse JSON: {exc}"}
        print(json.dumps(result))
        return 1

    if not decision_records:
        result = {"embedded": 0, "skipped": 0, "errors": 0, "total": 0}
        print(json.dumps(result))
        return 0

    logger.info(
        "Replay subprocess: loading %d decisions for strategy=%s",
        len(decision_records), args.strategy_name,
    )

    try:
        from src.memory.nexus_vector_memory import get_nexus_memory

        mem = get_nexus_memory()
        if not mem.enabled:
            result = {
                "errors": len(decision_records),
                "embedded": 0, "skipped": 0, "total": len(decision_records),
                "error_detail": "vector memory disabled (lancedb or sentence-transformers unavailable)",
            }
            print(json.dumps(result))
            return 0  # non-fatal: caller treats errors>0 as a soft failure

        stats = mem.batch_store_decisions(decision_records, batch_size=64)
        result = {
            "embedded": stats.get("embedded", 0),
            "skipped": stats.get("skipped", 0),
            "errors": stats.get("errors", 0),
            "total": stats.get("total", len(decision_records)),
        }
        logger.info(
            "Replay subprocess complete: embedded=%d skipped=%d errors=%d total=%d",
            result["embedded"], result["skipped"], result["errors"], result["total"],
        )
        print(json.dumps(result))
        return 0 if result["errors"] == 0 else 0  # soft-fail; caller decides

    except Exception as exc:
        logger.exception("Replay subprocess failed")
        result = {
            "errors": len(decision_records),
            "embedded": 0, "skipped": 0, "total": len(decision_records),
            "error_detail": str(exc),
        }
        print(json.dumps(result))
        return 0  # soft-fail


if __name__ == "__main__":
    sys.exit(main())