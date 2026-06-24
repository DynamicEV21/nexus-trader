"""
AQS Writer — Write Nexus decisions/lessons/trade results back to AQS
====================================================================

Writes PM decisions, lessons, and trade results from the Nexus committee
into the AQS DuckDB lakehouse (both DuckLake and the quant.duckdb file)
so that other AQS agents (alpha-factory, regime-intelligence, etc.) can
query what Nexus decided.

Write targets:
  1. **DuckLake** (PostgreSQL catalog + Parquet) — the live AQS data store
  2. **quant.duckdb** (standalone file) — what Nexus reads from

Both are kept in sync so that writes are immediately visible to all readers.

Usage::

    from src.memory.aqs_writer import AQSWriter

    writer = AQSWriter()
    writer.write_decision({
        "symbol": "BTC",
        "action": "hold",
        "regime": "trending_up",
        "thesis_summary": "...",
        "committee_split": {"bull": "conditional_long", "bear": "veto", "pm": "hold"},
    })
    writer.close()

Or use the convenience function::

    from src.memory.aqs_writer import write_decision_to_aqs
    write_decision_to_aqs(symbol="BTC", action="hold", ...)
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

# ── Paths ─────────────────────────────────────────────────────────────

_AQOS_SRC = os.path.expanduser("~/development/agentic-quant-os/src")
_QUANT_DB_PATH = os.path.expanduser(
    os.environ.get(
        "NEXUS_LAKEHOUSE_PATH",
        "~/development/agentic-quant-os/data/quant.duckdb",
    )
)


class AQSWriter:
    """Writes Nexus decisions, lessons, and trade results to AQS.

    Writes to both DuckLake (via QuantClient / db_connection) and the
    quant.duckdb standalone file. Best-effort: if DuckLake is unavailable,
    falls back to quant.duckdb only.
    """

    def __init__(self, quant_db_path: str = _QUANT_DB_PATH) -> None:
        self._quant_db_path = quant_db_path
        self._ducklake_con: Any = None
        self._file_con: Any = None
        self._agent_name = "nexus-trade.Nexus_Trader"
        self._source_repo = "nexus-trade"

    # ── Connection management ──────────────────────────────────────

    def _get_ducklake(self) -> Any:
        """Get a DuckLake write connection (lazy, best-effort).

        Handles the ``src`` namespace collision between nexus-trade/src and
        agentic-quant-os/src by using importlib to load the AQS db_connection
        module as part of a synthetic ``aqos_src`` package.
        """
        if self._ducklake_con is not None:
            return self._ducklake_con
        try:
            import importlib.util

            pkg_name = "aqos_src_writer"
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

            # Now import db_connection from the synthetic package
            full_name = f"{pkg_name}.db_connection"
            if full_name in sys.modules:
                db_mod = sys.modules[full_name]
            else:
                mod_path = os.path.join(_AQOS_SRC, "db_connection.py")
                spec = importlib.util.spec_from_file_location(
                    full_name, mod_path,
                    submodule_search_locations=[_AQOS_SRC],
                )
                if spec and spec.loader:
                    db_mod = importlib.util.module_from_spec(spec)
                    sys.modules[full_name] = db_mod
                    db_mod.__package__ = pkg_name
                    spec.loader.exec_module(db_mod)
                else:
                    raise ImportError(f"Cannot load {mod_path}")

            self._ducklake_con = db_mod.get_write_connection()
            logger.debug("DuckLake connection established")
        except Exception as exc:
            logger.warning("DuckLake connection failed: %s — falling back to file only", exc)
            self._ducklake_con = None
        return self._ducklake_con

    def _get_file_con(self) -> Any:
        """Get a read-write connection to quant.duckdb.

        NOTE: DuckDB only allows ONE connection per file per process. If
        ``NexusLakehouseReader`` already has a read-only connection open
        for this file, the read-only connection blocks our write. We close
        any active reader connection first via ``_close_active_reader()``.
        """
        if self._file_con is not None:
            try:
                self._file_con.execute("SELECT 1")
                return self._file_con
            except Exception:
                try:
                    self._file_con.close()
                except Exception:
                    pass
                self._file_con = None
        try:
            # Close any active read-only lakehouse reader connection
            # before opening a write connection to the same file.
            self._close_active_reader()
            import duckdb

            self._file_con = duckdb.connect(self._quant_db_path, read_only=False)
            logger.debug("quant.duckdb write connection established: %s", self._quant_db_path)
        except Exception as exc:
            logger.warning("quant.duckdb connection failed: %s", exc)
            self._file_con = None
        return self._file_con

    def _close_active_reader(self) -> None:
        """Close any active ``NexusLakehouseReader`` read-only connection.

        DuckDB enforces single-writer-multiple-readers per file, but only
        ONE connection per process. To allow writes to ``quant.duckdb``
        while a reader is open in the same process, we need to release the
        reader's connection first.
        """
        try:
            from src.lakehouse.reader import _READERS  # type: ignore
        except Exception:
            _READERS = None  # type: ignore
        try:
            # Lazy import to avoid circular dependency
            import src.lakehouse.reader as _reader_mod
            for inst in getattr(_reader_mod, "_READERS", []):
                if inst is not None:
                    try:
                        inst.close()
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Write helpers ──────────────────────────────────────────────

    def _write_agent_memory(
        self,
        key: str,
        memory_type: str,
        value: dict[str, Any],
    ) -> bool:
        """Write a key-value record to agent_memory in both DuckLake and quant.duckdb.

        Uses DELETE + INSERT pattern to be idempotent (no UNIQUE constraint
        in DuckLake schema).
        """
        now = datetime.now(timezone.utc)
        value_json = json.dumps(value, default=str)
        success = True

        # DuckLake write
        dl = self._get_ducklake()
        if dl is not None:
            try:
                dl.execute(
                    "DELETE FROM agent_memory WHERE agent_name = ? AND key = ?",
                    [self._agent_name, key],
                )
                dl.execute(
                    """
                    INSERT INTO agent_memory
                        (agent_name, memory_type, key, value_json, created_at, accessed_at, access_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [self._agent_name, memory_type, key, value_json, now, None, 0],
                )
                logger.debug("DuckLake agent_memory write OK: %s", key)
            except Exception as exc:
                logger.warning("DuckLake agent_memory write failed: %s", exc)
                success = False

        # quant.duckdb write
        fc = self._get_file_con()
        if fc is not None:
            try:
                fc.execute(
                    "DELETE FROM agent_memory WHERE agent_name = ? AND key = ?",
                    [self._agent_name, key],
                )
                fc.execute(
                    """
                    INSERT INTO agent_memory
                        (agent_name, memory_type, key, value_json, created_at, accessed_at, access_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [self._agent_name, memory_type, key, value_json, now, None, 0],
                )
                logger.debug("quant.duckdb agent_memory write OK: %s", key)
            except Exception as exc:
                logger.warning("quant.duckdb agent_memory write failed: %s", exc)
                success = False

        return success

    def _write_experience(
        self,
        detail: str,
        title: str = "",
        category: str = "nexus_trade",
        outcome: str = "info",
        severity: str = "info",
        tags: str = "",
        ticker: str = "",
        timeframe: str = "",
        regime: str = "",
    ) -> Optional[str]:
        """Write a lesson to experience_bank in both DuckLake and quant.duckdb.

        Returns the entry ID, or None on failure.
        """
        eid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        sql = """
            INSERT INTO experience_bank
                (id, source_type, source_id, title, category, subcategory,
                 detail, outcome, severity, tags, agent, ticker, timeframe,
                 regime, source_repo, created_at, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            eid, "lesson", None, title or detail[:80], category, None,
            detail, outcome, severity, tags,
            "nexus-trade", ticker, timeframe, regime,
            self._source_repo, now, now,
        ]

        success = True

        dl = self._get_ducklake()
        if dl is not None:
            try:
                dl.execute(sql, params)
            except Exception as exc:
                logger.warning("DuckLake experience_bank write failed: %s", exc)
                success = False

        fc = self._get_file_con()
        if fc is not None:
            try:
                fc.execute(sql, params)
            except Exception as exc:
                logger.warning("quant.duckdb experience_bank write failed: %s", exc)
                success = False

        return eid if success else None

    def _write_signal(
        self,
        ticker: str,
        value: float,
        signal_type: str = "committee_decision",
        confidence: float = 0.5,
        regime_context: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Optional[str]:
        """Write a signal to the signals table."""
        sid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        metadata_json = json.dumps(metadata or {}, default=str)

        sql = """
            INSERT INTO signals
                (id, source, signal_type, ticker, value, confidence,
                 regime_context, metadata_json, source_repo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            sid, "nexus-trade", signal_type, ticker, value,
            confidence, regime_context or None, metadata_json,
            self._source_repo, now,
        ]

        success = True

        dl = self._get_ducklake()
        if dl is not None:
            try:
                dl.execute(sql, params)
            except Exception as exc:
                logger.warning("DuckLake signal write failed: %s", exc)
                success = False

        fc = self._get_file_con()
        if fc is not None:
            try:
                fc.execute(sql, params)
            except Exception as exc:
                logger.warning("quant.duckdb signal write failed: %s", exc)
                success = False

        return sid if success else None

    # ── Public API ─────────────────────────────────────────────────

    def write_decision(
        self,
        symbol: str,
        action: str,
        regime: str = "unknown",
        thesis_summary: str = "",
        committee_split: dict[str, Any] | None = None,
        evidence_summary: str = "",
        run_id: str = "",
        backtest_id: str = "",
        timestamp: str | None = None,
    ) -> bool:
        """Write a PM trade decision to AQS agent_memory.

        Args:
            symbol: Ticker symbol (e.g. 'BTC').
            action: 'buy', 'sell', or 'hold'.
            regime: Current market regime.
            thesis_summary: One-paragraph thesis from the PM.
            committee_split: Dict of agent → vote/action.
            evidence_summary: Summary of evidence pack.
            run_id: Committee run identifier.
            backtest_id: Backtest identifier (if applicable).
            timestamp: ISO timestamp (defaults to now).

        Returns True if at least one write target succeeded.
        """
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        key = f"nexus-decision-{symbol}-{ts.replace(':', '').replace('.', '')}"

        value = {
            "symbol": symbol,
            "action": action,
            "regime": regime,
            "thesis_summary": thesis_summary[:1000],
            "committee_split": committee_split or {},
            "evidence_summary": evidence_summary[:500],
            "run_id": run_id,
            "backtest_id": backtest_id,
            "timestamp": ts,
            "source_repo": self._source_repo,
        }

        success = self._write_agent_memory(key, "trade_decision", value)

        # Also write a signal for other agents to consume
        signal_value = 1.0 if action == "buy" else (-1.0 if action == "sell" else 0.0)
        self._write_signal(
            ticker=symbol,
            value=signal_value,
            signal_type="committee_decision",
            confidence=0.7 if action != "hold" else 0.4,
            regime_context=regime,
            metadata={
                "committee_split": committee_split,
                "thesis": thesis_summary[:300],
                "run_id": run_id,
            },
        )

        if success:
            logger.info(
                "Wrote decision to AQS: %s %s (%s) — key=%s",
                symbol, action, regime, key,
            )

        return success

    def write_lesson(
        self,
        text: str,
        symbol: str = "",
        regime: str = "",
        outcome: str = "learned",
        severity: str = "info",
        tags: str = "",
        category: str = "nexus_trade",
        title: str = "",
    ) -> Optional[str]:
        """Write a trading lesson to AQS experience_bank and agent_memory.

        Args:
            text: The lesson text.
            symbol: Related ticker.
            regime: Market regime.
            outcome: 'learned', 'win', 'loss', 'neutral'.
            severity: 'info', 'warning', 'critical'.
            tags: Comma-separated tags.
            category: Lesson category.
            title: Optional title.

        Returns the experience_bank entry ID, or None on failure.
        """
        # Write to experience_bank
        eid = self._write_experience(
            detail=text,
            title=title or f"Nexus lesson: {symbol} {regime}",
            category=category,
            outcome=outcome,
            severity=severity,
            tags=tags,
            ticker=symbol,
            regime=regime,
        )

        # Also store in agent_memory for easy retrieval
        key = f"nexus-lesson-{uuid.uuid4().hex[:12]}"
        self._write_agent_memory(key, "lesson", {
            "text": text[:2000],
            "symbol": symbol,
            "regime": regime,
            "outcome": outcome,
            "severity": severity,
            "tags": tags,
            "title": title,
        })

        if eid:
            logger.info("Wrote lesson to AQS: %s (eid=%s)", title or text[:60], eid)

        return eid

    def write_trade_result(
        self,
        symbol: str,
        action: str,
        pnl_pct: float = 0.0,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        regime_at_entry: str = "",
        regime_at_exit: str = "",
        thesis: str = "",
        lesson: str = "",
        run_id: str = "",
    ) -> bool:
        """Write a closed trade result to AQS.

        Args:
            symbol: Ticker symbol.
            action: 'buy' or 'sell'.
            pnl_pct: P&L percentage.
            entry_price: Entry price.
            exit_price: Exit price.
            regime_at_entry: Market regime when trade was opened.
            regime_at_exit: Market regime when trade was closed.
            thesis: Original thesis.
            lesson: What was learned.
            run_id: Committee run identifier.

        Returns True if successful.
        """
        key = f"nexus-trade-result-{symbol}-{run_id}-{uuid.uuid4().hex[:8]}"

        value = {
            "symbol": symbol,
            "action": action,
            "pnl_pct": pnl_pct,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "regime_at_entry": regime_at_entry,
            "regime_at_exit": regime_at_exit,
            "thesis": thesis[:500],
            "lesson": lesson[:500],
            "run_id": run_id,
            "outcome": "win" if pnl_pct > 0 else "loss",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        success = self._write_agent_memory(key, "trade_result", value)

        # Also write a lesson if there's something to learn
        if lesson:
            self._write_experience(
                detail=lesson,
                title=f"Trade result: {symbol} {action} ({pnl_pct:+.1f}%)",
                category="nexus_trade",
                outcome="win" if pnl_pct > 0 else "loss",
                severity="info" if pnl_pct >= 0 else "warning",
                tags=f"{symbol},{regime_at_entry}",
                ticker=symbol,
                regime=regime_at_exit or regime_at_entry,
            )

        if success:
            logger.info(
                "Wrote trade result to AQS: %s %s pnl=%.1f%% — key=%s",
                symbol, action, pnl_pct, key,
            )

        return success

    # ── Sync from JSONL ────────────────────────────────────────────

    def sync_from_jsonl(
        self,
        strategy_name: str = "Nexus_Trader",
        memory_dir: str | None = None,
    ) -> dict[str, int]:
        """Sync decisions from LumiBot JSONL files to AQS agent_memory.

        Reads decisions.jsonl, lessons.jsonl from the LumiBot memory directory
        and writes each entry to AQS. Idempotent: uses deterministic keys
        based on the JSONL entry ID.

        Returns stats dict.
        """
        import pathlib

        mem_dir = pathlib.Path(
            memory_dir
            or os.environ.get(
                "NEXUS_MEMORY_DIR",
                os.path.expanduser("~/development/trading-bots/lumibot/.lumibot/memory"),
            )
        )
        strat_dir = mem_dir / strategy_name

        stats = {"decisions": 0, "lessons": 0, "errors": 0}

        # Sync decisions
        decisions_file = strat_dir / "decisions.jsonl"
        if decisions_file.exists():
            with decisions_file.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        meta = entry.get("metadata", {})
                        key = f"nexus-decision-jsonl-{entry.get('id', '')}"

                        self._write_agent_memory(key, "trade_decision", {
                            "symbol": meta.get("symbol", ""),
                            "action": meta.get("action", "hold"),
                            "regime": meta.get("regime", meta.get("market_regime", "unknown")),
                            "thesis_summary": entry.get("text", "")[:1000],
                            "evidence": meta.get("evidence", {}),
                            "timestamp": entry.get("timestamp", ""),
                            "source": "jsonl_sync",
                            "jsonl_id": entry.get("id", ""),
                        })
                        stats["decisions"] += 1
                    except Exception as exc:
                        logger.warning("Failed to sync decision: %s", exc)
                        stats["errors"] += 1

        # Sync lessons
        lessons_file = strat_dir / "lessons.jsonl"
        if lessons_file.exists():
            with lessons_file.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        meta = entry.get("metadata", {})
                        key = f"nexus-lesson-jsonl-{entry.get('id', '')}"

                        self._write_agent_memory(key, "lesson", {
                            "text": entry.get("text", "")[:2000],
                            "symbol": meta.get("symbol", ""),
                            "regime": meta.get("regime", ""),
                            "outcome": meta.get("outcome", "learned"),
                            "tags": entry.get("tags", []),
                            "timestamp": entry.get("timestamp", ""),
                            "source": "jsonl_sync",
                            "jsonl_id": entry.get("id", ""),
                        })
                        stats["lessons"] += 1
                    except Exception as exc:
                        logger.warning("Failed to sync lesson: %s", exc)
                        stats["errors"] += 1

        logger.info(
            "JSONL sync complete: %d decisions, %d lessons, %d errors",
            stats["decisions"], stats["lessons"], stats["errors"],
        )
        return stats

    # ── Lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close connections."""
        # DuckLake connections are pooled — don't actually close
        if self._file_con is not None:
            try:
                self._file_con.close()
            except Exception:
                pass
            self._file_con = None

    def __enter__(self) -> "AQSWriter":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

_writer: AQSWriter | None = None


def get_aqs_writer() -> AQSWriter:
    """Return the module-level singleton AQSWriter."""
    global _writer
    if _writer is None:
        _writer = AQSWriter()
    return _writer


def write_decision_to_aqs(
    symbol: str,
    action: str,
    regime: str = "unknown",
    thesis_summary: str = "",
    committee_split: dict[str, Any] | None = None,
    evidence_summary: str = "",
    run_id: str = "",
    **kwargs: Any,
) -> bool:
    """Convenience function to write a PM decision to AQS.

    Usage in nexus_committee.py::

        from src.memory.aqs_writer import write_decision_to_aqs

        write_decision_to_aqs(
            symbol="BTC",
            action="hold",
            regime="trending_up",
            thesis_summary=summary,
            committee_split={"bull": ..., "bear": ..., "pm": ...},
            run_id=f"run-{self.vars.committee_run_count}",
        )
    """
    writer = get_aqs_writer()
    return writer.write_decision(
        symbol=symbol,
        action=action,
        regime=regime,
        thesis_summary=thesis_summary,
        committee_split=committee_split,
        evidence_summary=evidence_summary,
        run_id=run_id,
        **kwargs,
    )
