"""
Counterfactual Tool — LumiBot @agent_tool for the counterfactual replay runner
================================================================================

Wraps :func:`src.runners.counterfactual_replay.query_counterfactual_outcomes`
as a LumiBot ``@agent_tool`` so the AI committee can ask "what WOULD have
happened if I'd chosen the regime-recommended strategy on bar T?"

The underlying data lives in ``nexus_results.duckdb.counterfactual_outcomes``
and is populated by ``src/runners/counterfactual_replay.py``.

Functions
---------
* ``query_counterfactuals_tool(symbol, strategy_name, top_k, limit)`` — read.
* ``run_counterfactual_replay_tool(symbols, forward_bars, top_k, dry_run)`` — write.

Both are exposed as ``@agent_tool`` functions for the Nexus Committee.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Make sure the runners package is importable from within the tool.
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def query_counterfactuals_tool(
    symbol: str = "",
    strategy_name: str = "",
    top_k: int = 10,
    limit: int = 50,
) -> dict[str, Any]:
    """Query the counterfactual_outcomes table for recent replay results.

    Returns one row per (HOLD bar, regime-recommended strategy) pair, with
    the estimated forward-N-bar performance if the committee had chosen
    that strategy instead of HOLDing.

    Args:
        symbol: Filter by symbol (e.g. 'BTC'). Blank = all.
        strategy_name: Filter by strategy name. Blank = all.
        top_k: Return only the top-K rows by strategy_rank (1=best).
            0 = no rank filter.
        limit: Max rows to return (default 50).

    Returns:
        dict with keys:
        - **outcomes** (list) — each row: symbol, hold_bar_date, regime,
          strategy_name, strategy_rank, forward_return_pct, forward_sharpe,
          forward_max_dd_pct, forward_num_trades, forward_win_rate, method.
        - **count** (int) — number of rows returned.
        - **as_of_sim_time** (str) — the active sim-time used for filtering.
    """
    from src.tools._strategy_context import get_sim_time
    from src.runners.counterfactual_replay import (
        DEFAULT_DB_PATH,
        query_counterfactual_outcomes,
    )

    sim_time = get_sim_time() or ""

    try:
        rows = query_counterfactual_outcomes(
            symbol=symbol,
            strategy_name=strategy_name,
            limit=limit,
            db_path=DEFAULT_DB_PATH,
        )
        if top_k > 0:
            rows = [r for r in rows if r.get("strategy_rank", 99) <= top_k]
        return {
            "outcomes": rows,
            "count": len(rows),
            "as_of_sim_time": sim_time,
        }
    except Exception as exc:
        logger.warning("query_counterfactuals failed: %s", exc)
        return {
            "outcomes": [],
            "count": 0,
            "error": str(exc),
            "as_of_sim_time": sim_time,
        }


def run_counterfactual_replay_tool(
    symbols: str = "BTC,ETH,SOL",
    forward_bars: int = 50,
    top_k: int = 3,
    lookback_days: int = 1825,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Trigger the counterfactual replay runner.

    Use this when the lakehouse needs fresh counterfactual data. The
    runner reads walk-forward results from nexus_results.duckdb and
    backtest metrics from quant.duckdb, computes approximate forward
    performance for each HOLD bar, and persists to
    ``nexus_results.duckdb.counterfactual_outcomes``.

    Note: this is an O(1)-per-bar approximation, NOT a full backtest.
    It uses cached per-strategy metrics to estimate forward return.
    See ``src/runners/counterfactual_replay.py`` for the math.

    Args:
        symbols: Comma-separated symbols (default 'BTC,ETH,SOL').
        forward_bars: Forward window size in bars (default 50).
        top_k: Top-K regime-recommended strategies to replay (default 3).
        lookback_days: Skip HOLD bars older than this many days (default 1825).
        dry_run: If True, compute but don't write to counterfactual_outcomes.

    Returns:
        dict with ``ok``, ``hold_bars_count``, ``outcomes_count``,
        ``per_symbol``, ``per_strategy``.
    """
    try:
        from src.runners.counterfactual_replay import run_replay
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        result = run_replay(
            symbols=syms,
            lookback_days=lookback_days,
            forward_bars=forward_bars,
            top_k=top_k,
            write=not dry_run,
        )
        return result
    except Exception as exc:
        logger.warning("run_counterfactual_replay failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    QUERY_COUNTERFACTUALS = agent_tool(
        name="query_counterfactuals",
        description=(
            "Query the counterfactual_outcomes table for recent replay results. "
            "Returns one row per (HOLD bar, regime-recommended strategy) pair with "
            "the estimated forward-N-bar performance IF the committee had chosen "
            "that strategy instead of HOLDing. Use this to evaluate 'would-have, "
            "could-have' scenarios when deciding whether to switch strategies."
        ),
    )(query_counterfactuals_tool)

    RUN_COUNTERFACTUAL_REPLAY = agent_tool(
        name="run_counterfactual_replay",
        description=(
            "Trigger the counterfactual replay runner to refresh counterfactual_outcomes "
            "from walk-forward and backtest results. Use sparingly — expensive O(N_bars) "
            "computation. Pass dry_run=true to preview without writing."
        ),
    )(run_counterfactual_replay_tool)

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorators skipped for counterfactual tools")
    QUERY_COUNTERFACTUALS = query_counterfactuals_tool
    RUN_COUNTERFACTUAL_REPLAY = run_counterfactual_replay_tool