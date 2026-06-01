"""
Trade Memory Tool — LumiBot @agent_tool for semantic memory search
===================================================================

Provides AI agents with the ability to query the NexusTrade vector
memory for similar past decisions, relevant lessons, and historical
context.  This allows the investment committee to learn from history.

The tool wraps :class:`NexusVectorMemory` methods as callable
agent tools that receive ``self`` (the strategy instance).

Requirements
------------
* ``GOOGLE_API_KEY`` environment variable for embeddings.
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
    self,
    query: str,
    symbol: str = "",
    regime: str = "",
    n_results: int = 5,
) -> dict[str, Any]:
    """Search the trade memory bank for similar past decisions and lessons.

    Uses semantic similarity (vector search) to find historical trade
    decisions and lessons that match the current context.  This helps
    the investment committee learn from prior experience.

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
    """
    from src.memory.nexus_vector_memory import get_nexus_memory

    try:
        nexus = get_nexus_memory()
        if not nexus.enabled:
            return {
                "similar_decisions": [],
                "relevant_lessons": [],
                "context_prompt": "",
                "total_results": 0,
                "warning": "Vector memory is disabled (GOOGLE_API_KEY not set or LanceDB unavailable)",
            }

        decisions = nexus.search_similar_decisions(
            query, n_results=n_results, symbol=symbol or None, regime=regime or None,
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
    self,
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

        timestamp = self.get_datetime().isoformat() if hasattr(self, "get_datetime") else ""
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
            "strategy_name": "NexusTrade",
            "backtest_id": backtest_id,
        }

        stored = nexus.store_decision(decision)
        logger.info("Decision %s stored=%s", decision_id, stored)
        return {"stored": stored, "decision_id": decision_id}

    except Exception as exc:
        logger.exception("Failed to remember decision")
        return {"stored": False, "decision_id": "", "error": str(exc)}


def remember_lesson_tool(
    self,
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

        timestamp = self.get_datetime().isoformat() if hasattr(self, "get_datetime") else ""

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
            "strategy_name": "NexusTrade",
            "source": "committee",
        }

        stored = nexus.store_lesson(lesson)
        logger.info("Lesson %s stored=%s", lesson_id, stored)
        return {"stored": stored, "lesson_id": lesson_id}

    except Exception as exc:
        logger.exception("Failed to remember lesson")
        return {"stored": False, "lesson_id": "", "error": str(exc)}


def get_memory_stats_tool(self) -> dict[str, Any]:
    """Get statistics about the NexusTrade vector memory bank.

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

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorators skipped for trade_memory_tools")
    QUERY_TRADE_MEMORY = query_trade_memory_tool
    REMEMBER_DECISION = remember_decision_tool
    REMEMBER_LESSON = remember_lesson_tool
    GET_MEMORY_STATS = get_memory_stats_tool
