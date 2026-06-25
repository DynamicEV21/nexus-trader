"""
Trade Memory Tool — LumiBot @agent_tool for semantic memory search
===================================================================

Provides AI agents with the ability to query the Nexus Trader vector
memory for similar past decisions, relevant lessons, and historical
context.  This allows the investment committee to learn from history.

The tool wraps :class:`NexusVectorMemory` methods as callable
agent tools that receive ``self`` (the strategy instance).

Requirements
------------
* ``sentence-transformers`` with ``Qwen/Qwen3-Embedding-0.6B`` (local embeddings)
* LanceDB database at ``NEXUS_LANCEDB_DIR`` or default location.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# Ensure this package is importable (for the memory module)
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

logger = logging.getLogger(__name__)


def query_trade_memory_tool(
    query: str,
    symbol: str = "",
    regime: str = "",
    n_results: int = 5,
) -> dict[str, Any]:
    """Search the trade memory bank for similar past decisions and lessons.

    Uses semantic similarity (vector search) to find historical trade
    decisions and lessons that match the current context.  This helps
    the investment committee learn from prior experience.

    B2 anti-leakage: the active strategy's ``_sim_time`` is read from
    the strategy context (set by ``NexusCommitteeStrategy`` at the
    start of every ``on_trading_iteration``). All returned decisions
    are filtered to those whose ``decision_sim_time <= _sim_time`` —
    the LLM cannot see decisions made at future sim-bars.

    Args:
        query: Natural-language description of the current trade context,
               thesis, or situation.
        symbol: Optional stock symbol to filter results.
        regime: Optional market regime to filter results.
        n_results: Maximum results per category (default 5).

    Returns:
        dict with keys:
        - **similar_decisions** (list) — past decisions with their metadata and outcome
        - **relevant_lessons** (list) — lessons learned from similar situations
        - **context_prompt** (str) — formatted markdown with key findings for inclusion in agent prompts
        - **total_results** (int) — combined count of results found
        - **as_of_sim_time** (str) — the active sim-time used for filtering (echo for transparency)
    """
    from src.memory.nexus_vector_memory import get_nexus_memory
    from src.tools._strategy_context import get_strategy, get_sim_time

    # B2: pull the active sim-time so we never leak future bars' decisions.
    sim_time = get_sim_time()
    if sim_time is None:
        # Fall back to wall-clock so live mode still works (legacy).
        try:
            strategy = get_strategy()
            if strategy is not None and hasattr(strategy, "get_datetime"):
                sim_time = strategy.get_datetime().isoformat()
        except Exception:
            pass

    try:
        nexus = get_nexus_memory()
        if not nexus.enabled:
            return {
                "similar_decisions": [],
                "relevant_lessons": [],
                "context_prompt": "",
                "total_results": 0,
                "warning": "Vector memory is disabled (embedding model not available or LanceDB unavailable)",
            }

        decisions = nexus.search_similar_decisions(
            query,
            n_results=n_results,
            symbol=symbol or None,
            regime=regime or None,
            as_of_sim_time=sim_time,
        )
        lessons = nexus.search_lessons(query, n_results=n_results)

        # Build a compact context prompt
        context_prompt = nexus.build_context_prompt(
            symbol=symbol or "unknown",
            regime=regime or "unknown",
            action="evaluate",
            thesis=query[:200],
            max_decisions=n_results,
            max_lessons=n_results,
        )

        result = {
            "similar_decisions": decisions,
            "relevant_lessons": lessons,
            "context_prompt": context_prompt,
            "total_results": len(decisions) + len(lessons),
            "as_of_sim_time": sim_time or "",
        }

        logger.info(
            "Trade memory query returned %d decisions + %d lessons",
            len(decisions), len(lessons),
        )
        return result

    except Exception as exc:
        logger.exception("Trade memory query failed")
        return {
            "similar_decisions": [],
            "relevant_lessons": [],
            "context_prompt": "",
            "total_results": 0,
            "error": str(exc),
        }


def remember_decision_tool(
    symbol: str,
    action: str,
    thesis_summary: str,
    regime: str = "unknown",
    indicators_snapshot: str = "{}",
    backtest_id: str = "",
) -> dict[str, Any]:
    """Store the current trade decision in persistent vector memory.

    Records the decision with full context (symbol, action, thesis,
    regime, indicators) so the system can learn from experience.
    Should be called after every trade decision for record-keeping.

    B2 anti-leakage: the active sim-time (from the strategy context)
    is stamped into ``decision_sim_time`` so future ``query_trade_memory``
    calls can filter out future-bar decisions during backtests.

    Args:
        symbol: Stock symbol (e.g., 'AAPL', 'SPY')
        action: Trade action ('buy', 'sell', or 'hold')
        thesis_summary: Brief summary of the investment thesis
        regime: Current market regime (e.g., 'trending_up')
        indicators_snapshot: JSON string of key indicators at decision time
        backtest_id: Optional backtest run identifier

    Returns:
        dict with keys:
        - **stored** (bool) — whether the decision was successfully stored
        - **decision_id** (str) — the ID of the stored decision
    """
    from src.memory.nexus_vector_memory import get_nexus_memory

    try:
        nexus = get_nexus_memory()
        if not nexus.enabled:
            return {
                "stored": False,
                "decision_id": "",
                "warning": "Vector memory is disabled",
            }

        from src.tools._strategy_context import get_strategy, get_sim_time

        strategy = get_strategy()
        timestamp = (
            strategy.get_datetime().isoformat()
            if strategy and hasattr(strategy, "get_datetime")
            else ""
        )
        # B2: prefer the explicit sim-time if the strategy registered it,
        # else fall back to wall-clock for live mode.
        sim_time = get_sim_time() or timestamp
        decision_id = f"decision_{timestamp}_{symbol}"

        decision = {
            "id": decision_id,
            "symbol": symbol,
            "action": action,
            "regime": regime,
            "thesis_summary": thesis_summary[:500],
            "indicators_snapshot": indicators_snapshot,
            "outcome": "pending",
            "pnl_pct": 0.0,
            "timestamp": timestamp,
            "decision_sim_time": sim_time,
            "strategy_name": "Nexus_Trader",
            "backtest_id": backtest_id,
        }

        stored = nexus.store_decision(decision)
        logger.info("Decision %s stored=%s (sim_time=%s)", decision_id, stored, sim_time)
        return {"stored": stored, "decision_id": decision_id, "decision_sim_time": sim_time}

    except Exception as exc:
        logger.exception("Failed to remember decision")
        return {"stored": False, "decision_id": "", "error": str(exc)}


def remember_lesson_tool(
    text: str,
    category: str = "insight",
    severity: str = "info",
    symbol: str = "",
    regime: str = "",
    tags: str = "[]",
) -> dict[str, Any]:
    """Store a lesson learned in persistent vector memory.

    Captures trading insights, mistakes, patterns, and adaptations
    so future committee runs can reference them.

    Args:
        text: The lesson text — what was learned
        category: Type of lesson: 'mistake', 'insight', 'pattern', or 'adaptation'
        severity: Importance: 'info', 'warning', or 'critical'
        symbol: Related stock symbol if applicable
        regime: Related market regime if applicable
        tags: JSON array of tags, e.g., '["momentum", "overbought"]'

    Returns:
        dict with keys:
        - **stored** (bool) — whether the lesson was successfully stored
        - **lesson_id** (str) — the ID of the stored lesson
    """
    from src.memory.nexus_vector_memory import get_nexus_memory

    try:
        nexus = get_nexus_memory()
        if not nexus.enabled:
            return {
                "stored": False,
                "lesson_id": "",
                "warning": "Vector memory is disabled",
            }

        from src.tools._strategy_context import get_strategy

        strategy = get_strategy()
        timestamp = strategy.get_datetime().isoformat() if strategy and hasattr(strategy, "get_datetime") else ""

        # Parse tags JSON string
        try:
            tags_list = json.loads(tags) if isinstance(tags, str) else tags
        except (json.JSONDecodeError, TypeError):
            tags_list = [t.strip() for t in tags.split(",") if t.strip()]

        import hashlib
        text_hash = hashlib.md5(text.encode()).hexdigest()[:8]
        lesson_id = f"lesson_{timestamp}_{text_hash}"

        lesson = {
            "id": lesson_id,
            "text": text,
            "symbol": symbol,
            "regime": regime,
            "category": category,
            "severity": severity,
            "tags": tags_list,
            "timestamp": timestamp,
            "strategy_name": "Nexus_Trader",
            "source": "committee",
        }

        stored = nexus.store_lesson(lesson)
        logger.info("Lesson %s stored=%s", lesson_id, stored)
        return {"stored": stored, "lesson_id": lesson_id}

    except Exception as exc:
        logger.exception("Failed to remember lesson")
        return {"stored": False, "lesson_id": "", "error": str(exc)}


def query_walkforward_memory_tool(
    symbol: str = "",
    regime: str = "",
    n_results: int = 5,
    min_sortino: float = 0.0,
    only_profitable: bool = False,
    query: str = "",
) -> dict[str, Any]:
    """Search walk-forward OOS evidence for prior strategy performance.

    Walks the ``nexus_walkforward`` LanceDB collection — a *separate*
    table from the live decisions table — to surface OOS evidence for
    strategies previously tested on the given symbol + regime
    combination. Returns per-window Sortino (primary), Sharpe
    (tiebreaker), profitable-window counts, and a synthesized
    context_prompt for inline use in the committee prompt.

    **Sortino is the primary ranking metric** for this tool (penalizes
    only downside volatility — the risk that actually matters for a
    long-biased crypto book). Sharpe is the tiebreaker. The result
    list is re-ranked by ``composite_rank_score`` which weights Sortino
    by win-rate across the (strategy, symbol) pair's full OOS history.

    Use this to give the committee cross-regime, cross-window evidence
    for strategies that are still candidates — much richer than the
    single-window Sharpe from the live StratForge query.

    Args:
        symbol: Filter by ticker (e.g. 'BTC', 'ETH', 'SOL'). Empty = all.
        regime: Filter by regime (e.g. 'trending_up'). Empty = all.
        n_results: Maximum number of windows to return (default 5).
        min_sortino: Minimum per-window Sortino filter (0 = no filter).
            Note: sortino == 0 is treated as "not measured" (legacy data).
        only_profitable: If True, only return profitable windows.
        query: Optional natural-language query to semantically re-rank
            results (e.g. "high-volatility mean-reversion"). If empty,
            uses ``f"{symbol} {regime}"`` as the semantic anchor.

    Returns:
        dict with keys:
        - **walkforward_windows** (list) — OOS window results with metadata
        - **n_windows_returned** (int) — number of windows in the result
        - **symbol** (str) — the queried symbol (echo)
        - **regime** (str) — the queried regime (echo)
        - **context_prompt** (str) — formatted markdown for agent prompts
        - **stats** (dict) — aggregate stats across the full walkforward table
        - **error** (str) — if anything failed
    """
    from src.memory.nexus_vector_memory import get_nexus_memory

    try:
        nexus = get_nexus_memory()
        if not nexus.enabled:
            return {
                "walkforward_windows": [],
                "n_windows_returned": 0,
                "symbol": symbol,
                "regime": regime,
                "context_prompt": "",
                "stats": {},
                "warning": "Vector memory is disabled (lancedb or sentence-transformers unavailable)",
            }

        # Default semantic anchor: the symbol + regime being asked about.
        # We embed this so LanceDB returns candidates most relevant to the
        # actual context (not just lexical filters). The composite_rank_score
        # re-rank then surfaces the strongest-OOS-evidence strategies first.
        if not query:
            query = f"{symbol} {regime}".strip() or "walk-forward OOS evidence"

        results = nexus.search_walkforward(
            query,
            n_results=n_results,
            symbol=symbol or None,
            regime=regime or None,
            min_sortino=min_sortino,
            only_profitable=only_profitable,
        )

        # Build a compact markdown context prompt
        context_lines: list[str] = []
        if results:
            context_lines.append(f"## Walk-Forward OOS Evidence for {symbol or 'all'} in {regime or 'any'} regime\n")
            for i, r in enumerate(results, 1):
                meta = r["metadata"]
                context_lines.append(
                    f"{i}. **{meta.get('strategy_name', '?')}** on **{meta.get('symbol', '?')}** "
                    f"(window {meta.get('window_index', '?')}, "
                    f"test {meta.get('test_start', '?')} → {meta.get('test_end', '?')})\n"
                    f"   - Return: **{meta.get('total_return_pct', 0):+.2f}%** | "
                    f"Sortino: **{meta.get('sortino', 0):.2f}** | "
                    f"Sharpe: {meta.get('sharpe', 0):.2f} | "
                    f"MaxDD: {meta.get('max_drawdown_pct', 0):.2f}%\n"
                    f"   - Profitable: **{'yes' if meta.get('profitable') else 'no'}** | "
                    f"{meta.get('n_profitable_windows', 0)}/"
                    f"{meta.get('n_windows_total', 0)} profitable windows for this strategy+symbol"
                )
                context_lines.append("")

        # Also fetch aggregate stats so the PM sees the global picture
        try:
            stats = nexus.get_walkforward_stats()
        except Exception:
            stats = {}

        return {
            "walkforward_windows": results,
            "n_windows_returned": len(results),
            "symbol": symbol,
            "regime": regime,
            "context_prompt": "\n".join(context_lines),
            "stats": stats,
        }

    except Exception as exc:
        logger.exception("Walkforward memory query failed")
        return {
            "walkforward_windows": [],
            "n_windows_returned": 0,
            "symbol": symbol,
            "regime": regime,
            "context_prompt": "",
            "stats": {},
            "error": str(exc),
        }


def refresh_walkforward_memory_tool(
    rebuild: bool = False,
    only_profitable: bool = False,
    min_windows: int = 1,
) -> dict[str, Any]:
    """Re-seed the ``nexus_walkforward`` LanceDB table from DuckDB.

    Pulls all rows from ``walk_forward_results`` in
    ``nexus_results.duckdb``, computes Sortino per window (or backfills
    it from a heuristic for legacy rows), and embeds + stores them in
    the ``nexus_walkforward`` LanceDB collection.

    Designed to be called from a weekly cron job, but can also be
    triggered manually after a fresh ``walk_forward_validation`` run.

    Venv routing: the Lumibot venv doesn't have lancedb or
    sentence-transformers. This tool spawns a subprocess in the AQOS
    venv (``/home/Zev/development/agentic-quant-os/.venv/bin/python``)
    and waits for completion. From a process already in the AQOS venv
    (e.g. AQS agent), the seeder can be called in-process via
    ``src.memory.walkforward_seeder.seed_walkforward_memory``.

    Args:
        rebuild: Drop existing rows and re-seed from scratch (default False).
        only_profitable: Only seed profitable windows (default False).
        min_windows: Skip (strategy, symbol) pairs with fewer than N windows
            (default 1 = everything).

    Returns:
        dict with keys:
        - **read** (int) — total windows read from DuckDB
        - **eligible** (int) — windows that passed filters
        - **embedded** (int) — windows successfully embedded + stored
        - **skipped** (int) — windows skipped (duplicates)
        - **errors** (int) — windows that failed
        - **table** (str) — the LanceDB table name
        - **rebuild** (bool) — echo of rebuild flag
        - **error_detail** (str) — if anything failed
    """
    from src.memory.walkforward_seeder import seed_via_subprocess

    return seed_via_subprocess(
        rebuild=rebuild,
        only_profitable=only_profitable,
        min_windows=min_windows,
        timeout=900,  # 15 min — embedding 1000s of windows on CPU is slow
    )


def get_memory_stats_tool() -> dict[str, Any]:
    """Get statistics about the Nexus Trader vector memory bank.

    Returns counts of stored decisions and lessons, and whether the
    memory system is operational.

    Returns:
        dict with keys:
        - **enabled** (bool) — whether vector memory is operational
        - **total_decisions** (int) — total decisions stored
        - **total_lessons** (int) — total lessons stored
        - **persist_dir** (str) — LanceDB storage directory
    """
    from src.memory.nexus_vector_memory import get_nexus_memory

    try:
        nexus = get_nexus_memory()
        return nexus.get_stats()
    except Exception as exc:
        logger.exception("Failed to get memory stats")
        return {"enabled": False, "total_decisions": 0, "total_lessons": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    QUERY_TRADE_MEMORY = agent_tool(
        name="query_trade_memory",
        description=(
            "Search the persistent trade memory bank for similar past decisions "
            "and lessons. Uses semantic search to find historical context that "
            "matches the current trade situation. Returns similar decisions with "
            "their outcomes and relevant lessons learned."
        ),
    )(query_trade_memory_tool)

    REMEMBER_DECISION = agent_tool(
        name="remember_decision",
        description=(
            "Store the current trade decision in persistent vector memory for "
            "future reference. Call after every buy/sell/hold decision to build "
            "the experience bank. Includes symbol, action, thesis, regime, and "
            "indicators."
        ),
    )(remember_decision_tool)

    REMEMBER_LESSON = agent_tool(
        name="remember_lesson",
        description=(
            "Store a lesson learned in persistent vector memory. Use for mistakes, "
            "insights, patterns, or adaptations discovered during trading. Future "
            "committee runs will be able to reference this."
        ),
    )(remember_lesson_tool)

    GET_MEMORY_STATS = agent_tool(
        name="get_memory_stats",
        description=(
            "Get statistics about the vector memory bank — how many decisions "
            "and lessons are stored, and whether it's operational."
        ),
    )(get_memory_stats_tool)

    QUERY_WALKFORWARD_MEMORY = agent_tool(
        name="query_walkforward_memory",
        description=(
            "Search the walk-forward OOS evidence bank for prior strategy "
            "performance on the given symbol + regime. Returns per-window "
            "Sortino (primary metric), Sharpe (tiebreaker), profitable-window "
            "counts, and a synthesized context prompt. Use this to surface "
            "cross-window OOS evidence for strategies that are still "
            "candidates. Sortino > Sharpe: penalizes only downside volatility, "
            "which is the risk that matters for a long-biased crypto book."
        ),
    )(query_walkforward_memory_tool)

    REFRESH_WALKFORWARD_MEMORY = agent_tool(
        name="refresh_walkforward_memory",
        description=(
            "Re-seed the walk-forward OOS evidence bank from the DuckDB "
            "walk_forward_results table. Designed for a weekly cron but can "
            "be triggered manually after a fresh walk_forward_validation run. "
            "Routes embedding work through the AQOS venv via subprocess."
        ),
    )(refresh_walkforward_memory_tool)

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorators skipped for trade_memory_tools")
    QUERY_TRADE_MEMORY = query_trade_memory_tool
    REMEMBER_DECISION = remember_decision_tool
    REMEMBER_LESSON = remember_lesson_tool
    GET_MEMORY_STATS = get_memory_stats_tool
    QUERY_WALKFORWARD_MEMORY = query_walkforward_memory_tool
    REFRESH_WALKFORWARD_MEMORY = refresh_walkforward_memory_tool
