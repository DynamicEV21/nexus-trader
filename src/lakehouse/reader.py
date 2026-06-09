"""
NexusLakehouseReader — Lazy-inited wrapper around DuckDB curated views.

Reads validated data from the agentic-quant-os DuckDB lakehouse and surfaces
it to the committee agents via thin delegation methods.  Never crashes — all
methods catch exceptions and return empty / None on failure.

All reads go through curated views (v_nexus_*) — never raw tables.
Ticker values are passed as-is (no case mangling) since the lakehouse
stores mixed formats (SPY, BTC-USD, BTCUSDT, XBTUSD).

Usage::

    from src.lakehouse import get_reader

    lakehouse = get_reader()
    regime = lakehouse.get_regime("BTC-USD")
    intel  = lakehouse.get_ticker_intelligence("AAPL")
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure agentic-quant-os src is importable for QuantClient
# ---------------------------------------------------------------------------
_AQOS_SRC = os.path.expanduser("~/development/agentic-quant-os/src")
if _AQOS_SRC not in sys.path:
    sys.path.insert(0, _AQOS_SRC)

_DEFAULT_DB_PATH = os.path.expanduser(
    os.environ.get("NEXUS_LAKEHOUSE_PATH", "~/development/agentic-quant-os/data/quant.duckdb")
)


class NexusLakehouseReader:
    """Lazy-inited lakehouse reader using curated DuckDB views.

    Parameters
    ----------
    db_path : str | None
        Path to the DuckDB file.  Defaults to ``NEXUS_LAKEHOUSE_PATH`` env var
        or ``~/agentic-quant-os/data/quant.duckdb``.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._con: Any | None = None

    # ── Lazy connection (read-only, no bootstrap) ─────────────────────

    def _con_get(self) -> Any:
        """Lazily open a read-only DuckDB connection."""
        if self._con is not None:
            return self._con
        try:
            import duckdb
            self._con = duckdb.connect(self._db_path, read_only=True)
            logger.info("Lakehouse reader connected to %s", self._db_path)
        except Exception as exc:
            logger.error("Failed to connect to lakehouse: %s", exc)
            self._con = None
        return self._con

    def _query(self, sql: str, params: list | None = None) -> list[dict[str, Any]]:
        """Execute a read query and return list of dicts."""
        con = self._con_get()
        if con is None:
            return []
        try:
            result = con.execute(sql, params or [])
            columns = [d[0] for d in result.description]
            rows = result.fetchall()
            return [dict(zip(columns, r)) for r in rows]
        except Exception as exc:
            logger.warning("Lakehouse query failed: %s", exc)
            return []

    def _query_one(self, sql: str, params: list | None = None) -> dict[str, Any] | None:
        """Execute a query and return the first row as a dict, or None."""
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ── Regime ───────────────────────────────────────────────────────

    def get_regime(self, ticker: str) -> dict[str, Any]:
        """Get latest composite regime for *ticker* from v_nexus_regime.

        The curated view filters to composite/ensemble detector outputs only.
        """
        return self._query_one(
            "SELECT * FROM v_nexus_regime WHERE ticker = ?", [ticker]
        ) or {}

    def get_all_regimes(self) -> list[dict[str, Any]]:
        """Get latest composite regime for every tracked ticker."""
        return self._query("SELECT * FROM v_nexus_regime")

    # ── Signals ──────────────────────────────────────────────────────

    def get_signals(
        self,
        ticker: str = "",
        signal_type: str = "",
        min_confidence: float = 0.5,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Curated signal feed from v_nexus_signal_feed.

        Returns validated signals from alpha-factory, alpha-lab,
        quant-loop-testnet, and regime-intelligence.
        """
        clauses = []
        params: list[Any] = []
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker)
        if signal_type:
            clauses.append("signal_type = ?")
            params.append(signal_type)
        if min_confidence > 0:
            clauses.append("confidence >= ?")
            params.append(min_confidence)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._query(
            f"SELECT * FROM v_nexus_signal_feed {where} LIMIT ?", params
        )

    # ── Factors ───────────────────────────────────────────────────────

    def get_factors(
        self, ticker: str = "", factor_name: str = "",
    ) -> list[dict[str, Any]]:
        """Factor snapshot from v_nexus_factors (alpha-factory only)."""
        if ticker and factor_name:
            return self._query(
                "SELECT * FROM v_nexus_factors WHERE ticker = ? AND factor_name = ?",
                [ticker, factor_name],
            )
        elif ticker:
            return self._query(
                "SELECT * FROM v_nexus_factors WHERE ticker = ?", [ticker]
            )
        return self._query("SELECT * FROM v_nexus_factors LIMIT 1000")

    # ── Strategy Pool ────────────────────────────────────────────────

    def get_strategy_pool(
        self,
        regime_label: str = "",
        min_sharpe: float = 1.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Promoted/high-Sharpe strategies from v_nexus_strategy_pool."""
        if regime_label:
            return self._query(
                "SELECT * FROM v_nexus_strategy_pool WHERE backtest_sharpe >= ? "
                "ORDER BY backtest_sharpe DESC NULLS LAST LIMIT ?",
                [min_sharpe, limit],
            )
        return self._query(
            "SELECT * FROM v_nexus_strategy_pool WHERE backtest_sharpe >= ? "
            "ORDER BY backtest_sharpe DESC NULLS LAST LIMIT ?",
            [min_sharpe, limit],
        )

    # ── Catalyst ────────────────────────────────────────────────────

    def get_catalyst(self, ticker: str) -> dict[str, Any]:
        """Latest catalyst grade from v_nexus_catalyst_digest."""
        return self._query_one(
            "SELECT * FROM v_nexus_catalyst_digest WHERE ticker = ?", [ticker]
        ) or {}

    # ── Experience Bank ─────────────────────────────────────────────

    def get_experience(
        self,
        ticker: str = "",
        severity: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Lessons from v_nexus_experience (quant project entries only)."""
        clauses = []
        params: list[Any] = []
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._query(
            f"SELECT * FROM v_nexus_experience {where} LIMIT ?", params
        )

    # ── Failures ────────────────────────────────────────────────────

    def get_failures(
        self, strategy_name: str = "", limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Failure history from v_nexus_failures."""
        if strategy_name:
            return self._query(
                "SELECT * FROM v_nexus_failures WHERE strategy_name = ? LIMIT ?",
                [strategy_name, limit],
            )
        return self._query(
            "SELECT * FROM v_nexus_failures LIMIT ?", [limit]
        )

    # ── Regime-Strategy Map ────────────────────────────────────────

    def get_regime_strategy_map(
        self, regime_label: str = "",
    ) -> list[dict[str, Any]]:
        """Regime-strategy performance mapping from v_nexus_regime_strategy_map."""
        if regime_label:
            return self._query(
                "SELECT * FROM v_nexus_regime_strategy_map WHERE regime_label = ?",
                [regime_label],
            )
        return self._query("SELECT * FROM v_nexus_regime_strategy_map")

    # ── Full Intelligence Packet ───────────────────────────────────

    def get_ticker_intelligence(self, ticker: str) -> dict[str, Any]:
        """Aggregate ALL lakehouse data for *ticker* into one dict.

        Returns: regime, signals, factors, catalyst, experience,
        failures, regime_strategy_map, strategy_candidates.
        """
        packet: dict[str, Any] = {"ticker": ticker}

        try:
            packet["regime"] = self.get_regime(ticker)
        except Exception:
            packet["regime"] = None

        try:
            packet["signals"] = self.get_signals(ticker=ticker, limit=20)
        except Exception:
            packet["signals"] = []

        try:
            packet["factors"] = self.get_factors(ticker=ticker)
        except Exception:
            packet["factors"] = []

        try:
            packet["catalyst"] = self.get_catalyst(ticker)
        except Exception:
            packet["catalyst"] = None

        try:
            packet["experience"] = self.get_experience(ticker=ticker, limit=10)
        except Exception:
            packet["experience"] = []

        try:
            packet["failures"] = self.get_failures(limit=10)
        except Exception:
            packet["failures"] = []

        # Regime-strategy mapping based on current regime
        regime = packet.get("regime") or {}
        regime_label = (
            regime.get("composite_regime")
            or regime.get("regime_label")
            or ""
        )
        try:
            packet["regime_strategy_map"] = self.get_regime_strategy_map(
                regime_label=regime_label,
            )
        except Exception:
            packet["regime_strategy_map"] = []

        try:
            packet["strategy_candidates"] = self.get_strategy_pool(
                min_sharpe=1.0, limit=10,
            )
        except Exception:
            packet["strategy_candidates"] = []

        return packet

    # ── Write-back (requires QuantClient, not read-only) ───────────

    def write_trade_result(self, record: dict[str, Any]) -> bool:
        """Write a trade result back to the lakehouse.

        Requires QuantClient (write access). Returns False if unavailable.
        """
        try:
            from src.client import QuantClient
            c = QuantClient(db_path=self._db_path)
            c.write_nexus_trade_result(record)
            c.close()
            return True
        except Exception as exc:
            logger.warning("write_trade_result() failed: %s", exc)
            return False

    def write_lesson(self, record: dict[str, Any]) -> bool:
        """Write a lesson to the ecosystem experience bank.

        Requires QuantClient (write access). Returns False if unavailable.
        """
        try:
            from src.client import QuantClient
            c = QuantClient(db_path=self._db_path)
            c.write_nexus_lesson(record)
            c.close()
            return True
        except Exception as exc:
            logger.warning("write_lesson() failed: %s", exc)
            return False

    # ── Health check ────────────────────────────────────────────────

    def health_check(self) -> dict[str, Any]:
        """Check connectivity and curated view availability."""
        result: dict[str, Any] = {
            "db_path": self._db_path,
            "connected": False,
            "views": {},
        }
        try:
            import duckdb
            if not os.path.exists(self._db_path):
                result["error"] = "Database file not found"
                return result

            con = duckdb.connect(self._db_path, read_only=True)
            result["connected"] = True

            for vname in [
                "v_nexus_regime",
                "v_nexus_signal_feed",
                "v_nexus_factors",
                "v_nexus_strategy_pool",
                "v_nexus_catalyst_digest",
                "v_nexus_experience",
                "v_nexus_failures",
                "v_nexus_regime_strategy_map",
            ]:
                try:
                    cnt = con.execute(f"SELECT COUNT(*) FROM {vname}").fetchone()[0]
                    result["views"][vname] = cnt
                except Exception as e:
                    result["views"][vname] = f"error: {e}"

            con.close()
        except Exception as exc:
            result["error"] = str(exc)

        return result

    # ── Lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        if self._con is not None:
            try:
                self._con.close()
            except Exception:
                pass
            self._con = None

    def __enter__(self) -> "NexusLakehouseReader":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_reader: NexusLakehouseReader | None = None


def get_reader() -> NexusLakehouseReader:
    """Return the module-level singleton reader (creates on first call)."""
    global _reader
    if _reader is None:
        _reader = NexusLakehouseReader()
    return _reader
