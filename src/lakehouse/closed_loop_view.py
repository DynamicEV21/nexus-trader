"""
Closed-Loop View — B4 (2026-06-25)
==================================

Creates the ``v_nexus_closed_loop`` view in ``nexus_results.duckdb`` that
joins:

* ``nexus_lumibot_results`` — what the committee ACTUALLY did (real backtests)
* ``counterfactual_outcomes`` (B3) — what would have happened if the
  committee chose a different strategy instead

The view is the headline for the closed-loop feedback system. Each row
is one (strategy, symbol) pair aggregated over all bars where the
strategy was the "what-if" alternative. The view also computes:

* ``actual_return_pct`` — the realized backtest return for the chosen strategy
* ``counterfactual_return_pct`` — the avg counterfactual return across HOLD bars
* ``uplift_pct`` — ``counterfactual - actual`` (positive = the committee
  should have switched)
* ``outperformance_rate`` — fraction of counterfactual rows where the
  alternative beat the actual

The companion ``query_closed_loop`` tool exposes this view to the
Nexus Committee agents.

Schema
------
``v_nexus_closed_loop`` is a DuckDB view created by
``ensure_views_in_results()``. The function is idempotent and safe to
call repeatedly.

Usage
-----
CLI::

    python -m src.lakehouse.closed_loop_view

API::

    from src.lakehouse.closed_loop_view import ensure_views_in_results, query_closed_loop
    ensure_views_in_results()  # idempotent
    rows = query_closed_loop(symbol='BTC', limit=10)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser(
    os.environ.get(
        "NEXUS_RESULTS_DB",
        "~/development/nexus-trade/data/nexus_results.duckdb",
    )
)


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

V_NEXUS_CLOSED_LOOP_DDL = """
CREATE OR REPLACE VIEW v_nexus_closed_loop AS
SELECT
    cf.strategy_name,
    cf.symbol,
    -- Actual: from nexus_lumibot_results (committee's real backtests)
    COALESCE(act.total_return_pct, 0) AS actual_return_pct,
    COALESCE(act.sharpe, 0) AS actual_sharpe,
    COALESCE(act.max_drawdown_pct, 0) AS actual_max_dd_pct,
    COALESCE(act.num_entries, 0) AS actual_num_entries,
    -- Counterfactual: from counterfactual_outcomes (B3)
    AVG(cf.forward_return_pct) AS counterfactual_return_pct,
    AVG(cf.forward_sharpe) AS counterfactual_sharpe,
    AVG(cf.forward_sortino) AS counterfactual_sortino,
    AVG(cf.forward_max_dd_pct) AS counterfactual_max_dd_pct,
    AVG(cf.forward_win_rate) AS counterfactual_win_rate,
    SUM(cf.forward_num_trades) AS counterfactual_total_trades,
    COUNT(*) AS counterfactual_n_bars,
    -- Uplift: positive = committee should have switched
    AVG(cf.forward_return_pct) - COALESCE(act.total_return_pct, 0) AS uplift_pct,
    -- Fraction of bars where the alternative would have beaten actual
    AVG(CASE WHEN cf.forward_return_pct > COALESCE(act.total_return_pct, 0)
             THEN 1.0 ELSE 0.0 END) AS outperformance_rate,
    -- Min strategy_rank across the bars (1 = most-recommended regime strategy)
    MIN(cf.strategy_rank) AS min_strategy_rank,
    MAX(cf.created_at) AS last_updated
FROM counterfactual_outcomes cf
LEFT JOIN nexus_lumibot_results act
    ON cf.strategy_name = act.strategy_name
   AND cf.symbol = act.symbol
GROUP BY cf.strategy_name, cf.symbol, act.total_return_pct, act.sharpe,
         act.max_drawdown_pct, act.num_entries
"""


def _open_con(db_path: str, read_only: bool = False):
    """Open DuckDB connection (with nexus_seq ensured)."""
    import duckdb
    con = duckdb.connect(db_path, read_only=read_only)
    try:
        con.execute("CREATE SEQUENCE IF NOT EXISTS nexus_seq START 1")
    except Exception:
        pass
    return con


def ensure_views_in_results(db_path: str = DEFAULT_DB_PATH) -> int:
    """Create the v_nexus_closed_loop view if it doesn't exist.

    Idempotent. Returns 1 if the view was created/replaced, 0 otherwise.
    Skips silently if the underlying tables don't exist yet.
    """
    if not os.path.exists(db_path):
        logger.warning("Database not found: %s — skipping closed-loop view", db_path)
        return 0
    con = _open_con(db_path, read_only=False)
    try:
        # Ensure the underlying tables exist
        for tbl in ("counterfactual_outcomes", "nexus_lumibot_results"):
            try:
                con.execute(f"SELECT 1 FROM {tbl} LIMIT 1").fetchone()
            except Exception:
                logger.warning(
                    "Closed-loop view skipped: table '%s' missing in %s",
                    tbl, db_path,
                )
                return 0
        con.execute(V_NEXUS_CLOSED_LOOP_DDL)
        logger.info("v_nexus_closed_loop view ensured in %s", db_path)
        return 1
    except Exception as exc:
        logger.warning("ensure_views_in_results failed: %s", exc)
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Query helper (used by query_closed_loop tool)
# ---------------------------------------------------------------------------
def query_closed_loop(
    symbol: str = "",
    strategy_name: str = "",
    min_uplift_pct: float = -1e9,
    limit: int = 20,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Read v_nexus_closed_loop rows (most-recently-updated first).

    Args:
        symbol: Filter by symbol (blank = all).
        strategy_name: Filter by strategy name (blank = all).
        min_uplift_pct: Only return rows where uplift_pct >= this
            (negative numbers include losers; large positives flag
            "should have switched" candidates).
        limit: Max rows to return.
        db_path: Path to nexus_results.duckdb.

    Returns:
        list of dict rows.
    """
    if not os.path.exists(db_path):
        return []
    # Make sure the view exists BEFORE opening the read connection
    # (DuckDB forbids two connections on the same file with different
    # configurations — ensure_views uses read_only=False to CREATE VIEW,
    # and the read connection below uses read_only=True).
    ensure_views_in_results(db_path)
    con = _open_con(db_path, read_only=True)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if strategy_name:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)
        if min_uplift_pct > -1e8:
            clauses.append("uplift_pct >= ?")
            params.append(min_uplift_pct)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = con.execute(
            f"""
            SELECT strategy_name, symbol,
                   actual_return_pct, actual_sharpe, actual_max_dd_pct,
                   actual_num_entries,
                   counterfactual_return_pct, counterfactual_sharpe,
                   counterfactual_sortino, counterfactual_max_dd_pct,
                   counterfactual_win_rate, counterfactual_total_trades,
                   counterfactual_n_bars,
                   uplift_pct, outperformance_rate, min_strategy_rank,
                   last_updated
            FROM v_nexus_closed_loop
            {where}
            ORDER BY uplift_pct DESC, counterfactual_sortino DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
        keys = (
            "strategy_name", "symbol",
            "actual_return_pct", "actual_sharpe", "actual_max_dd_pct",
            "actual_num_entries",
            "counterfactual_return_pct", "counterfactual_sharpe",
            "counterfactual_sortino", "counterfactual_max_dd_pct",
            "counterfactual_win_rate", "counterfactual_total_trades",
            "counterfactual_n_bars",
            "uplift_pct", "outperformance_rate", "min_strategy_rank",
            "last_updated",
        )
        return [dict(zip(keys, r)) for r in rows]
    except Exception as exc:
        logger.warning("query_closed_loop failed: %s", exc)
        return []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Closed-loop view (B4)")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    n = ensure_views_in_results(args.db)
    print(f"Created/replaced views: {n}")
    rows = query_closed_loop(symbol=args.symbol, limit=args.limit, db_path=args.db)
    print(f"Closed-loop rows: {len(rows)}")
    for r in rows[:10]:
        print(
            f"  {r['strategy_name']:50s} {r['symbol']:5s} "
            f"actual={r['actual_return_pct']:+7.2f}% "
            f"cf={r['counterfactual_return_pct']:+7.2f}% "
            f"uplift={r['uplift_pct']:+7.2f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())