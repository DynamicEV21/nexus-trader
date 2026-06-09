"""
Curated Views — health-check for the 8 nexus lakehouse views.

Provides a quick way to verify which views exist and have data, useful
for diagnostics and pre-flight checks.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

NEXUS_VIEW_NAMES: list[str] = [
    "v_ticker_dashboard",
    "v_alpha_predictions",
    "v_catalyst_signals",
    "v_catalyst_grades_full",
    "v_experience_summary",
    "v_experience_search",
    "v_all_failures",
    "v_ohlcv_all",
]

_DEFAULT_DB_PATH = os.path.expanduser(
    os.environ.get("NEXUS_LAKEHOUSE_PATH", "~/agentic-quant-os/data/quant.duckdb")
)


def check_views(db_path: str | None = None) -> dict[str, Any]:
    """Check which nexus views exist and have data.

    Opens the database in read-only mode, iterates through
    :data:`NEXUS_VIEW_NAMES`, and returns a dict mapping each view name
    to its row count (int) or ``False`` if the view doesn't exist / errors.

    Parameters
    ----------
    db_path : str | None
        Path to the DuckDB file.  Defaults to ``NEXUS_LAKEHOUSE_PATH`` env var
        or ``~/agentic-quant-os/data/quant.duckdb``.

    Returns
    -------
    dict[str, int | bool]
        Mapping of view name → row count or False.
    """
    import duckdb

    path = db_path or _DEFAULT_DB_PATH
    result: dict[str, Any] = {}

    if not os.path.exists(path):
        logger.error("Lakehouse database not found at %s", path)
        for name in NEXUS_VIEW_NAMES:
            result[name] = False
        return result

    try:
        con = duckdb.connect(path, read_only=True)
        for view_name in NEXUS_VIEW_NAMES:
            try:
                row = con.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()
                result[view_name] = row[0] if row else 0
            except duckdb.CatalogException:
                result[view_name] = False
            except Exception as exc:
                logger.warning("View %s check failed: %s", view_name, exc)
                result[view_name] = False
        con.close()
    except Exception as exc:
        logger.error("Failed to connect to lakehouse at %s: %s", path, exc)
        for name in NEXUS_VIEW_NAMES:
            result[name] = False

    return result
