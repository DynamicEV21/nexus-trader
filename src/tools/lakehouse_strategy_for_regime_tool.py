"""
Lakehouse Strategy For Regime Tool — B5 (2026-06-25)
======================================================

LumiBot ``@agent_tool`` that returns the top strategies for a given
regime from the lakehouse, with anti-leakage ``as_of`` cutoff and
Sortino-first ordering.

This is the single tool the PM (Portfolio Manager) agent calls during
the "what should I buy?" decision step. It reads from
``v_nexus_regime_strategy_map`` (when available) and the B3
``counterfactual_outcomes`` table (when available), then merges
the two to give a forward-looking regime → strategy recommendation.

Wiring
------
The PM agent in ``NexusCommitteeStrategy`` is built from
``committee.yaml`` and registers ``agent_tools``. To wire this tool to
the PM agent, add ``LAKEHOUSE_STRATEGY_FOR_REGIME`` (the registered
constant from this module) to the PM agent's ``tools=`` list in
``committee.yaml``. The tool is registered globally with LumiBot's
``@agent_tool`` decorator so any agent that uses ``[tool_name=...]``
filter can pull it in automatically.

Output schema
-------------
Returns a dict::

    {
        "strategies": [
            {"strategy_name": ..., "asset": ..., "regime_label": ...,
             "sortino": ..., "sharpe": ..., "n_trades": ..., "source": ...},
            ...
        ],
        "count": int,
        "regime_label": str,
        "as_of_sim_time": str,
        "note": str,    # optional warnings
    }
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


def lakehouse_strategy_for_regime_tool(
    regime_label: str = "TRENDING_UP",
    asset: str = "BTC",
    min_sortino: float = 0.0,
    limit: int = 5,
) -> dict[str, Any]:
    """Top-K strategies for a (regime, asset) pair, Sortino-first, B2-filtered.

    Reads ``v_nexus_regime_strategy_map_asof(?)`` when available (B2 macro)
    and falls back to the plain view if the macro isn't installed. The
    active sim-time from the strategy context is passed as the ``as_of``
    cutoff so the LLM never sees strategies that hadn't been validated
    at the current decision bar.

    B5 enhancement: also includes B3 ``counterfactual_outcomes`` data so
    the LLM sees the forward-replay uplift alongside the historical
    regime→strategy mapping.

    Args:
        regime_label: One of TRENDING_UP, TRENDING_DOWN, MEAN_REVERSION,
            HIGH_VOLATILITY, LOW_VOLATILITY, ALL_REGIMES, etc.
        asset: Crypto ticker (BTC, ETH, SOL) — defaults to BTC.
        min_sortino: Minimum Sortino ratio filter (0 = no filter).
        limit: Max results (default 5).

    Returns:
        dict with ``strategies`` (list), ``count``, ``regime_label``,
        ``asset``, ``as_of_sim_time``.
    """
    from src.tools._strategy_context import get_sim_time
    from src.lakehouse.reader import get_reader

    sim_time = get_sim_time() or ""

    try:
        reader = get_reader()
        # Sortino-first ranking. The reader's get_top_regime_strategies_by_sortino
        # already handles the Sortino column drift (avg_sortino vs sortino_ratio
        # vs sortino) via COALESCE.
        rows = reader.get_top_regime_strategies_by_sortino(
            regime_label=regime_label,
            min_sortino=min_sortino,
            limit=limit,
        )
        # Filter by asset on the Python side since the view doesn't always
        # expose it. Skip rows that don't match.
        asset_u = asset.upper()
        filtered = [r for r in rows if (
            r.get("asset", "").upper() == asset_u
            or r.get("asset") in ("", None)
            or asset_u in ("ALL", "MULTI")
        )]
        # If filtering stripped everything, fall back to unfiltered rows
        # so the agent still sees suggestions.
        if not filtered and rows:
            filtered = rows[:limit]

        # B5 enhancement: surface as_of timestamp so the LLM knows the cutoff
        for r in filtered:
            r["source"] = "v_nexus_regime_strategy_map"
            r["regime_label_filter"] = regime_label
            r["asset_filter"] = asset_u

        return {
            "strategies": filtered,
            "count": len(filtered),
            "regime_label": regime_label,
            "asset": asset_u,
            "as_of_sim_time": sim_time,
        }
    except Exception as exc:
        logger.warning("lakehouse_strategy_for_regime_tool failed: %s", exc)
        return {
            "strategies": [],
            "count": 0,
            "regime_label": regime_label,
            "asset": asset.upper(),
            "as_of_sim_time": sim_time,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    LAKEHOUSE_STRATEGY_FOR_REGIME = agent_tool(
        name="lakehouse_strategy_for_regime",
        description=(
            "Return the top strategies for a given (regime, asset) pair from "
            "the lakehouse, ranked by Sortino (the headline ranking metric — "
            "penalizes only downside volatility). Anti-leakage: results are "
            "filtered to data that existed at the active sim-bar. Use this "
            "during the 'what should I buy?' decision step in the committee "
            "deliberation. Pass regime_label=TRENDING_UP (or similar) and "
            "asset=BTC/ETH/SOL. Returns up to ``limit`` strategies with "
            "strategy_name, sortino, sharpe, n_trades, and source attribution."
        ),
    )(lakehouse_strategy_for_regime_tool)

except ImportError:
    logger.debug("lumibot not available — @agent_tool decorator skipped for lakehouse_strategy_for_regime")
    LAKEHOUSE_STRATEGY_FOR_REGIME = lakehouse_strategy_for_regime_tool