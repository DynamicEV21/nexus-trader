"""
View migration — ensure the 4 Nexus curated views exist in quant.duckdb.

Background
----------
The agentic-quant-os ``schema.py`` defines 4 nexus curated views
(``v_nexus_regime_strategy_map``, ``v_nexus_catalyst_digest``,
``v_nexus_failures``, ``v_nexus_experience``) that are created in DuckLake
when ``get_all_ddl()`` is run. The standalone ``quant.duckdb`` file
inherits the underlying tables (``regime_strategy_map``, ``catalyst_grades``,
``failures``, ``experience_bank``) but NOT the views, so direct readers
see ``CatalogException: v_nexus_regime_strategy_map does not exist`` and
log warnings (cosmetic, but noisy).

This module is a one-shot migration: it re-creates the 4 views in any
open DuckDB connection (write mode). It is idempotent (``CREATE OR
REPLACE VIEW``) and safe to call multiple times.

The migration is invoked automatically by ``NexusLakehouseReader._con_get()``
on first read after a fresh ``quant.duckdb`` is created, OR by the
``AQSWriter`` on first write, OR directly via the CLI:

    python -m src.lakehouse.view_migration
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# The 4 nexus curated views that are NOT auto-created in quant.duckdb.
# Schema mirrors agentic-quant-os/src/schema.py:NEXUS_CURATED_VIEWS.
NEXUS_CURATED_VIEW_DDL: list[str] = [
    # 5. Regime-strategy mapping: which strategies historically work in which regimes
    """
    CREATE OR REPLACE VIEW v_nexus_regime_strategy_map AS
    SELECT * FROM regime_strategy_map
    WHERE sample_count >= 5 AND avg_sharpe > 0
    ORDER BY avg_sharpe DESC
    """,
    # 6. Catalyst digest — latest catalyst grades
    """
    CREATE OR REPLACE VIEW v_nexus_catalyst_digest AS
    SELECT DISTINCT ON (ticker) *
    FROM catalyst_grades
    WHERE score IS NOT NULL
    ORDER BY ticker, timestamp DESC
    """,
    # 7. Failure memory for Nexus preflight checks
    """
    CREATE OR REPLACE VIEW v_nexus_failures AS
    SELECT * FROM failures
    ORDER BY timestamp DESC
    """,
    # 8. Experience bank entries from quant projects (not Nexus itself)
    """
    CREATE OR REPLACE VIEW v_nexus_experience AS
    SELECT * FROM experience_bank
    WHERE source_repo IN (
        'regime-intelligence', 'alpha-factory',
        'quant-loop-testnet', 'alpha-lab', 'quant-research-mas'
    )
       AND severity IN ('warning', 'critical', 'info')
    ORDER BY created_at DESC
    """,
]


def ensure_views(con: Any, view_ddl: list[str] | None = None) -> int:
    """Ensure the 4 nexus curated views exist in the given connection.

    Idempotent (``CREATE OR REPLACE``). Returns the number of views
    successfully created or replaced. Skips silently if the underlying
    tables don't exist (e.g. on a fresh empty quant.duckdb where the
    tables will be created by the next AQS agent sync).

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        An open writable DuckDB connection. The function will NOT
        open or close the connection itself.
    view_ddl : list[str] | None
        Optional override of the DDL list. Defaults to
        :data:`NEXUS_CURATED_VIEW_DDL`.

    Returns
    -------
    int
        Number of views created or replaced.
    """
    ddl = view_ddl or NEXUS_CURATED_VIEW_DDL
    created = 0
    for stmt in ddl:
        try:
            con.execute(stmt)
            created += 1
        except Exception as exc:
            # Most likely cause: the underlying table doesn't exist
            # yet (fresh DB, no AQS sync). Not a fatal error — the
            # views will be created on the next call after the table
            # appears. Log and continue.
            logger.debug(
                "View migration skipped (table likely missing): %s", exc,
            )
    if created:
        logger.info(
            "Nexus view migration: %d/%d views ensured",
            created, len(ddl),
        )
    return created


def ensure_views_in_quantdb(db_path: str | None = None) -> int:
    """Open quant.duckdb in write mode and ensure the 4 nexus views exist.

    Convenience function for the CLI / one-shot migration. Returns the
    number of views created/replaced.

    Parameters
    ----------
    db_path : str | None
        Path to the DuckDB file. Defaults to ``NEXUS_LAKEHOUSE_PATH``
        env var or ``~/development/agentic-quant-os/data/quant.duckdb``.

    Returns
    -------
    int
        Number of views created or replaced (0 if file doesn't exist
        or can't be opened in write mode).
    """
    if db_path is None:
        db_path = os.path.expanduser(
            os.environ.get(
                "NEXUS_LAKEHOUSE_PATH",
                "~/development/agentic-quant-os/data/quant.duckdb",
            )
        )
    if not os.path.exists(db_path):
        logger.warning(
            "quant.duckdb not found at %s — skipping view migration",
            db_path,
        )
        return 0
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=False)
        try:
            n = ensure_views(con)
            return n
        finally:
            con.close()
    except Exception as exc:
        logger.error(
            "View migration failed for %s: %s", db_path, exc,
        )
        return 0


def check_missing_views(db_path: str | None = None) -> list[str]:
    """Check which of the 4 nexus views are missing from quant.duckdb.

    Returns a list of view names that don't exist or error on read.
    Empty list means all 4 are present.
    """
    if db_path is None:
        db_path = os.path.expanduser(
            os.environ.get(
                "NEXUS_LAKEHOUSE_PATH",
                "~/development/agentic-quant-os/data/quant.duckdb",
            )
        )
    if not os.path.exists(db_path):
        return [v for v in (
            "v_nexus_regime_strategy_map",
            "v_nexus_catalyst_digest",
            "v_nexus_failures",
            "v_nexus_experience",
        )]
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        try:
            missing = []
            for vname in (
                "v_nexus_regime_strategy_map",
                "v_nexus_catalyst_digest",
                "v_nexus_failures",
                "v_nexus_experience",
            ):
                try:
                    con.execute(f"SELECT 1 FROM {vname} LIMIT 1").fetchone()
                except Exception:
                    missing.append(vname)
            return missing
        finally:
            con.close()
    except Exception:
        return []


if __name__ == "__main__":
    # CLI: python -m src.lakehouse.view_migration
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    missing = check_missing_views(db_path)
    if missing:
        print(f"Missing views: {missing}")
        n = ensure_views_in_quantdb(db_path)
        print(f"Created/replaced: {n}")
    else:
        print("All 4 nexus views already present.")
