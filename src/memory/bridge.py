"""
Memory Bridge — LumiBot JSONL ↔ Nexus Trader Vector Memory
==========================================================

Parses LumiBot's JSONL memory files (decisions.jsonl, lessons.jsonl,
theses.jsonl, memories.jsonl) and bridges them into the Nexus Trader
LanceDB vector store for semantic search.

Usage
-----
    python -m nexus_trade.memory.bridge [--strategy Nexus_Trader] [--dry-run]

Or programmatically:

    from nexus_trade.memory.bridge import MemoryBridge
    bridge = MemoryBridge("NexusCommitteeStrategy")
    stats = bridge.sync_all()

Key design principles
---------------------
* **Idempotent** — deduplicates by ``id``; re-running the bridge
  does not create duplicates.
* **Graceful** — handles missing files, empty JSONL, malformed entries
  without crashing.
* **Batch embedding** — uses the batch API for efficiency.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Best-effort .env loader (module-level so it runs at import, before any
# downstream module captures NEXUS_LANCEDB_DIR). Uses a tiny manual parser
# to avoid adding a python-dotenv dependency. Existing env vars win
# (setdefault semantics).
# ---------------------------------------------------------------------------

def _load_nexus_env() -> None:
    _env_path = Path(__file__).resolve().parents[2] / ".env"
    if not _env_path.exists():
        return
    try:
        for _line in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip()
            if " #" in _v:
                _v = _v.split(" #", 1)[0].strip()
            _v = _v.strip('"').strip("'")
            os.environ.setdefault(_k, _v)
    except OSError:
        pass

_load_nexus_env()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MEMORY_DIR: str = os.environ.get(
    "NEXUS_MEMORY_DIR",
    os.path.expanduser("~/development/trading-bots/lumibot/.lumibot/memory"),
)


class MemoryBridge:
    """Bridges LumiBot JSONL memory files to Nexus Trader vector memory.

    Parameters
    ----------
    strategy_name : str
        The LumiBot strategy name (subdirectory under ``.lumibot/memory/``).
    memory_dir : str | None
        Root directory for LumiBot memory files.  Defaults to
        ``~/.lumibot/memory/`` via ``NEXUS_MEMORY_DIR`` env var.
    nexus_memory : Any | None
        Pre-existing vector memory instance.  Created lazily if None.
    """

    def __init__(
        self,
        strategy_name: str = "NexusCommitteeStrategy",
        memory_dir: str | None = None,
        nexus_memory = None,
    ) -> None:
        # .env loading now happens at module import time (see top of
        # file), so NEXUS_LANCEDB_DIR is in os.environ before any
        # downstream module captures its _DEFAULT_PERSIST_DIR.
        self.strategy_name = strategy_name
        self.memory_dir = Path(memory_dir or _DEFAULT_MEMORY_DIR)
        self.strategy_dir = self.memory_dir / strategy_name
        self._nexus_memory = nexus_memory

    @property
    def nexus_memory(self):
        """Lazy-initialized NexusVectorMemory instance."""
        if self._nexus_memory is None:
            from .nexus_vector_memory import get_nexus_memory
            self._nexus_memory = get_nexus_memory()
        return self._nexus_memory

    # ------------------------------------------------------------------
    # File readers
    # ------------------------------------------------------------------

    def _read_jsonl(self, filename: str) -> list[dict[str, Any]]:
        """Read and parse a JSONL file, returning a list of dicts.

        Handles missing files, empty files, and malformed lines gracefully.
        """
        path = self.strategy_dir / filename
        if not path.exists():
            logger.debug("JSONL file not found: %s", path)
            return []

        entries: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping malformed JSON in %s line %d: %.80s...",
                            filename, line_num, line,
                        )
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return []

        logger.debug("Read %d entries from %s", len(entries), filename)
        return entries

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _deduplicate(
        self,
        entries: list[dict[str, Any]],
        existing_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Deduplicate a list of entries by their ``id`` field.

        Parameters
        ----------
        entries : list[dict]
            Entries to deduplicate.
        existing_ids : set[str] | None
            Already-seen IDs to also skip.  Updated in-place.

        Returns
        -------
        list[dict]
            Entries with unique IDs, in original order.
        """
        seen: set[str] = existing_ids or set()
        result: list[dict[str, Any]] = []

        for entry in entries:
            eid = entry.get("id", "")
            if not eid:
                logger.debug("Skipping entry without ID: %.80s", str(entry)[:80])
                continue
            if eid in seen:
                logger.debug("Skipping duplicate ID: %s", eid)
                continue
            seen.add(eid)
            result.append(entry)

        return result

    # ------------------------------------------------------------------
    # Sync methods
    # ------------------------------------------------------------------

    def sync_decisions(self) -> dict[str, int]:
        """Parse decisions.jsonl and bridge to vector memory.

        Maps LumiBot decision entries to :class:`NexusDecisionRecord`
        and batch-stores them.

        Returns
        -------
        dict[str, int]
            Keys: ``read``, ``deduped``, ``embedded``, ``skipped``, ``errors``.
        """
        entries = self._read_jsonl("decisions.jsonl")
        stats = {"read": len(entries), "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0}

        if not entries:
            logger.info("No decisions to bridge from %s", self.strategy_dir / "decisions.jsonl")
            return stats

        # Deduplicate within the file
        unique = self._deduplicate(entries)
        stats["deduped"] = len(unique)

        # Map to decision dicts for vector storage
        decisions: list[dict[str, Any]] = []
        for entry in unique:
            try:
                kind = entry.get("kind", "decision")
                if kind not in ("decision",):
                    # Skip thesis updates etc — those go to their own bridge
                    continue

                metadata = entry.get("metadata", {})
                decisions.append({
                    "id": entry["id"],
                    "symbol": metadata.get("symbol", ""),
                    "action": metadata.get("action", "hold"),
                    "regime": metadata.get("regime", metadata.get("market_regime", "unknown")),
                    "thesis_summary": metadata.get("thesis_summary", entry.get("text", ""))[:500],
                    "indicators_snapshot": json.dumps(metadata.get("indicators", {})) if metadata.get("indicators") else "{}",
                    "outcome": metadata.get("outcome", "pending"),
                    "pnl_pct": float(metadata.get("pnl_pct", metadata.get("pnl", 0))),
                    "timestamp": entry.get("timestamp", ""),
                    "strategy_name": entry.get("strategy", self.strategy_name),
                    "backtest_id": metadata.get("backtest_id", ""),
                })
            except Exception:
                logger.exception("Failed to map decision entry: %.80s", str(entry)[:80])
                stats["errors"] += 1

        if decisions:
            result = self.nexus_memory.batch_store_decisions(decisions)
            stats["embedded"] = result.get("embedded", 0)
            stats["skipped"] = result.get("skipped", 0)
            stats["errors"] += result.get("errors", 0)

        logger.info("Synced decisions: %s", stats)
        return stats

    def sync_lessons(self) -> dict[str, int]:
        """Parse lessons.jsonl and bridge to vector memory.

        Returns
        -------
        dict[str, int]
            Keys: ``read``, ``deduped``, ``embedded``, ``skipped``, ``errors``.
        """
        entries = self._read_jsonl("lessons.jsonl")
        stats = {"read": len(entries), "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0}

        if not entries:
            logger.info("No lessons to bridge")
            return stats

        unique = self._deduplicate(entries)
        stats["deduped"] = len(unique)

        lessons: list[dict[str, Any]] = []
        for entry in unique:
            try:
                tags = entry.get("tags", [])
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except json.JSONDecodeError:
                        tags = [t.strip() for t in tags.split(",")]

                lessons.append({
                    "id": entry["id"],
                    "text": entry.get("text", ""),
                    "symbol": entry.get("metadata", {}).get("symbol", ""),
                    "regime": entry.get("metadata", {}).get("regime", ""),
                    "category": entry.get("kind", "insight"),
                    "severity": entry.get("metadata", {}).get("severity", "info"),
                    "tags": tags,
                    "timestamp": entry.get("timestamp", ""),
                    "strategy_name": entry.get("strategy", self.strategy_name),
                    "source": "bridge",
                })
            except Exception:
                logger.exception("Failed to map lesson entry: %.80s", str(entry)[:80])
                stats["errors"] += 1

        for lesson in lessons:
            if self.nexus_memory.store_lesson(lesson):
                stats["embedded"] += 1
            else:
                stats["errors"] += 1

        logger.info("Synced lessons: %s", stats)
        return stats

    def sync_theses(self) -> dict[str, int]:
        """Parse theses.jsonl and store as lessons (category='thesis').

        LumiBot theses contain thesis, thesis_update, and thesis_close
        entries.  We store these as lessons with category='thesis' for
        future context retrieval.

        Returns
        -------
        dict[str, int]
            Keys: ``read``, ``deduped``, ``embedded``, ``skipped``, ``errors``.
        """
        entries = self._read_jsonl("theses.jsonl")
        stats = {"read": len(entries), "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0}

        if not entries:
            logger.info("No theses to bridge")
            return stats

        unique = self._deduplicate(entries)
        stats["deduped"] = len(unique)

        theses: list[dict[str, Any]] = []
        for entry in unique:
            try:
                kind = entry.get("kind", "thesis")
                # Map kind to category
                category_map = {
                    "thesis": "thesis",
                    "thesis_update": "thesis",
                    "thesis_close": "thesis",
                    "memory": "insight",
                }
                category = category_map.get(kind, "insight")

                tags = entry.get("tags", [])
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except json.JSONDecodeError:
                        tags = [t.strip() for t in tags.split(",")]

                theses.append({
                    "id": entry["id"],
                    "text": entry.get("text", ""),
                    "symbol": entry.get("metadata", {}).get("symbol", ""),
                    "regime": entry.get("metadata", {}).get("regime", ""),
                    "category": category,
                    "severity": "info",
                    "tags": tags,
                    "timestamp": entry.get("timestamp", ""),
                    "strategy_name": entry.get("strategy", self.strategy_name),
                    "source": "bridge-theses",
                })
            except Exception:
                logger.exception("Failed to map thesis entry: %.80s", str(entry)[:80])
                stats["errors"] += 1

        for thesis in theses:
            if self.nexus_memory.store_lesson(thesis):
                stats["embedded"] += 1
            else:
                stats["errors"] += 1

        logger.info("Synced theses: %s", stats)
        return stats

    def sync_memories(self) -> dict[str, int]:
        """Parse memories.jsonl and store as lessons (category='insight').

        Returns
        -------
        dict[str, int]
            Keys: ``read``, ``deduped``, ``embedded``, ``skipped``, ``errors``.
        """
        entries = self._read_jsonl("memories.jsonl")
        stats = {"read": len(entries), "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0}

        if not entries:
            logger.info("No memories to bridge")
            return stats

        unique = self._deduplicate(entries)
        stats["deduped"] = len(unique)

        memories: list[dict[str, Any]] = []
        for entry in unique:
            try:
                tags = entry.get("tags", [])
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except json.JSONDecodeError:
                        tags = [t.strip() for t in tags.split(",")]

                memories.append({
                    "id": entry["id"],
                    "text": entry.get("text", ""),
                    "symbol": entry.get("metadata", {}).get("symbol", ""),
                    "regime": entry.get("metadata", {}).get("regime", ""),
                    "category": "insight",
                    "severity": entry.get("metadata", {}).get("severity", "info"),
                    "tags": tags,
                    "timestamp": entry.get("timestamp", ""),
                    "strategy_name": entry.get("strategy", self.strategy_name),
                    "source": "bridge-memories",
                })
            except Exception:
                logger.exception("Failed to map memory entry: %.80s", str(entry)[:80])
                stats["errors"] += 1

        for memory in memories:
            if self.nexus_memory.store_lesson(memory):
                stats["embedded"] += 1
            else:
                stats["errors"] += 1

        logger.info("Synced memories: %s", stats)
        return stats

    def sync_all(self) -> dict[str, Any]:
        """Run all sync operations — decisions, lessons, theses, memories.

        Returns
        -------
        dict
            Keys: ``decisions``, ``lessons``, ``theses``, ``memories``,
            each a stats dict; plus ``nexus_stats`` for overall vector
            memory state.
        """
        if not self.strategy_dir.exists():
            logger.warning(
                "Memory directory does not exist: %s — nothing to bridge",
                self.strategy_dir,
            )
            return {
                "decisions": {"read": 0, "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0},
                "lessons": {"read": 0, "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0},
                "theses": {"read": 0, "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0},
                "memories": {"read": 0, "deduped": 0, "embedded": 0, "skipped": 0, "errors": 0},
                "nexus_stats": self.nexus_memory.get_stats(),
                "warning": f"Directory not found: {self.strategy_dir}",
            }

        result = {
            "decisions": self.sync_decisions(),
            "lessons": self.sync_lessons(),
            "theses": self.sync_theses(),
            "memories": self.sync_memories(),
            "nexus_stats": self.nexus_memory.get_stats(),
        }

        total_embedded = sum(
            result[k].get("embedded", 0)
            for k in ("decisions", "lessons", "theses", "memories")
        )
        logger.info(
            "Bridge sync complete: %d total entries embedded into vector memory",
            total_embedded,
        )
        return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Bridge LumiBot JSONL memory to Nexus Trader vector memory",
    )
    parser.add_argument(
        "--strategy", "-s",
        default="Nexus_Trader",
        help="Strategy name (subdirectory under .lumibot/memory/)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Count entries but don't embed/store anything",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    bridge = MemoryBridge(strategy_name=args.strategy)

    if args.dry_run:
        for filename in ("decisions.jsonl", "lessons.jsonl", "theses.jsonl", "memories.jsonl"):
            entries = bridge._read_jsonl(filename)
            if entries:
                unique = bridge._deduplicate(entries)
                print(f"{filename}: {len(entries)} read, {len(unique)} unique ready to bridge")
            else:
                print(f"{filename}: no entries")
    else:
        stats = bridge.sync_all()
        print(json.dumps(stats, indent=2, default=str))
