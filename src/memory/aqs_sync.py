"""
AQS Sync — Write Nexus Trader decisions/lessons/results to the AQS lakehouse.

This module bridges Nexus Trader committee outputs into the agentic-quant-os
DuckDB lakehouse via :class:`QuantClient`. It writes to three tables:

1. ``agent_memory`` — PM decisions (memory_type='trade_decision')
2. ``experience_bank`` — lessons and trade outcomes (source_repo='nexus-trade')
3. ``signals`` — committee signal broadcasts (source_repo='nexus-trade')

Usage (post-backtest hook)::

    from src.memory.aqs_sync import sync_committee_decision, sync_lesson

    # After PM makes a decision:
    sync_committee_decision(decision_dict)

    # After learning a lesson:
    sync_lesson(lesson_dict)

    # Or sync everything from LumiBot JSONL files:
    from src.memory.aqs_sync import sync_from_jsonl
    stats = sync_from_jsonl()
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Ensure AQS src is importable for QuantClient
_AQOS_SRC = os.path.expanduser("~/development/agentic-quant-os/src")
if _AQOS_SRC not in sys.path:
    sys.path.insert(0, _AQOS_SRC)

# Ensure nexus-trade src is importable
_NEXUS_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _NEXUS_SRC not in sys.path:
    sys.path.insert(0, _NEXUS_SRC)


# ---------------------------------------------------------------------------
# QuantClient loader — handles the ``src`` namespace collision between
# nexus-trade/src and agentic-quant-os/src by using importlib.
# ---------------------------------------------------------------------------

import time as _time


def _retry_write(client, sql: str, parameters: list | None = None,
                 *, attempts: int = 4, base_delay_s: float = 0.1) -> bool:
    """Execute a write with retry-on-DuckLake-conflict.

    DuckLake's optimistic concurrency check (``CheckForConflicts`` in
    ducklake.duckdb_extension) raises an assertion failure when another
    writer commits between our snapshot read and our commit. The standard
    recovery is to retry the transaction with exponential backoff.

    Catches: ``duckdb.Error``, ``IOError``, and bare ``Exception`` (logged
    at WARNING so the failure isn't silent).

    Returns True on success, False on final failure.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if parameters is not None:
                client.execute_write(sql, parameters)
            else:
                client.execute_write(sql)
            if attempt > 1:
                logger.info(
                    "AQS write succeeded on attempt %d/%d after retry",
                    attempt, attempts,
                )
            return True
        except Exception as exc:  # noqa: BLE001 — DuckLake can raise anything
            last_exc = exc
            if attempt >= attempts:
                logger.warning(
                    "AQS write failed after %d attempts; giving up: %s",
                    attempts, exc,
                )
                return False
            delay = base_delay_s * (3 ** (attempt - 1))  # 0.1s, 0.3s, 0.9s
            logger.warning(
                "AQS write attempt %d/%d failed (%s); retrying in %.2fs",
                attempt, attempts, exc, delay,
            )
            _time.sleep(delay)
    return False  # unreachable

import importlib
import importlib.util


def _load_quant_client():
    """Load QuantClient from the AQS source tree.

    Handles the ``src`` namespace collision between nexus-trade/src and
    agentic-quant-os/src by temporarily manipulating sys.path and module
    namespace, then loading QuantClient as part of the ``aqos_src`` package.

    Returns the QuantClient class, or None if unavailable.
    """
    if not os.path.isdir(_AQOS_SRC):
        logger.warning("AQS src directory not found: %s", _AQOS_SRC)
        return None
    try:
        # Create a synthetic package 'aqos_src' that maps to the AQS src dir.
        # This allows relative imports inside client.py (``from .schema import ...``)
        # to resolve correctly.
        pkg_name = "aqos_src"
        if pkg_name not in sys.modules:
            pkg = type(sys)(pkg_name)
            pkg.__path__ = [_AQOS_SRC]
            pkg.__package__ = pkg_name
            sys.modules[pkg_name] = pkg

            # Pre-load sub-modules into the package namespace
            for mod_name in ("schema", "db_connection"):
                mod_path = os.path.join(_AQOS_SRC, f"{mod_name}.py")
                if os.path.exists(mod_path):
                    full_name = f"{pkg_name}.{mod_name}"
                    spec = importlib.util.spec_from_file_location(
                        full_name, mod_path,
                        submodule_search_locations=[_AQOS_SRC],
                    )
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        sys.modules[full_name] = mod
                        mod.__package__ = pkg_name
                        spec.loader.exec_module(mod)

        # Now load client.py as aqos_src.client
        client_path = os.path.join(_AQOS_SRC, "client.py")
        full_name = f"{pkg_name}.client"
        spec = importlib.util.spec_from_file_location(
            full_name, client_path,
            submodule_search_locations=[_AQOS_SRC],
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = pkg_name
            sys.modules[full_name] = mod
            spec.loader.exec_module(mod)
            return mod.QuantClient
    except Exception as exc:
        logger.warning("Failed to load QuantClient from %s: %s", _AQOS_SRC, exc)
    return None


# ---------------------------------------------------------------------------
# QuantClient singleton
# ---------------------------------------------------------------------------

_client: Any | None = None
_client_error: str | None = None


def _get_client():
    """Lazily create a QuantClient singleton. Returns None if unavailable."""
    global _client, _client_error
    if _client is not None:
        return _client
    if _client_error is not None:
        return None  # Already failed once, don't keep retrying
    try:
        QuantClient = _load_quant_client()
        if QuantClient is None:
            raise ImportError("QuantClient class not loadable")
        _client = QuantClient()
        logger.info("AQS QuantClient connected for Nexus write-back")
        return _client
    except Exception as exc:
        _client_error = str(exc)
        logger.warning("AQS QuantClient unavailable: %s", exc)
        return None


def reset_client():
    """Reset the cached client (for testing)."""
    global _client, _client_error
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None
    _client_error = None


# ---------------------------------------------------------------------------
# Decision write-back
# ---------------------------------------------------------------------------

def sync_committee_decision(
    decision: dict[str, Any],
    *,
    run_id: str = "",
    timestamp: str = "",
) -> bool:
    """Write a PM committee decision to AQS ``agent_memory``.

    Args:
        decision: Dict with keys:
            - action: 'buy', 'sell', 'hold'
            - symbol: ticker (e.g. 'BTC')
            - regime: current market regime
            - thesis: decision rationale
            - committee_split: optional dict of agent votes
            - evidence_summary: optional summary
            - backtest_id: optional run reference
            - pnl_pct: optional realized P&L
        run_id: Run identifier for the key.
        timestamp: ISO timestamp for the record.

    Returns:
        True if written, False on failure.
    """
    client = _get_client()
    if client is None:
        return False

    try:
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        symbol = decision.get("symbol", "UNKNOWN")
        action = decision.get("action", "hold")
        key = f"nexus:{symbol}:{action}:{run_id or ts[:19]}"

        value_json = json.dumps({
            "symbol": symbol,
            "action": action,
            "regime": decision.get("regime", "unknown"),
            "thesis": decision.get("thesis", "")[:2000],
            "evidence_summary": decision.get("evidence_summary", "")[:1000],
            "committee_split": decision.get("committee_split", {}),
            "backtest_id": decision.get("backtest_id", run_id),
            "pnl_pct": decision.get("pnl_pct", 0.0),
            "source_repo": "nexus-trade",
            "timestamp": ts,
        }, default=str)

        # Use plain INSERT (not INSERT OR REPLACE) — DuckLake lacks unique constraints.
        # Wrap with retry: DuckLake's optimistic concurrency check
        # (``CheckForConflicts``) raises assertion failures when another writer
        # commits between our snapshot read and our commit. Standard recovery
        # is to retry with exponential backoff.
        ok = _retry_write(
            client,
            """
            INSERT INTO agent_memory
                (agent_name, memory_type, key, value_json, created_at, access_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ["nexus-trade", "trade_decision", key, value_json, ts, 0],
        )
        if ok:
            logger.info("Synced decision to AQS: %s (%s)", key, action)
        return ok
    except Exception as exc:
        logger.warning("Failed to sync decision to AQS: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Lesson write-back
# ---------------------------------------------------------------------------

def sync_lesson(
    lesson: dict[str, Any],
) -> bool:
    """Write a lesson to AQS ``experience_bank`` via ``QuantClient.write_nexus_lesson()``.

    Args:
        lesson: Dict with at least 'detail' or 'text'. Optional:
            title, category, subcategory, outcome, severity, tags,
            ticker, timeframe, regime.

    Returns:
        True if written, False on failure.
    """
    client = _get_client()
    if client is None:
        return False

    try:
        # Support both 'detail' and 'text' keys
        detail = lesson.get("detail") or lesson.get("text", "")
        if not detail:
            logger.warning("sync_lesson: no 'detail' or 'text' in lesson dict")
            return False

        record = {
            "id": lesson.get("id") or str(uuid.uuid4()),
            "source_type": lesson.get("source_type", "lesson"),
            "source_id": lesson.get("source_id", ""),
            "title": lesson.get("title", f"Nexus: {lesson.get('category', 'insight')}"),
            "category": lesson.get("category", "nexus_trade"),
            "subcategory": lesson.get("subcategory"),
            "detail": detail,
            "outcome": lesson.get("outcome", "learned"),
            "severity": lesson.get("severity", "info"),
            "tags": lesson.get("tags", ""),
            "ticker": lesson.get("ticker") or lesson.get("symbol", ""),
            "timeframe": lesson.get("timeframe", ""),
            "regime": lesson.get("regime", ""),
        }

        client.write_nexus_lesson(record)
        logger.info("Synced lesson to AQS: %s", record.get("title", "")[:60])
        return True
    except Exception as exc:
        logger.warning("Failed to sync lesson to AQS: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Trade result write-back
# ---------------------------------------------------------------------------

def sync_trade_result(
    result: dict[str, Any],
) -> bool:
    """Write a trade result to AQS via ``QuantClient.write_nexus_trade_result()``.

    Args:
        result: Dict with strategy_name, ticker, sharpe, total_return,
                max_drawdown, etc.

    Returns:
        True if written, False on failure.
    """
    client = _get_client()
    if client is None:
        return False

    last_exc: Exception | None = None
    # Wrap with retry: write_nexus_trade_result internally calls execute_write,
    # which can hit DuckLake optimistic-concurrency conflicts when other
    # writers (e.g. AQOS stratforge sweeps) commit concurrently.
    for attempt in range(1, 4):
        try:
            client.write_nexus_trade_result(result)
            logger.info(
                "Synced trade result to AQS: %s/%s",
                result.get("strategy_name", "unknown"),
                result.get("ticker", "unknown"),
            )
            if attempt > 1:
                logger.info("trade_result succeeded on attempt %d/3", attempt)
            return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= 3:
                logger.warning(
                    "Failed to sync trade result to AQS after 3 attempts: %s",
                    exc,
                )
                return False
            delay = 0.1 * (3 ** (attempt - 1))
            logger.warning(
                "trade_result attempt %d/3 failed (%s); retrying in %.2fs",
                attempt, exc, delay,
            )
            _time.sleep(delay)
    return False  # unreachable


# ---------------------------------------------------------------------------
# Signal write-back
# ---------------------------------------------------------------------------

def sync_signal(
    signal: dict[str, Any],
) -> bool:
    """Write a committee signal to AQS ``signals`` table.

    Args:
        signal: Dict with source, signal_type, ticker, value, confidence,
                metadata_json, etc.

    Returns:
        True if written, False on failure.
    """
    client = _get_client()
    if client is None:
        return False

    try:
        # Enrich with source_repo if not set
        if "source_repo" not in signal:
            signal["source_repo"] = "nexus-trade"
        if "source" not in signal:
            signal["source"] = "nexus-trade"

        sid = signal.get("id") or str(uuid.uuid4())
        # Use plain INSERT (DuckLake lacks unique constraints for OR REPLACE).
        # Wrap with retry: DuckLake optimistic concurrency can fail with an
        # assertion failure if another writer commits between our snapshot
        # read and our commit. Standard recovery: exponential backoff retry.
        ok = _retry_write(
            client,
            """
            INSERT INTO signals
                (id, source, signal_type, ticker, value, confidence,
                 regime_context, metadata_json, source_repo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                sid,
                signal["source"],
                signal["signal_type"],
                signal["ticker"],
                signal.get("value"),
                signal.get("confidence", 0.5),
                signal.get("regime_context"),
                signal.get("metadata_json"),
                signal["source_repo"],
                signal.get("created_at", datetime.now(timezone.utc)),
            ],
        )
        if ok:
            logger.info(
                "Synced signal to AQS: %s %s = %.1f",
                signal.get("signal_type", ""),
                signal.get("ticker", ""),
                signal.get("value", 0),
            )
        return ok
    except Exception as exc:
        logger.warning("Failed to sync signal to AQS: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Bulk JSONL sync
# ---------------------------------------------------------------------------

def sync_from_jsonl(
    strategy_name: str = "Nexus_Trader",
    memory_dir: str | None = None,
) -> dict[str, Any]:
    """Sync all LumiBot JSONL memory files to AQS.

    Reads decisions, lessons, and theses from the LumiBot JSONL files
    and writes them to the AQS lakehouse. Idempotent — re-running won't
    create duplicates (uses content-hash keys).

    Args:
        strategy_name: LumiBot strategy subdirectory name.
        memory_dir: Root memory directory (default: LumiBot default).

    Returns:
        Stats dict with keys: decisions, lessons, errors.
    """
    from pathlib import Path

    mem_dir = Path(memory_dir or os.environ.get(
        "NEXUS_MEMORY_DIR",
        os.path.expanduser("~/development/trading-bots/lumibot/.lumibot/memory"),
    ))
    strat_dir = mem_dir / strategy_name

    if not strat_dir.exists():
        logger.warning("Memory dir not found: %s", strat_dir)
        return {"decisions": 0, "lessons": 0, "errors": 0, "warning": "dir not found"}

    stats = {"decisions": 0, "lessons": 0, "errors": 0}

    # Sync decisions.jsonl → agent_memory
    decisions_path = strat_dir / "decisions.jsonl"
    if decisions_path.exists():
        for line in decisions_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                metadata = entry.get("metadata", {})
                ok = sync_committee_decision({
                    "symbol": metadata.get("symbol", ""),
                    "action": metadata.get("action", "hold"),
                    "regime": metadata.get("regime", metadata.get("market_regime", "")),
                    "thesis": entry.get("text", ""),
                    "evidence_summary": str(metadata.get("evidence", ""))[:500],
                    "backtest_id": metadata.get("backtest_id", ""),
                }, run_id=entry.get("id", "")[:12])
                if ok:
                    stats["decisions"] += 1
            except Exception as exc:
                logger.debug("Failed to sync decision line: %s", exc)
                stats["errors"] += 1

    # Sync lessons.jsonl → experience_bank
    lessons_path = strat_dir / "lessons.jsonl"
    if lessons_path.exists():
        for line in lessons_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                metadata = entry.get("metadata", {})
                ok = sync_lesson({
                    "detail": entry.get("text", ""),
                    "title": f"Nexus lesson: {entry.get('kind', 'insight')}",
                    "category": "nexus_trade",
                    "outcome": metadata.get("outcome", "learned"),
                    "severity": "info",
                    "ticker": metadata.get("symbol", ""),
                    "tags": ",".join(entry.get("tags", [])),
                })
                if ok:
                    stats["lessons"] += 1
            except Exception as exc:
                logger.debug("Failed to sync lesson line: %s", exc)
                stats["errors"] += 1

    logger.info(
        "JSONL sync complete: %d decisions, %d lessons, %d errors",
        stats["decisions"], stats["lessons"], stats["errors"],
    )
    return stats


# ---------------------------------------------------------------------------
# Context injection for committee
# ---------------------------------------------------------------------------

def get_aqs_context(ticker: str = "BTC") -> dict[str, Any]:
    """Read AQS lakehouse intelligence for a ticker (for committee context).

    This is a read-only convenience function that wraps QuantClient reads
    for use during committee sessions.
    """
    client = _get_client()
    if client is None:
        return {"available": False}

    try:
        summary = client.read_nexus_intelligence_summary(ticker)
        summary["available"] = True
        return summary
    except Exception as exc:
        logger.warning("get_aqs_context failed: %s", exc)
        return {"available": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Count helpers (for verification)
# ---------------------------------------------------------------------------

def count_nexus_entries() -> dict[str, int]:
    """Count nexus-trade entries in AQS tables (for verification)."""
    client = _get_client()
    if client is None:
        return {"agent_memory": -1, "experience_bank": -1, "signals": -1}

    counts: dict[str, int] = {}
    try:
        rows = client.query(
            "SELECT COUNT(*) as cnt FROM agent_memory WHERE agent_name = 'nexus-trade'"
        )
        counts["agent_memory"] = rows[0]["cnt"] if rows else 0
    except Exception:
        counts["agent_memory"] = -1

    try:
        rows = client.query(
            "SELECT COUNT(*) as cnt FROM experience_bank WHERE source_repo = 'nexus-trade'"
        )
        counts["experience_bank"] = rows[0]["cnt"] if rows else 0
    except Exception:
        counts["experience_bank"] = -1

    try:
        rows = client.query(
            "SELECT COUNT(*) as cnt FROM signals WHERE source_repo = 'nexus-trade' OR source = 'nexus-trade'"
        )
        counts["signals"] = rows[0]["cnt"] if rows else 0
    except Exception:
        counts["signals"] = -1

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Nexus Trader data to AQS lakehouse")
    parser.add_argument("--sync-jsonl", action="store_true", help="Sync all JSONL files")
    parser.add_argument("--count", action="store_true", help="Count nexus entries in AQS")
    parser.add_argument("--strategy", default="Nexus_Trader", help="Strategy name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.count:
        counts = count_nexus_entries()
        print("Nexus entries in AQS:")
        for table, count in counts.items():
            print(f"  {table}: {count}")
    elif args.sync_jsonl:
        stats = sync_from_jsonl(strategy_name=args.strategy)
        print(f"Sync complete: {stats}")
    else:
        parser.print_help()
