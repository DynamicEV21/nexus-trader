"""
Strategy Context Registry
=========================

Module-level registry that lets @agent_tool functions access the active
LumiBot strategy instance WITHOUT taking ``self`` as a parameter.

This solves the self-binding bug: when LumiBot's agent system (via Google
ADK's FunctionTool) generates the tool schema from the function signature,
it includes ``self`` as a required parameter. The AI model then passes a
string placeholder for ``self``, causing ``self.get_historical_prices()``
and similar calls to fail with AttributeError.

Usage
-----
In the strategy's ``initialize()`` method::

    from src.tools._strategy_context import register_strategy
    register_strategy(self)

In any @agent_tool function::

    from src.tools._strategy_context import get_strategy

    def my_tool(symbol: str):
        strategy = get_strategy()
        prices = strategy.get_historical_prices(symbol, length=100)
        ...
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lumibot.strategies.strategy import Strategy

logger = logging.getLogger(__name__)

_strategy: Any = None
# B2 anti-leakage (2026-06-25): the active sim-bar datetime (ISO string)
# that agent tools should use when filtering time-sensitive queries.
# Set by ``NexusCommitteeStrategy`` / ``PaperTradeCommitteeStrategy`` at
# the start of every ``on_trading_iteration()`` and cleared at the end.
_sim_time: str | None = None


def register_strategy(strategy: Any) -> None:
    """Register the active LumiBot strategy instance.

    Called once during ``NexusCommitteeStrategy.initialize()``.
    """
    global _strategy
    _strategy = strategy
    logger.debug("Strategy registered with _strategy_context: %s", strategy.__class__.__name__)


def get_strategy() -> Any:
    """Return the active strategy instance, or None if not registered.

    Tools should handle None gracefully (e.g., return an error dict).
    """
    return _strategy


def clear_strategy() -> None:
    """Clear the registered strategy (useful for testing)."""
    global _strategy
    _strategy = None


def register_sim_time(sim_time_iso: str | None) -> None:
    """Register the active sim-bar datetime (ISO format).

    The sim-time is the ``strategy.get_datetime()`` snapshot taken at
    the start of an ``on_trading_iteration()``. Agent tools use it as
    the as-of cutoff for lakehouse and vector-memory queries to
    prevent look-ahead bias.

    Pass ``None`` to clear (e.g., at end of iteration or in tests).
    """
    global _sim_time
    _sim_time = sim_time_iso
    logger.debug("Sim-time registered with _strategy_context: %s", sim_time_iso)


def get_sim_time() -> str | None:
    """Return the active sim-time (ISO string) or None if not registered.

    Agent tools should fall back to ``strategy.get_datetime()`` if this
    returns None (live mode), and to wall-clock if even that fails.
    """
    return _sim_time


def clear_sim_time() -> None:
    """Clear the registered sim-time (useful for testing)."""
    global _sim_time
    _sim_time = None
