"""
Lakehouse Tools — LumiBot @agent_tool wrappers for the Nexus Trader committee.

Each tool function takes ``self`` (the strategy instance) as its first
argument and delegates to :class:`NexusLakehouseReader`.

Follows the same pattern as ``src/tools/trade_memory_tool.py``:
- ``try/except ImportError`` fallback for lumibot
- Module-level constants for each registered tool
- All tools catch exceptions internally — never crash the agent
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_reader():
    """Lazily import the reader to avoid import-time side effects."""
    from src.lakehouse.reader import get_reader
    return get_reader()


def _get_sim_time() -> str:
    """Read the active sim-time from the strategy context (B2 anti-leakage).

    Falls back to ``strategy.get_datetime()`` for live mode and to
    wall-clock only as a last resort (so non-strategy callers don't
    crash). Returns an empty string if no sim-time is available — in
    that case the reader falls back to the non-asof view (legacy
    behavior).
    """
    from src.tools._strategy_context import get_sim_time, get_strategy
    sim = get_sim_time()
    if sim:
        return sim
    try:
        strategy = get_strategy()
        if strategy is not None and hasattr(strategy, "get_datetime"):
            dt = strategy.get_datetime()
            return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Tool functions  (each takes ``self`` — the strategy instance)
# ---------------------------------------------------------------------------

def lakehouse_regime(
    ticker: str = "SPY",
) -> dict[str, Any]:
    """Get the latest composite market regime for a ticker.

    Returns the Regime Intelligence state (regime_label, confidence,
    component scores, volatility regime, trend direction, etc.).

    Args:
        ticker: Stock/crypto ticker, e.g. 'SPY', 'BTC', 'AAPL'.

    Returns:
        dict with regime fields, or empty dict on failure.
    """
    try:
        reader = _get_reader()
        regime = reader.get_regime(ticker)
        if not regime:
            return {"regime": "unknown", "ticker": ticker.upper(), "note": "No regime data found"}
        return regime
    except Exception as exc:
        logger.warning("lakehouse_regime(%s) failed: %s", ticker, exc)
        return {"regime": "error", "ticker": ticker.upper(), "error": str(exc)}


def lakehouse_signals(
    ticker: str = "",
    min_confidence: float = 0.5,
    limit: int = 20,
) -> dict[str, Any]:
    """Get curated signal feed from the lakehouse.

    Returns validated signals with confidence scores, optionally filtered
    by ticker and minimum confidence threshold.

    Args:
        ticker: Optional ticker filter (blank = all tickers).
        min_confidence: Minimum signal confidence (0.0-1.0).
        limit: Max signals to return.

    Returns:
        dict with ``signals`` list and ``count``.
    """
    try:
        reader = _get_reader()
        signals = reader.get_signals(
            ticker=ticker,
            signal_type="",
            min_confidence=min_confidence,
            limit=limit,
        )
        return {"signals": signals, "count": len(signals)}
    except Exception as exc:
        logger.warning("lakehouse_signals() failed: %s", exc)
        return {"signals": [], "count": 0, "error": str(exc)}


def lakehouse_factors(
    ticker: str = "",
) -> dict[str, Any]:
    """Get factor snapshot from the alpha-factory pipeline.

    Returns computed factor values (momentum, mean-reversion, volatility,
    etc.) for the given ticker.

    Args:
        ticker: Optional ticker filter (blank = all tickers).

    Returns:
        dict with ``factors`` list and ``count``.
    """
    try:
        reader = _get_reader()
        factors = reader.get_factors(ticker=ticker)
        return {"factors": factors, "count": len(factors)}
    except Exception as exc:
        logger.warning("lakehouse_factors() failed: %s", exc)
        return {"factors": [], "count": 0, "error": str(exc)}


def lakehouse_strategy_candidates(
    regime: str = "",
    min_composite: float = 49.0,
    min_sharpe: float = 0.0,
    min_sortino: float = 0.0,
    ticker: str = "",
    limit: int = 10,
    asset_class: str = "crypto",
) -> dict[str, Any]:
    """Get validated strategy candidates from the lakehouse.

    Asset-class routing (2026-06-26):
      - ``asset_class='crypto'`` (default) → reads from
        ``v_nexus_strategy_pool_crypto`` (the canonical crypto pool;
        BTC/ETH/SOL/ALL/MULTI tickers with sortino/composite_score).
      - ``asset_class='stocks'`` → reads from
        ``v_nexus_strategy_pool_stocks`` (20 MAS strategies;
        Sharpe-only, no Sortino/composite). ``min_composite`` is
        ignored for stocks (default 49.0 wouldn't match); callers
        should pass ``min_composite=0`` or rely on ``min_sharpe``.
      - ``asset_class=''`` or ``'all'`` → legacy ``v_nexus_strategy_pool``
        (back-compat).

    Sortino is the headline ranking metric for crypto (penalizes only
    downside vol, which is the risk that actually hurts PnL for
    long-biased crypto). For stocks Sortino is NULL so the reader
    falls back to Sharpe-first ordering automatically.

    Args:
        regime: Optional regime label filter (blank = all regimes).
        min_composite: Minimum composite score (default 49.0, the WF gate;
            ignored for stocks).
        min_sharpe: Minimum in-sample Sharpe ratio.
        min_sortino: Minimum in-sample Sortino ratio (default 0.0 = no
            filter; e.g. 0.5 to require Sortino > 0.5).
        ticker: Filter by ticker (e.g. 'BTC'). For stocks pass empty
            string or a category like 'TECH'.
        limit: Max strategies to return.
        asset_class: ``"crypto"`` (default), ``"stocks"``, or ``""``.

    Returns:
        dict with ``strategies`` list (each row carries ``sortino`` as
        the headline + ``sharpe`` as tiebreaker + ``sortino_headline``
        flag + ``note`` if fallback was used), ``count``, ``note``
        if the result is Sharpe-only, and ``asset_class`` echoed back
        for the agent's awareness.
    """
    try:
        reader = _get_reader()
        # Sortino-first ranking by default — the reader's ORDER BY clause
        # is "sortino DESC NULLS LAST, sharpe DESC NULLS LAST, composite_score DESC NULLS LAST".
        # ``sort_by='sortino'`` is now the default in the reader; pass it
        # explicitly so this function is self-documenting.
        # B2 anti-leakage (2026-06-25): pass the active sim-time as
        # ``as_of`` so the reader routes the query through the asof macro
        # and only returns rows whose as_of_timestamp <= current sim bar.
        sim_time = _get_sim_time()
        # Stocks default to min_composite=0 since their composite_score
        # is NULL; using the crypto default (49.0) would silently drop
        # all rows. The caller can still override.
        if asset_class == "stocks" and min_composite == 49.0:
            effective_min_composite = 0.0
        else:
            effective_min_composite = min_composite
        strategies = reader.get_strategy_pool(
            regime_label=regime,
            min_composite=effective_min_composite,
            min_sharpe=min_sharpe,
            min_sortino=min_sortino,
            ticker=ticker,
            sort_by="sortino",
            limit=limit,
            as_of=sim_time,
            asset_class=asset_class,
        )

        # Detect whether the upstream table is Sharpe-only (no Sortino
        # column populated). If every row has a NULL sortino, the data
        # is "sharpe_only" — flag it so the LLM knows the ranking is
        # by Sharpe and the Sortino commentary in the PM prompt is
        # inapplicable to this batch.
        sharpe_only = bool(strategies) and all(
            row.get("sortino") is None for row in strategies
        )

        # Surface Sortino as the headline metric in each row. The reader
        # already returns the row dict with ``sortino`` and ``sharpe``
        # columns; we just normalize the field name and add a flag so
        # downstream agents can pin the primary metric without parsing.
        for row in strategies:
            row.setdefault("sortino", row.get("sortino"))
            row.setdefault("sharpe", row.get("sharpe"))
            row["sortino_headline"] = (
                row.get("sortino") if not sharpe_only else None
            )
            if sharpe_only:
                row["note"] = "sharpe_only"

        result: dict[str, Any] = {
            "strategies": strategies,
            "count": len(strategies),
            "as_of_sim_time": sim_time,
            "asset_class": asset_class,
        }
        if sharpe_only:
            result["note"] = "sharpe_only"
        return result
    except Exception as exc:
        logger.warning("lakehouse_strategy_candidates() failed: %s", exc)
        return {"strategies": [], "count": 0, "error": str(exc)}


def lakehouse_catalyst(
    ticker: str,
) -> dict[str, Any]:
    """Get catalyst grade for a ticker.

    Returns event-driven catalyst analysis with grade, impact assessment,
    and timing information.

    Args:
        ticker: Stock/crypto ticker.

    Returns:
        dict with catalyst fields, or empty dict if not found.
    """
    try:
        reader = _get_reader()
        catalyst = reader.get_catalyst(ticker)
        if not catalyst:
            return {"ticker": ticker.upper(), "catalyst_grade": "none", "note": "No catalyst data"}
        return catalyst
    except Exception as exc:
        logger.warning("lakehouse_catalyst(%s) failed: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc)}


def lakehouse_experience(
    query_type: str = "all",
    ticker: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Search the experience bank for relevant lessons.

    Returns lessons, mistakes, patterns, and adaptations from past
    trading activity.

    Args:
        query_type: 'all', 'mistakes', 'patterns', 'adaptations', or 'insights'.
        ticker: Optional ticker filter.
        limit: Max results.

    Returns:
        dict with ``experience`` list and ``count``.
    """
    try:
        reader = _get_reader()
        severity = ""
        if query_type == "mistakes":
            severity = "critical"
        elif query_type == "adaptations":
            severity = "warning"

        experience = reader.get_experience(
            ticker=ticker,
            severity=severity,
            limit=limit,
        )
        return {"experience": experience, "count": len(experience)}
    except Exception as exc:
        logger.warning("lakehouse_experience() failed: %s", exc)
        return {"experience": [], "count": 0, "error": str(exc)}


def lakehouse_preflight(
    strategy_name: str = "",
    ticker: str = "",
) -> dict[str, Any]:
    """Pre-flight failure/risk check before trading.

    Aggregates known failures for the strategy and ticker, plus relevant
    experience lessons.  Use this BEFORE committing to a trade.

    Args:
        strategy_name: Strategy to check (blank = all).
        ticker: Ticker to check (blank = all).

    Returns:
        dict with ``failures``, ``experience``, and ``warnings`` list.
    """
    try:
        reader = _get_reader()
        failures = reader.get_failures(strategy_name=strategy_name, limit=10)
        experience = reader.get_experience(ticker=ticker, severity="critical", limit=5)

        warnings = []
        if failures:
            warnings.append(f"{len(failures)} known failure(s) for this strategy")
        if experience:
            warnings.append(f"{len(experience)} critical lesson(s) from experience bank")

        return {
            "failures": failures,
            "experience": experience,
            "warnings": warnings,
            "clear": len(warnings) == 0,
        }
    except Exception as exc:
        logger.warning("lakehouse_preflight() failed: %s", exc)
        return {"failures": [], "experience": [], "warnings": [], "clear": True, "error": str(exc)}


def lakehouse_intelligence(
    ticker: str,
) -> dict[str, Any]:
    """Get FULL intelligence packet for a ticker in one call.

    Aggregates regime, signals, factors, catalyst, experience, failures,
    and regime-strategy map into a single comprehensive packet.  This is
    the primary tool for committee agents to get complete context.

    Args:
        ticker: Stock/crypto ticker.

    Returns:
        dict with all intelligence sections.
    """
    try:
        reader = _get_reader()
        intel = reader.get_ticker_intelligence(ticker)
        return intel
    except Exception as exc:
        logger.warning("lakehouse_intelligence(%s) failed: %s", ticker, exc)
        return {"ticker": ticker.upper(), "error": str(exc)}


def lakehouse_write_lesson(
    text: str,
    category: str = "insight",
    severity: str = "info",
    ticker: str = "",
    regime: str = "",
    tags: str = "[]",
) -> dict[str, Any]:
    """Write a lesson to the lakehouse ecosystem.

    Persists a trading lesson, mistake, pattern, or adaptation so the
    full agentic-quant-os ecosystem can learn from it.

    Args:
        text: The lesson content.
        category: 'mistake', 'insight', 'pattern', or 'adaptation'.
        severity: 'info', 'warning', or 'critical'.
        ticker: Related ticker if applicable.
        regime: Current market regime.
        tags: JSON array string of tags, e.g. '["momentum", "overbought"]'.

    Returns:
        dict with ``stored`` bool and ``lesson_id``.
    """
    try:
        reader = _get_reader()
        from datetime import datetime, timezone

        try:
            tags_list = json.loads(tags) if isinstance(tags, str) else tags
        except (json.JSONDecodeError, TypeError):
            tags_list = [t.strip() for t in tags.split(",") if t.strip()]

        from src.tools._strategy_context import get_strategy

        strategy = get_strategy()
        timestamp = (
            strategy.get_datetime().isoformat()
            if strategy and hasattr(strategy, "get_datetime")
            else datetime.now(timezone.utc).isoformat()
        )

        record = {
            "detail": text,
            "title": f"Lesson: {category}",
            "category": category,
            "severity": severity,
            "ticker": ticker if ticker else "",
            "regime": regime,
            "tags": tags_list,
            "source": "nexus_trader_committee",
        }

        stored = reader.write_lesson(record)
        return {"stored": stored, "lesson_id": f"lesson_{timestamp}"}
    except Exception as exc:
        logger.warning("lakehouse_write_lesson() failed: %s", exc)
        return {"stored": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    LAKEHOUSE_REGIME = agent_tool(
        name="lakehouse_regime",
        description=(
            "Get the latest composite market regime for a ticker from the "
            "lakehouse. Returns regime label (e.g. trending_up, mean_reverting, "
            "volatile), confidence score, component breakdowns, volatility regime, "
            "and trend direction. Use this as the first step in trade evaluation."
        ),
    )(lakehouse_regime)

    LAKEHOUSE_SIGNALS = agent_tool(
        name="lakehouse_signals",
        description=(
            "Get curated signal feed from the lakehouse. Returns validated "
            "trading signals with confidence scores, source, and timing. "
            "Filter by ticker and minimum confidence. Use to check what "
            "the quant system is signaling for a given asset."
        ),
    )(lakehouse_signals)

    LAKEHOUSE_FACTORS = agent_tool(
        name="lakehouse_factors",
        description=(
            "Get factor snapshot from the alpha-factory pipeline. Returns "
            "computed factor values (momentum, mean-reversion, volatility, "
            "volume profile, etc.) for a ticker. Use to understand the "
            "quantitative factor landscape for an asset."
        ),
    )(lakehouse_factors)

    LAKEHOUSE_STRATEGY_CANDIDATES = agent_tool(
        name="lakehouse_strategy_candidates",
        description=(
            "Get validated crypto strategy candidates from the lakehouse. "
            "Returns strategies from backtest_results_v2 (BTC, ETH, SOL, ALL, MULTI) "
            "ranked **Sortino-first** (penalizes only downside volatility — "
            "the risk that actually hurts a long-biased book), then Sharpe "
            "as tiebreaker, then composite score. All strategies have "
            "composite >= 49 and status winner or tested. If the upstream "
            "table is Sharpe-only, the response includes `note: 'sharpe_only'`. "
            "Use to find the best strategies for the current market regime."
        ),
    )(lakehouse_strategy_candidates)

    LAKEHOUSE_CATALYST = agent_tool(
        name="lakehouse_catalyst",
        description=(
            "Get catalyst grade for a ticker from the lakehouse. Returns "
            "event-driven catalyst analysis with grade, impact assessment, "
            "and timing. Use to understand upcoming catalysts that may "
            "affect the trade thesis."
        ),
    )(lakehouse_catalyst)

    LAKEHOUSE_EXPERIENCE = agent_tool(
        name="lakehouse_experience",
        description=(
            "Search the experience bank for relevant lessons, mistakes, "
            "patterns, and adaptations from past trading. Filter by type "
            "and ticker. Use to learn from historical context before "
            "making a decision."
        ),
    )(lakehouse_experience)

    LAKEHOUSE_PREFLIGHT = agent_tool(
        name="lakehouse_preflight",
        description=(
            "Pre-flight failure/risk check before trading. Aggregates known "
            "failures for a strategy and relevant critical lessons from the "
            "experience bank. Always call this BEFORE committing to a trade "
            "to check for known risks."
        ),
    )(lakehouse_preflight)

    LAKEHOUSE_INTELLIGENCE = agent_tool(
        name="lakehouse_intelligence",
        description=(
            "Get FULL intelligence packet for a ticker in one call. Aggregates "
            "regime, signals, factors, catalyst, experience, failures, and "
            "regime-strategy mapping. This is the primary comprehensive tool "
            "for committee agents to get complete context on an asset."
        ),
    )(lakehouse_intelligence)

    LAKEHOUSE_WRITE_LESSON = agent_tool(
        name="lakehouse_write_lesson",
        description=(
            "Write a lesson to the lakehouse ecosystem. Persists trading "
            "insights, mistakes, patterns, or adaptations so the full "
            "quant system can learn. Use after important trades or when "
            "discovering new patterns."
        ),
    )(lakehouse_write_lesson)

except ImportError:
    logger.debug(
        "lumibot not available — @agent_tool decorators skipped for lakehouse tools"
    )
    LAKEHOUSE_REGIME = lakehouse_regime
    LAKEHOUSE_SIGNALS = lakehouse_signals
    LAKEHOUSE_FACTORS = lakehouse_factors
    LAKEHOUSE_STRATEGY_CANDIDATES = lakehouse_strategy_candidates
    LAKEHOUSE_CATALYST = lakehouse_catalyst
    LAKEHOUSE_EXPERIENCE = lakehouse_experience
    LAKEHOUSE_PREFLIGHT = lakehouse_preflight
    LAKEHOUSE_INTELLIGENCE = lakehouse_intelligence
    LAKEHOUSE_WRITE_LESSON = lakehouse_write_lesson
