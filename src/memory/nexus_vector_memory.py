"""
Nexus Trader Vector Memory — LanceDB-backed persistent trade memory
==================================================================

Wraps and extends the ``VectorMemory`` from agentic-quant-os to provide
Nexus Trader-specific schemas for trade decisions and lessons.

Key design principles
---------------------
* **Same LanceDB directory** as agentic-quant-os for unified storage.
* **Separate table names** — ``nexus_decisions`` and ``nexus_lessons``
  — to avoid collision with existing ``strategy_experience_bank`` and
  ``trade_assessments`` tables.
* **Graceful degradation** — every method handles missing API keys,
  embedding failures, and LanceDB errors without crashing.
* **Lazy initialization** — no API calls or file I/O at import time.
* **Singleton via ``get_nexus_memory()``** — one shared instance per
  process, created on demand.

Technology
----------
* Vector store : LanceDB (local, embedded, file-based)
* Embeddings   : ``models/gemini-embedding-001`` (3072-dim)
* API key      : ``GOOGLE_API_KEY`` environment variable
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import lancedb
from lancedb.pydantic import LanceModel, Vector
from pydantic import Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure agentic-quant-os is importable
# ---------------------------------------------------------------------------

_AQOS_PATH = os.path.expanduser("~/development/agentic-quant-os/src")
if _AQOS_PATH not in sys.path:
    sys.path.insert(0, _AQOS_PATH)

# ---------------------------------------------------------------------------
# LanceDB Pydantic schemas
# ---------------------------------------------------------------------------

EMBEDDING_DIM: int = 3072  # gemini-embedding-001 output dimensionality


class NexusDecisionRecord(LanceModel):
    """LanceDB row schema for a Nexus Trader trade decision."""

    id: str = Field(description="Unique key: decision_{timestamp}_{symbol}")
    vector: Vector(EMBEDDING_DIM) = Field(description="Gemini embedding of decision DNA")  # type: ignore[valid-type]
    text: str = Field(description="Decision DNA string that was embedded")
    symbol: str
    action: str = Field(description="buy, sell, or hold")
    regime: str
    thesis_summary: str = ""
    indicators_snapshot: str = Field(description="JSON of key indicators at decision time")
    outcome: str = Field(description="win, loss, or pending")
    pnl_pct: float = 0.0
    timestamp: str
    strategy_name: str = "Nexus_Trader"
    backtest_id: str = ""


class NexusLessonRecord(LanceModel):
    """LanceDB row schema for a lesson learned from trading experience."""

    id: str = Field(description="Unique key: lesson_{timestamp}_{hash}")
    vector: Vector(EMBEDDING_DIM) = Field(description="Gemini embedding of lesson text")  # type: ignore[valid-type]
    text: str = Field(description="Lesson text that was embedded")
    symbol: str = ""
    regime: str = ""
    category: str = Field(description="Category: mistake, insight, pattern, adaptation")
    severity: str = Field(description="info, warning, or critical")
    tags_json: str = Field(description="JSON list of tags")
    timestamp: str
    strategy_name: str = "Nexus_Trader"
    source: str = Field(description="Origin: committee, backtest, manual, bridge")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_PERSIST_DIR: str = os.environ.get(
    "NEXUS_LANCEDB_DIR",
    os.path.expanduser("~/agentic-quant-os/data/vectors"),
)

_MAX_RETRIES: int = 3
_BASE_BACKOFF_SECONDS: float = 1.0


# ---------------------------------------------------------------------------
# NexusVectorMemory
# ---------------------------------------------------------------------------


class NexusVectorMemory:
    """Nexus Trader-specific vector memory backed by LanceDB + Gemini embeddings.

    Uses the same LanceDB directory as agentic-quant-os but with
    dedicated table names ``nexus_decisions`` and ``nexus_lessons``.

    Parameters
    ----------
    persist_dir : str | None
        Directory for the LanceDB database.  Defaults to
        ``NEXUS_LANCEDB_DIR`` or ``~/agentic-quant-os/data/vectors``.
    """

    def __init__(self, persist_dir: str | None = None) -> None:
        self._persist_dir = persist_dir or _DEFAULT_PERSIST_DIR
        self._decisions_table_name = "nexus_decisions"
        self._lessons_table_name = "nexus_lessons"

        # Lazy-initialized
        self._db: lancedb.DBConnection | None = None
        self._decisions_table: lancedb.table.Table | None = None
        self._lessons_table: lancedb.table.Table | None = None
        self._genai_client: Any | None = None

        # Auto-load GOOGLE_API_KEY from env or Hermes profile
        if not os.getenv("GOOGLE_API_KEY"):
            for candidate in [
                Path.home() / ".hermes" / "profiles" / "hermcules" / ".env",
                Path.home() / ".hermes" / ".env",
            ]:
                if candidate.is_file():
                    for line in candidate.read_text().splitlines():
                        if line.startswith("GOOGLE_API_KEY=") and not line.startswith("#"):
                            os.environ["GOOGLE_API_KEY"] = line.split("=", 1)[1].strip()
                            break
                    if os.getenv("GOOGLE_API_KEY"):
                        break

        self._api_key: str | None = os.getenv("GOOGLE_API_KEY")
        self.enabled: bool = bool(self._api_key)
        if not self._api_key:
            logger.warning(
                "GOOGLE_API_KEY not set — Nexus vector memory disabled. "
                "Set the environment variable to enable experience banking."
            )

    # ------------------------------------------------------------------
    # Internal lazy initializers
    # ------------------------------------------------------------------

    def _ensure_db(self) -> None:
        """Open (or create) the LanceDB connection and both tables."""
        if self._db is not None:
            return

        try:
            Path(self._persist_dir).mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(self._persist_dir)

            resp = self._db.list_tables()
            existing_tables = set(resp.tables if hasattr(resp, "tables") else resp)

            if self._decisions_table_name in existing_tables:
                self._decisions_table = self._db.open_table(self._decisions_table_name)
            else:
                self._decisions_table = self._db.create_table(
                    self._decisions_table_name,
                    schema=NexusDecisionRecord,
                    exist_ok=True,
                )
                logger.info("Created new table: %s", self._decisions_table_name)

            if self._lessons_table_name in existing_tables:
                self._lessons_table = self._db.open_table(self._lessons_table_name)
            else:
                self._lessons_table = self._db.create_table(
                    self._lessons_table_name,
                    schema=NexusLessonRecord,
                    exist_ok=True,
                )
                logger.info("Created new table: %s", self._lessons_table_name)

            logger.debug(
                "Nexus LanceDB ready at %s (decisions=%d, lessons=%d)",
                self._persist_dir,
                self._decisions_table.count_rows() if self._decisions_table else 0,
                self._lessons_table.count_rows() if self._lessons_table else 0,
            )
        except Exception:
            logger.exception(
                "Failed to initialize LanceDB at %s — Nexus vector memory disabled",
                self._persist_dir,
            )
            self.enabled = False

    def _ensure_genai_client(self) -> None:
        """Create the Google GenAI client (one-time)."""
        if self._genai_client is not None:
            return

        try:
            from google import genai  # type: ignore[import-untyped]

            self._genai_client = genai.Client(api_key=self._api_key)
            logger.debug("Google GenAI client initialized for Nexus memory")
        except Exception:
            logger.exception(
                "Failed to initialize Google GenAI client — "
                "Nexus embedding operations will be skipped"
            )
            self.enabled = False

    # ------------------------------------------------------------------
    # Embedding helpers
    # ------------------------------------------------------------------

    def _get_embedding(
        self,
        text: str,
        task_type: str = "retrieval_document",
    ) -> list[float]:
        """Call ``gemini-embedding-001`` with retry and exponential back-off."""
        if not self.enabled:
            return []

        self._ensure_genai_client()
        if not self.enabled:
            return []

        from google.genai import types as genai_types  # type: ignore[import-untyped]

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                assert self._genai_client is not None
                response = self._genai_client.models.embed_content(
                    model="models/gemini-embedding-001",
                    contents=text,
                    config=genai_types.EmbedContentConfig(task_type=task_type),
                )
                if response.embeddings:
                    values: list[float] = response.embeddings[0].values  # type: ignore[union-attr]
                    if values:
                        return list(values)
                logger.warning(
                    "Gemini returned empty embedding (attempt %d/%d)",
                    attempt,
                    _MAX_RETRIES,
                )
            except Exception as exc:
                exc_str = str(exc).lower()
                if any(kw in exc_str for kw in ("401", "403", "permission", "invalid api key")):
                    logger.error(
                        "Gemini auth error (won't retry): %s — disabling Nexus memory",
                        exc,
                    )
                    self.enabled = False
                    return []

                logger.warning(
                    "Embedding API error on attempt %d/%d: %s",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    backoff = _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    time.sleep(backoff)
                else:
                    logger.error(
                        "All %d embedding attempts failed for text starting "
                        "with %.80s — skipping",
                        _MAX_RETRIES,
                        text,
                    )

        return []

    def _batch_embed(
        self,
        texts: list[str],
        task_type: str = "retrieval_document",
    ) -> list[list[float]]:
        """Embed multiple texts in a single API call with retry logic."""
        if not self.enabled or not texts:
            return [[] for _ in texts]

        self._ensure_genai_client()
        if not self.enabled:
            return [[] for _ in texts]

        from google.genai import types as genai_types  # type: ignore[import-untyped]

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                assert self._genai_client is not None
                response = self._genai_client.models.embed_content(
                    model="models/gemini-embedding-001",
                    contents=texts,
                    config=genai_types.EmbedContentConfig(task_type=task_type),
                )
                if response.embeddings:
                    return [list(e.values) for e in response.embeddings]  # type: ignore[union-attr]
            except Exception as exc:
                exc_str = str(exc).lower()
                if any(kw in exc_str for kw in ("401", "403", "permission", "invalid api key")):
                    logger.error("Gemini auth error (won't retry): %s", exc)
                    self.enabled = False
                    return [[] for _ in texts]

                is_rate_limit = any(kw in exc_str for kw in ("429", "quota", "resource_exhausted"))
                if is_rate_limit and attempt == 1:
                    logger.warning("Rate limited. Waiting 65s for quota reset...")
                    time.sleep(65.0)
                    continue
                elif is_rate_limit:
                    logger.warning("Rate limited after retry. Giving up on this batch.")
                    return [[] for _ in texts]

                logger.warning(
                    "Batch embed API error on attempt %d/%d: %s",
                    attempt, _MAX_RETRIES, exc,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))

        return [[] for _ in texts]

    # ------------------------------------------------------------------
    # Decision DNA builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_decision_dna(decision: dict[str, Any]) -> str:
        """Construct a human-readable decision DNA string for embedding.

        Format::

            [SYMBOL] ACTION in REGIME regime | Thesis: ... |
            Indicators: ... | PnL: X.X% | Outcome: ... |
            Strategy: ...
        """
        symbol = decision.get("symbol", "???")
        action = decision.get("action", "hold").upper()
        regime = decision.get("regime", "unknown")
        thesis = decision.get("thesis_summary", "")[:200]
        outcome = decision.get("outcome", "pending")
        pnl = decision.get("pnl_pct", 0.0)
        strategy = decision.get("strategy_name", "Nexus_Trader")

        return (
            f"[{symbol}] {action} in {regime} regime | "
            f"Thesis: {thesis} | "
            f"PnL: {pnl:+.2f}% | "
            f"Outcome: {outcome} | "
            f"Strategy: {strategy}"
        )

    # ------------------------------------------------------------------
    # Public API — Decisions
    # ------------------------------------------------------------------

    def store_decision(self, decision: dict[str, Any]) -> bool:
        """Store a single trade decision in the vector memory.

        Parameters
        ----------
        decision : dict
            Must have keys: ``id``, ``symbol``, ``action``, ``regime``,
            ``timestamp``.  Optional: ``thesis_summary``,
            ``indicators_snapshot``, ``outcome``, ``pnl_pct``,
            ``strategy_name``, ``backtest_id``.

        Returns
        -------
        bool
            ``True`` if the decision was persisted successfully.
        """
        if not self.enabled:
            return False

        try:
            self._ensure_db()
            if not self.enabled or self._decisions_table is None:
                return False

            dna = self._build_decision_dna(decision)
            vector = self._get_embedding(dna, task_type="retrieval_document")
            if not vector:
                logger.warning(
                    "Skipping decision %s — no embedding produced",
                    decision.get("id", "?"),
                )
                return False

            # Serialize indicators_snapshot to JSON string if it's a dict
            indicators = decision.get("indicators_snapshot", "{}")
            if isinstance(indicators, dict):
                indicators = json.dumps(indicators)

            record = NexusDecisionRecord(
                id=decision.get("id", f"decision_{decision.get('timestamp', 'unknown')}_{decision.get('symbol', '???')}"),
                vector=vector,
                text=dna,
                symbol=decision.get("symbol", ""),
                action=decision.get("action", "hold"),
                regime=decision.get("regime", "unknown"),
                thesis_summary=decision.get("thesis_summary", "")[:500],
                indicators_snapshot=indicators,
                outcome=decision.get("outcome", "pending"),
                pnl_pct=float(decision.get("pnl_pct", 0.0)),
                timestamp=decision.get("timestamp", ""),
                strategy_name=decision.get("strategy_name", "Nexus_Trader"),
                backtest_id=decision.get("backtest_id", ""),
            )

            # Upsert: delete existing row with same ID, then add
            try:
                safe_id = record.id.replace("'", "''")
                self._decisions_table.delete(f"id = '{safe_id}'")
            except Exception:
                pass

            self._decisions_table.add([record.model_dump()])
            logger.info(
                "Stored decision %s (%s %s, regime=%s)",
                record.id, record.symbol, record.action, record.regime,
            )
            return True

        except Exception:
            logger.exception(
                "Failed to store decision %s", decision.get("id", "?")
            )
            return False

    def batch_store_decisions(
        self,
        decisions: list[dict[str, Any]],
        batch_size: int = 100,
    ) -> dict[str, int]:
        """Embed and store multiple decisions in batches.

        Parameters
        ----------
        decisions : list[dict]
            Each dict must have: ``id``, ``symbol``, ``action``,
            ``regime``, ``timestamp``.
        batch_size : int
            Number of texts per Gemini API call (max 100).

        Returns
        -------
        dict[str, int]
            Keys: ``embedded``, ``skipped``, ``errors``, ``total``.
        """
        stats: dict[str, int] = {"embedded": 0, "skipped": 0, "errors": 0, "total": len(decisions)}

        if not self.enabled or not decisions:
            return stats

        try:
            self._ensure_db()
            if not self.enabled or self._decisions_table is None:
                stats["errors"] = len(decisions)
                return stats
        except Exception:
            logger.exception("Failed to ensure DB for batch decision store")
            stats["errors"] = len(decisions)
            return stats

        # Pre-load existing IDs to skip duplicates
        existing_ids: set[str] = set()
        try:
            rows = self._decisions_table.search().select(["id"]).limit(500_000).to_list()
            existing_ids = {r["id"] for r in rows}
        except Exception:
            logger.warning("Could not pre-load existing decision IDs; will try upserts")

        api_calls = 0

        for batch_start in range(0, len(decisions), batch_size):
            batch = decisions[batch_start : batch_start + batch_size]

            dna_strings: list[str] = []
            record_ids: list[str] = []
            valid_indices: list[int] = []

            for i, dec in enumerate(batch):
                record_id = dec.get("id", "")
                if not record_id:
                    record_id = f"decision_{dec.get('timestamp', 'unknown')}_{dec.get('symbol', '???')}"
                    dec["id"] = record_id
                if record_id in existing_ids:
                    stats["skipped"] += 1
                    continue
                dna = self._build_decision_dna(dec)
                dna_strings.append(dna)
                record_ids.append(record_id)
                valid_indices.append(i)

            if not dna_strings:
                continue

            # Rate limit between batches
            if api_calls > 0:
                if api_calls % 15 == 0:
                    time.sleep(1.0)
                else:
                    time.sleep(0.1)

            api_calls += 1

            # Batch embed
            try:
                vectors = self._batch_embed(dna_strings, task_type="retrieval_document")
            except Exception:
                logger.exception("Batch embed failed for decisions at offset %d", batch_start)
                vectors = [[] for _ in dna_strings]

            all_failed = all(not v for v in vectors) if vectors else True
            if all_failed and dna_strings:
                logger.warning(
                    "All embeddings failed for decision batch at offset %d — cooling down 65s",
                    batch_start,
                )
                time.sleep(65.0)

            # Build and store records
            records_to_add: list[dict[str, Any]] = []
            for j, idx in enumerate(valid_indices):
                dec = batch[idx]
                if j < len(vectors) and vectors[j]:
                    try:
                        indicators = dec.get("indicators_snapshot", "{}")
                        if isinstance(indicators, dict):
                            indicators = json.dumps(indicators)

                        record = NexusDecisionRecord(
                            id=record_ids[j],
                            vector=vectors[j],
                            text=dna_strings[j],
                            symbol=dec.get("symbol", ""),
                            action=dec.get("action", "hold"),
                            regime=dec.get("regime", "unknown"),
                            thesis_summary=dec.get("thesis_summary", "")[:500],
                            indicators_snapshot=str(indicators),
                            outcome=dec.get("outcome", "pending"),
                            pnl_pct=float(dec.get("pnl_pct", 0.0)),
                            timestamp=dec.get("timestamp", ""),
                            strategy_name=dec.get("strategy_name", "Nexus_Trader"),
                            backtest_id=dec.get("backtest_id", ""),
                        )
                        records_to_add.append(record.model_dump())
                        existing_ids.add(record_ids[j])
                        stats["embedded"] += 1
                    except Exception:
                        logger.exception("Failed to build decision record for %s", record_ids[j])
                        stats["errors"] += 1
                else:
                    stats["errors"] += 1

            if records_to_add:
                try:
                    self._decisions_table.add(records_to_add)
                except Exception:
                    logger.exception("Failed to insert decision batch at offset %d", batch_start)
                    stats["errors"] += len(records_to_add)
                    stats["embedded"] -= len(records_to_add)

            done = batch_start + len(batch)
            if done % (batch_size * 5) == 0 or done >= len(decisions):
                logger.info(
                    "Decision batch progress: %d/%d embedded=%d skipped=%d errors=%d",
                    done, len(decisions),
                    stats["embedded"], stats["skipped"], stats["errors"],
                )

        return stats

    def search_similar_decisions(
        self,
        query_text: str,
        n_results: int = 5,
        symbol: str | None = None,
        regime: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find similar past trade decisions by semantic similarity.

        Parameters
        ----------
        query_text : str
            Natural-language query describing the current trade context.
        n_results : int
            Maximum number of results to return.
        symbol : str | None
            Optional filter by symbol.
        regime : str | None
            Optional filter by market regime.

        Returns
        -------
        list[dict]
            Each dict has keys ``text``, ``metadata``, ``distance``.
        """
        if not self.enabled or not query_text or not query_text.strip():
            return []

        try:
            self._ensure_db()
            if not self.enabled or self._decisions_table is None:
                return []

            query_vector = self._get_embedding(query_text, task_type="retrieval_query")
            if not query_vector:
                return []

            query = self._decisions_table.search(query_vector).limit(n_results)

            # Build SQL WHERE clause from filters
            clauses: list[str] = []
            if symbol:
                clauses.append(f"symbol = '{symbol.replace(chr(39), chr(39)+chr(39))}'")
            if regime:
                clauses.append(f"regime = '{regime.replace(chr(39), chr(39)+chr(39))}'")
            if clauses:
                query = query.where(" AND ".join(clauses), prefilter=True)

            rows = query.to_list()

            results: list[dict[str, Any]] = []
            for row in rows:
                distance = row.pop("_distance", None)
                row.pop("vector", None)
                text = row.pop("text", "")
                metadata = dict(row)
                results.append({"text": text, "metadata": metadata, "distance": distance})

            return results

        except Exception:
            logger.exception("Decision search failed: %s", query_text[:100])
            return []

    # ------------------------------------------------------------------
    # Public API — Lessons
    # ------------------------------------------------------------------

    def store_lesson(self, lesson: dict[str, Any]) -> bool:
        """Store a single lesson learned from trading experience.

        Parameters
        ----------
        lesson : dict
            Must have keys: ``id``, ``text``, ``timestamp``.
            Optional: ``symbol``, ``regime``, ``category``, ``severity``,
            ``tags`` (list), ``strategy_name``, ``source``.

        Returns
        -------
        bool
            ``True`` if the lesson was persisted successfully.
        """
        if not self.enabled:
            return False

        try:
            self._ensure_db()
            if not self.enabled or self._lessons_table is None:
                return False

            text = lesson.get("text", "")
            if not text:
                logger.warning("Skipping lesson with empty text")
                return False

            vector = self._get_embedding(text, task_type="retrieval_document")
            if not vector:
                logger.warning("Skipping lesson %s — no embedding produced", lesson.get("id", "?"))
                return False

            tags = lesson.get("tags", [])
            if isinstance(tags, list):
                tags_json = json.dumps(tags)
            elif isinstance(tags, str):
                tags_json = tags
            else:
                tags_json = "[]"

            record = NexusLessonRecord(
                id=lesson.get("id", f"lesson_{lesson.get('timestamp', 'unknown')}"),
                vector=vector,
                text=text,
                symbol=lesson.get("symbol", ""),
                regime=lesson.get("regime", ""),
                category=lesson.get("category", "insight"),
                severity=lesson.get("severity", "info"),
                tags_json=tags_json,
                timestamp=lesson.get("timestamp", ""),
                strategy_name=lesson.get("strategy_name", "Nexus_Trader"),
                source=lesson.get("source", ""),
            )

            try:
                safe_id = record.id.replace("'", "''")
                self._lessons_table.delete(f"id = '{safe_id}'")
            except Exception:
                pass

            self._lessons_table.add([record.model_dump()])
            logger.info("Stored lesson %s (category=%s)", record.id, record.category)
            return True

        except Exception:
            logger.exception("Failed to store lesson %s", lesson.get("id", "?"))
            return False

    def search_lessons(
        self,
        query_text: str,
        n_results: int = 5,
        category: str | None = None,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for relevant lessons by semantic similarity.

        Parameters
        ----------
        query_text : str
            Natural-language query describing the current situation.
        n_results : int
            Maximum number of results.
        category : str | None
            Optional filter: mistake, insight, pattern, adaptation.
        severity : str | None
            Optional filter: info, warning, critical.

        Returns
        -------
        list[dict]
            Each dict has keys ``text``, ``metadata``, ``distance``.
        """
        if not self.enabled or not query_text or not query_text.strip():
            return []

        try:
            self._ensure_db()
            if not self.enabled or self._lessons_table is None:
                return []

            query_vector = self._get_embedding(query_text, task_type="retrieval_query")
            if not query_vector:
                return []

            query = self._lessons_table.search(query_vector).limit(n_results)

            clauses: list[str] = []
            if category:
                clauses.append(f"category = '{category.replace(chr(39), chr(39)+chr(39))}'")
            if severity:
                clauses.append(f"severity = '{severity.replace(chr(39), chr(39)+chr(39))}'")
            if clauses:
                query = query.where(" AND ".join(clauses), prefilter=True)

            rows = query.to_list()

            results: list[dict[str, Any]] = []
            for row in rows:
                distance = row.pop("_distance", None)
                row.pop("vector", None)
                text = row.pop("text", "")
                metadata = dict(row)
                results.append({"text": text, "metadata": metadata, "distance": distance})

            return results

        except Exception:
            logger.exception("Lesson search failed: %s", query_text[:100])
            return []

    def get_recent_lessons(self, n: int = 10) -> list[dict[str, Any]]:
        """Get the most recently stored lessons (by timestamp).

        Uses a table scan — no semantic search.

        Parameters
        ----------
        n : int
            Maximum number of lessons to return.

        Returns
        -------
        list[dict]
            Lesson records without vectors, ordered by recency.
        """
        if not self.enabled:
            return []

        try:
            self._ensure_db()
            if not self.enabled or self._lessons_table is None:
                return []

            # LanceDB doesn't support ORDER BY natively on all queries,
            # so we fetch and sort in Python
            rows = self._lessons_table.search().limit(max(n * 5, 200)).to_list()
            results: list[dict[str, Any]] = []
            for row in rows:
                row.pop("vector", None)
                results.append(dict(row))

            # Sort by timestamp descending and take top n
            results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
            return results[:n]

        except Exception:
            logger.exception("Failed to get recent lessons")
            return []

    def build_context_prompt(
        self,
        symbol: str,
        regime: str,
        action: str,
        thesis: str,
        max_decisions: int = 3,
        max_lessons: int = 3,
    ) -> str:
        """Build a context prompt string for an AI committee member.

        Searches for similar past decisions and relevant lessons, then
        formats them into a markdown prompt for inline inclusion.

        Parameters
        ----------
        symbol : str
            The trading symbol being evaluated.
        regime : str
            Current market regime.
        action : str
            Proposed action (buy, sell, hold).
        thesis : str
            The current investment thesis.
        max_decisions : int
            Max similar decisions to include.
        max_lessons : int
            Max relevant lessons to include.

        Returns
        -------
        str
            Formatted markdown prompt section, or empty string if nothing found.
        """
        if not self.enabled:
            return ""

        parts: list[str] = []
        query = f"{action} {symbol} in {regime} regime: {thesis[:200]}"

        # Search similar decisions
        decisions = self.search_similar_decisions(
            query, n_results=max_decisions, symbol=symbol, regime=regime,
        )
        if decisions:
            parts.append("## Similar Past Decisions\n")
            for i, d in enumerate(decisions, 1):
                meta = d["metadata"]
                parts.append(
                    f"{i}. **{meta.get('symbol', '?')}** — {meta.get('action', '?').upper()} "
                    f"in {meta.get('regime', '?')} | PnL: {meta.get('pnl_pct', 0):+.2f}% | "
                    f"Outcome: {meta.get('outcome', '?')}"
                )
                if d["text"]:
                    parts.append(f"   - {d['text'][:200]}")
                parts.append("")

        # Search relevant lessons
        lessons = self.search_lessons(query, n_results=max_lessons)
        if lessons:
            parts.append("## Relevant Lessons\n")
            for i, lsn in enumerate(lessons, 1):
                meta = lsn["metadata"]
                sev = meta.get("severity", "info").upper()
                cat = meta.get("category", "insight")
                parts.append(f"{i}. [{sev}] ({cat}) {lsn['text'][:300]}")
                parts.append("")

        return "\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return summary statistics about the Nexus vector memory."""
        stats: dict[str, Any] = {
            "enabled": self.enabled,
            "persist_dir": self._persist_dir,
            "total_decisions": 0,
            "total_lessons": 0,
        }

        if not self.enabled:
            return stats

        try:
            self._ensure_db()
            if self._decisions_table is not None:
                stats["total_decisions"] = self._decisions_table.count_rows()
            if self._lessons_table is not None:
                stats["total_lessons"] = self._lessons_table.count_rows()
        except Exception:
            logger.exception("Failed to read Nexus memory stats")

        return stats


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: NexusVectorMemory | None = None


def get_nexus_memory() -> NexusVectorMemory:
    """Return the process-wide :class:`NexusVectorMemory` singleton.

    The instance is created on first call (lazy), **not** at import time.
    """
    global _instance
    if _instance is None:
        _instance = NexusVectorMemory()
    return _instance
