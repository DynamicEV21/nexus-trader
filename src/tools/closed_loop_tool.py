"""
Closed-Loop Tool — LumiBot @agent_tool for the v_nexus_closed_loop view
=========================================================================

Wraps :func:`src.lakehouse.closed_loop_view.query_closed_loop` as a
LumiBot ``@agent_tool`` so the AI committee can ask "given my recent
decisions, what would have happened if I'd chosen differently?"

The view joins ``nexus_lumibot_results`` (what the committee ACTUALLY
did) with ``counterfactual_outcomes`` (B3's what-if data) and computes
per-strategy uplift (counterfactual - actual return).

Functions
---------
* ``query_closed_loop_tool(symbol, strategy_name, min_uplift_pct, limit)``
  - reads the view and returns ranked rows.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def query_closed_loop_tool(
    symbol: str = "",
    strategy_name: str = "",
    min_uplift_pct: float = -100.0,
    limit: int = 10,
) -> dict[str, Any]:
    """Query the v_nexus_closed_loop view for committee vs counterfactual analysis.

    Each row is a (strategy, symbol) pair with both the ACTUAL realized
    backtest return and the AVERAGE counterfactual return (over HOLD
    bars where the strategy was a 'what-if'). The ``uplift_pct`` column
    is counterfactual - actual; positive values flag strategies the
    committee should consider switching to.

    Use this during committee deliberation when choosing between
    strategies. Top-ranked rows (highest uplift) are the "you should
    have done this instead" candidates.

    Args:
        symbol: Filter by symbol (blank = all).
        strategy_name: Filter by strategy name (blank = all).
        min_uplift_pct: Minimum uplift to include (default -100 = all).
            Pass e.g. 5.0 to only see "you should have switched" rows.
        limit: Max rows to return (default 10).

    Returns:
        dict with keys:
        - **rows** (list) — each row carries strategy_name, symbol,
          actual_return_pct, actual_sharpe, actual_max_dd_pct,
          actual_num_entries, counterfactual_return_pct,
          counterfactual_sharpe, counterfactual_sortino,
          counterfactual_max_dd_pct, counterfactual_win_rate,
          counterfactual_total_trades, counterfactual_n_bars,
          uplift_pct, outperformance_rate, min_strategy_rank,
          last_updated.
        - **count** (int) — number of rows returned.
        - **as_of_sim_time** (str) — active sim-time for the query.
    """
    from src.tools._strategy_context import get_sim_time
    from src.lakehouse.closed_loop_view import (
        DEFAULT_DB_PATH,
        ensure_views_in_results,
        query_closed_loop,
    )

    sim_time = get_sim_time() or ""

    # Ensure view exists (cheap, idempotent — creates on first call).
    ensure_views_in_results(DEFAULT_DB_PATH)

    try:
        rows = query_closed_loop(
            symbol=symbol,
            strategy_name=strategy_name,
            min_uplift_pct=min_uplift_pct,
            limit=limit,
            db_path=DEFAULT_DB_PATH,
        )
        return {
            "rows": rows,
            "count": len(rows),
            "as_of_sim_time": sim_time,
        }
    except Exception as exc:
        logger.warning("query_closed_loop_tool failed: %s", exc)
        return {
            "rows": [],
            "count": 0,
            "error": str(exc),
            "as_of_sim_time": sim_time,
        }


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    QUERY_CLOSED_LOOP = agent_tool(
        name="query_closed_loop",
        description=(
            "Query the v_nexus_closed_loop view for committee-vs-counterfactual "
            "analysis. Each row is a (strategy, symbol) pair with both the ACTUAL "
            "realized backtest return and the AVERAGE counterfactual return over "
            "HOLD bars where the strategy was a 'what-if'. Uplift_pct = "
            "counterfactual - actual; positive values flag strategies the "
            "committee should consider switching to. Use during deliberation "
            "to evaluate 'you should have done this instead' candidates."
        ),
    )(query_closed_loop_tool)

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorator skipped for closed_loop_tool")
    QUERY_CLOSED_LOOP = query_closed_loop_tool