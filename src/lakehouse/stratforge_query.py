"""
StratForge Query Tool — LumiBot @agent_tool for querying the StratForge lakehouse
=====================================================================================

Provides the ``query_stratforge_strategies`` agent tool that lets the committee
Portfolio Manager discover best-fit strategies from the StratForge DuckDB
lakehouse during the debate.

Reads from ``~/.hermes/profiles/herm-bot/home/agentic-quant-os/data/quant.duckdb``
via the existing :class:`StratForgeBridge` (read-only connections).

Registered as ``QUERY_STRATFORGE_STRATEGIES`` for import by ``nexus_committee.py``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def query_stratforge_strategies(
    symbol: str = "BTC",
    min_composite: float = 0,
    min_wf_sharpe: float = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """Discover best-fit trading strategies from the StratForge lakehouse.

    Queries the validated strategy database for strategies tested on the given
    symbol, ranked by walk-forward test Sharpe ratio. Returns key metadata so
    the portfolio manager can decide which strategy to load.

    Args:
        symbol: Ticker symbol to filter by (e.g. 'BTC', 'ETH', 'SOL').
                Use '' for all symbols.
        min_composite: Minimum composite score (0-100) to filter.
        min_wf_sharpe: Minimum walk-forward average test Sharpe ratio.
        limit: Maximum number of strategies to return (default 10).

    Returns:
        dict with keys:
        - **strategies** (list[dict]) — top strategies ranked by WF Sharpe, each with:
          - strategy_name, source_composite, wf_test_sharpe, n_windows,
            wf_pass, ticker, total_return, max_drawdown, sortino, calmar
        - **count** (int) — number of strategies returned
        - **symbol** (str) — the queried symbol
    """
    try:
        from src.lakehouse.stratforge_bridge import StratForgeBridge

        bridge = StratForgeBridge()

        # Build query against the actual schema
        query = """
            SELECT DISTINCT ON (strategy_name)
                strategy_name,
                ticker,
                composite_score,
                avg_test_sharpe,
                wf_pass,
                n_windows,
                total_return,
                max_drawdown,
                sortino,
                calmar,
                sharpe,
                regime_label,
                archetype
            FROM backtest_results_v2
            WHERE status = 'winner'
              AND source_code IS NOT NULL
              AND LENGTH(source_code) > 100
        """
        params: list = []

        if symbol:
            query += " AND UPPER(ticker) = ?"
            params.append(symbol.upper())

        if min_composite > 0:
            query += " AND composite_score >= ?"
            params.append(min_composite)

        if min_wf_sharpe > 0:
            query += " AND avg_test_sharpe >= ?"
            params.append(min_wf_sharpe)

        query += """
            ORDER BY strategy_name, is_best_version DESC NULLS LAST,
                     avg_test_sharpe DESC NULLS LAST
            LIMIT ?
        """
        params.append(limit)

        df = bridge._read_query(query, params)

        if df.empty:
            return {
                "strategies": [],
                "count": 0,
                "symbol": symbol,
                "message": f"No strategies found for {symbol or 'any'} with the given filters",
            }

        # Convert to list of dicts with clean field names
        strategies = []
        for _, row in df.iterrows():
            def _safe_float(val, default=0.0):
                try:
                    if val is None or (hasattr(val, '__class__') and 'NA' in str(val)):
                        return default
                    return float(val)
                except (TypeError, ValueError):
                    return default

            def _safe_int(val, default=0):
                try:
                    if val is None or (hasattr(val, '__class__') and 'NA' in str(val)):
                        return default
                    return int(val)
                except (TypeError, ValueError):
                    return default

            def _safe_bool(val, default=False):
                try:
                    if val is None or (hasattr(val, '__class__') and 'NA' in str(val)):
                        return default
                    return bool(val)
                except (TypeError, ValueError):
                    return default

            strategies.append({
                "strategy_name": str(row.get("strategy_name", "") or ""),
                "ticker": str(row.get("ticker", "") or ""),
                "source_composite": _safe_float(row.get("composite_score")),
                "wf_avg_sharpe": _safe_float(row.get("avg_test_sharpe")),
                "wf_pass": _safe_bool(row.get("wf_pass")),
                "num_windows": _safe_int(row.get("n_windows")),
                "total_return_pct": _safe_float(row.get("total_return")),
                "max_drawdown_pct": _safe_float(row.get("max_drawdown")),
                "sortino": _safe_float(row.get("sortino")),
                "calmar": _safe_float(row.get("calmar")),
                "sharpe": _safe_float(row.get("sharpe")),
                "regime_label": str(row.get("regime_label", "") or ""),
                "archetype": str(row.get("archetype", "") or ""),
            })

        logger.info(
            "query_stratforge_strategies(%s): returned %d strategies",
            symbol, len(strategies),
        )

        return {
            "strategies": strategies,
            "count": len(strategies),
            "symbol": symbol,
        }

    except Exception as exc:
        logger.warning("query_stratforge_strategies(%s) failed: %s", symbol, exc)
        return {
            "strategies": [],
            "count": 0,
            "symbol": symbol,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# LumiBot @agent_tool registration
# ---------------------------------------------------------------------------

try:
    from lumibot.components.agents.tools import agent_tool

    QUERY_STRATFORGE_STRATEGIES = agent_tool(
        name="query_stratforge_strategies",
        description=(
            "Discover best-fit trading strategies from the StratForge lakehouse. "
            "Queries the validated strategy database for strategies tested on a "
            "given symbol, ranked by walk-forward Sharpe ratio. Returns strategy "
            "name, composite score, WF Sharpe, pass status, and key metrics. "
            "Use this to find validated strategies for the current market conditions."
        ),
    )(query_stratforge_strategies)

except ImportError:
    logger.debug(
        "lumibot not available — @agent_tool decorator skipped for stratforge_query"
    )
    QUERY_STRATFORGE_STRATEGIES = query_stratforge_strategies
