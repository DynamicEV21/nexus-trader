"""
Nexus Lakehouse Reader — curated data from agentic-quant-os DuckDB lakehouse.

Wraps QuantClient's read_nexus_* methods as a lazy-inited singleton for
the Nexus Trader committee agents.
"""

from .reader import NexusLakehouseReader, get_reader

__all__ = ["NexusLakehouseReader", "get_reader"]
