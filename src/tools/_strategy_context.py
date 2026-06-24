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
