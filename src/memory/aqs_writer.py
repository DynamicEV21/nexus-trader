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

        Also runs the nexus view migration on first connection: ensures
        the 4 nexus curated views (``v_nexus_*``) exist in quant.duckdb
        so the reader doesn't log "view does not exist" warnings. The
        migration is idempotent (CREATE OR REPLACE) and silent if the
        underlying tables don't exist yet.
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
            # Run view migration on this writable connection (one-shot,
            # idempotent). Lazy import to avoid circular dep.
            try:
                from src.lakehouse.view_migration import ensure_views
                ensure_views(self._file_con)
            except Exception as exc:
                logger.debug("View migration skipped: %s", exc)
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

    def _execute_with_retry(
        self,
        con: Any,
        sql: str,
        params: list[Any],
        target: str,
        reset_fn: Any,
    ) -> bool:
        """Execute SQL with one retry on intermittent connection errors.

        Workaround for DuckLake v1.5.3 commit NULL bug and other transient
        extension errors. On failure, the caller-provided ``reset_fn`` is
        invoked to drop and recreate the connection before one retry.

        Args:
            con: Connection object (may be None — caller checks first).
            sql: Parameterized SQL string.
            params: SQL parameters.
            target: Human label (e.g. 'DuckLake', 'quant.duckdb') for logging.
            reset_fn: Callable that returns a fresh connection (or None)
                when the existing one is stale.

        Returns:
            True if the SQL executed successfully (initial OR retry),
            False otherwise.
        """
        if con is None:
            return False
        # First attempt
        try:
            con.execute(sql, params)
            return True
        except Exception as exc:
            err = str(exc).lower()
            # Only retry on transient extension / commit errors. Surface
            # schema/constraint errors immediately — those are real bugs.
            #
            # DuckLake commit-NULL signature: error contains BOTH
            # "commit" and "null" (or "internal error") together.
            # Plain "NOT NULL" violations must NOT trigger retry.
            err_l = err.lower()
            transient = (
                ("commit" in err_l and "null" in err_l)
                or "internal error" in err_l
                or "io error" in err_l
                or "database is locked" in err_l
            )
            if not transient:
                logger.warning(
                    "%s write failed (non-transient, no retry): %s",
                    target, exc,
                )
                return False
            logger.warning(
                "%s write failed (transient, retrying once): %s",
                target, exc,
            )
            # Retry: reset the connection, then run the same SQL.
            try:
                fresh = reset_fn()
                if fresh is None:
                    logger.warning(
                        "%s retry aborted: connection reset returned None",
                        target,
                    )
                    return False
                fresh.execute(sql, params)
                logger.info("%s write succeeded on retry", target)
                # Promote the fresh connection into place for the next call.
                if target == "DuckLake":
                    self._ducklake_con = fresh
                elif target == "quant.duckdb":
                    self._file_con = fresh
                return True
            except Exception as exc2:
                logger.error(
                    "%s write failed on retry: %s", target, exc2,
                )
                return False

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

        delete_sql = (
            "DELETE FROM agent_memory WHERE agent_name = ? AND key = ?"
        )
        delete_params = [self._agent_name, key]
        insert_sql = """
            INSERT INTO agent_memory
                (agent_name, memory_type, key, value_json, created_at, accessed_at, access_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        insert_params = [
            self._agent_name, memory_type, key, value_json, now, None, 0,
        ]

        # DuckLake write (with retry-once on transient errors)
        dl = self._get_ducklake()
        if dl is not None:
            ok_del = self._execute_with_retry(
                dl, delete_sql, delete_params, "DuckLake",
                reset_fn=self._reset_ducklake,
            )
            if ok_del:
                ok_ins = self._execute_with_retry(
                    self._ducklake_con or dl, insert_sql, insert_params,
                    "DuckLake", reset_fn=self._reset_ducklake,
                )
                if not ok_ins:
                    success = False
            else:
                success = False
            if success:
                logger.debug("DuckLake agent_memory write OK: %s", key)

        # quant.duckdb write (with retry-once on transient errors)
        fc = self._get_file_con()
        if fc is not None:
            ok_del = self._execute_with_retry(
                fc, delete_sql, delete_params, "quant.duckdb",
                reset_fn=self._reset_file,
            )
            if ok_del:
                ok_ins = self._execute_with_retry(
                    self._file_con or fc, insert_sql, insert_params,
                    "quant.duckdb", reset_fn=self._reset_file,
                )
                if not ok_ins:
                    success = False
            else:
                success = False
            if success:
                logger.debug("quant.duckdb agent_memory write OK: %s", key)

        return success

    def _reset_ducklake(self) -> Any:
        """Drop the cached DuckLake connection and rebuild it."""
        try:
            if self._ducklake_con is not None:
                try:
                    self._ducklake_con.close()
                except Exception:
                    pass
        except Exception:
            pass
        self._ducklake_con = None
        return self._get_ducklake()

    def _reset_file(self) -> Any:
        """Drop the cached quant.duckdb connection and rebuild it."""
        try:
            if self._file_con is not None:
                try:
                    self._file_con.close()
                except Exception:
                    pass
        except Exception:
            pass
        self._file_con = None
        return self._get_file_con()

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
            if not self._execute_with_retry(
                dl, sql, params, "DuckLake", reset_fn=self._reset_ducklake,
            ):
                success = False

        fc = self._get_file_con()
        if fc is not None:
            if not self._execute_with_retry(
                fc, sql, params, "quant.duckdb", reset_fn=self._reset_file,
            ):
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
            if not self._execute_with_retry(
                dl, sql, params, "DuckLake", reset_fn=self._reset_ducklake,
            ):
                success = False

        fc = self._get_file_con()
        if fc is not None:
            if not self._execute_with_retry(
                fc, sql, params, "quant.duckdb", reset_fn=self._reset_file,
            ):
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
