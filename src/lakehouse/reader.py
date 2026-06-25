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

# ---------------------------------------------------------------------------
# DuckDB → DuckLake redirect (safety net)
#
# If the agentic-quant-os ducklake_redirect module is available, import it
# so that any *accidental* `duckdb.connect(quant.duckdb_path)` calls anywhere
# in this process are transparently redirected to DuckLake. This is a
# safety net for the dual-write pipeline: if quant.duckdb ever drifts from
# DuckLake, a direct file read would return stale data, but a redirected
# read would always get fresh DuckLake data.
#
# This is best-effort. If the import fails (DuckLake not reachable,
# extensions not installed, etc.), we log a warning and continue with
# normal file-based duckdb behavior.
# ---------------------------------------------------------------------------
try:
    import ducklake_redirect  # noqa: F401  -- side-effect: monkeypatches duckdb.connect
    logger.info("ducklake_redirect loaded — quant.duckdb reads will route to DuckLake")
except Exception as exc:
    logger.warning(
        "ducklake_redirect unavailable (%s) — quant.duckdb reads will use file directly", exc
    )

_DEFAULT_DB_PATH = os.path.expanduser(
    os.environ.get("NEXUS_LAKEHOUSE_PATH", "~/development/agentic-quant-os/data/quant.duckdb")
)

# Registry of active reader instances — used by aqs_writer to close them
# before opening a write connection to the same file (DuckDB single-
# connection-per-file rule).
_READERS: list["NexusLakehouseReader"] = []


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
        if self not in _READERS:
            _READERS.append(self)

    # ── Lazy connection (read-only, no bootstrap) ─────────────────────

    def _con_get(self) -> Any:
        """Lazily open a read-only DuckDB connection.

        Uses ``ATTACH`` against an in-memory connection rather than
        ``duckdb.connect(path)`` to bypass a DuckDB v1.5.x catalog-cache
        bug where direct-connect reads can return stale view/table
        definitions after the file has been rewritten. ``ATTACH`` always
        re-reads the catalog from disk.

        Verifies the connection is still alive before returning it.
        """
        if self._con is not None:
            try:
                # Ping the connection — if it's closed/corrupted, this raises
                self._con.execute("SELECT 1")
                return self._con
            except Exception:
                try:
                    self._con.close()
                except Exception:
                    pass
                self._con = None
        try:
            import duckdb
            # Use ATTACH into a fresh in-memory connection. This forces a
            # fresh catalog load and avoids the per-file stale-catalog issue.
            self._con = duckdb.connect(":memory:")
            # Escape single quotes in path for SQL
            esc_path = self._db_path.replace("'", "''")
            self._con.execute(
                f"ATTACH '{esc_path}' AS nexus_lake (READ_ONLY)"
            )
            self._con.execute("USE nexus_lake")
            logger.info("Lakehouse reader attached to %s via ATTACH", self._db_path)
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

        When ``NEXUS_REGIME_CHAMPION_ONLY=1``, queries ``v_nexus_regime_champion``
        instead — preferring the ``closed_loop`` detector for BTC-USDT, falling
        back to ``composite``/``ensemble`` for all other tickers.  This removes
        ``ml_classifier`` (TESTBTC debug noise) from the result stream.

        The curated view filters to composite/ensemble detector outputs only.
        """
        if os.environ.get("NEXUS_REGIME_CHAMPION_ONLY", "0") == "1":
            return self._query_one(
                "SELECT * FROM v_nexus_regime_champion WHERE ticker = ?", [ticker]
            ) or {}
        return self._query_one(
            "SELECT * FROM v_nexus_regime WHERE ticker = ?", [ticker]
        ) or {}

    def get_all_regimes(self) -> list[dict[str, Any]]:
        """Get latest composite regime for every tracked ticker.

        When ``NEXUS_REGIME_CHAMPION_ONLY=1``, returns ``v_nexus_regime_champion``
        instead — preferring ``closed_loop`` for BTC-USDT and ``composite`` for
        all other tickers.
        """
        if os.environ.get("NEXUS_REGIME_CHAMPION_ONLY", "0") == "1":
            return self._query("SELECT * FROM v_nexus_regime_champion")
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
        min_composite: float = 49.0,
        min_sharpe: float = 0.0,
        min_sortino: float = 0.0,
        ticker: str = "",
        sort_by: str = "sortino",
        limit: int = 20,
        as_of: str = "",
    ) -> list[dict[str, Any]]:
        """Validated crypto strategies from v_nexus_strategy_pool.

        The view reads from ``backtest_results_v2`` and is pre-filtered to
        crypto-relevant tickers (BTC, ETH, SOL, ALL, MULTI) with
        ``composite_score >= 49`` and ``status IN ('winner', 'tested')``.

        Default ranking is by Sortino (penalizes only downside volatility —
        better metric than Sharpe for asymmetric crypto payoffs where upside
        vol is welcomed). Falls back to Sharpe when Sortino is NULL,
        then to composite_score.

        Args:
            regime_label: Filter by regime_best_tag or regime_label.
            min_composite: Minimum composite score (default 49.0, the WF gate).
            min_sharpe: Minimum in-sample Sharpe ratio.
            min_sortino: Minimum in-sample Sortino ratio (NEW, default 0 = no
                filter; e.g. 0.5 = only return strategies with Sortino > 0.5).
            ticker: Filter by ticker (e.g. 'BTC').
            sort_by: Ranking key — ``"sortino"`` (default), ``"sharpe"``, or
                ``"composite"``.
            limit: Max results.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if min_composite > 0:
            clauses.append("composite_score >= ?")
            params.append(min_composite)
        if min_sharpe > 0:
            clauses.append("sharpe >= ?")
            params.append(min_sharpe)
        if min_sortino > 0:
            clauses.append("sortino IS NOT NULL AND sortino >= ?")
            params.append(min_sortino)
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker)
        if regime_label:
            clauses.append("(regime_best_tag = ? OR regime_label = ?)")
            params.extend([regime_label, regime_label])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        # Map sort_by to a SQL ORDER BY clause. Sortino DESC NULLS LAST is
        # the canonical ranking for crypto strategies: penalizes only downside
        # volatility, which is the risk that matters to a long-biased committee.
        order_by_map: dict[str, str] = {
            "sortino": "sortino DESC NULLS LAST, sharpe DESC NULLS LAST, composite_score DESC NULLS LAST",
            "sharpe": "sharpe DESC NULLS LAST, sortino DESC NULLS LAST, composite_score DESC NULLS LAST",
            "composite": "composite_score DESC NULLS LAST, sortino DESC NULLS LAST",
        }
        order_by = order_by_map.get(sort_by, order_by_map["sortino"])
        params.append(limit)

        # B2 anti-leakage (2026-06-25): when ``as_of`` is set, route the
        # query through the ``v_nexus_strategy_pool_asof`` macro so we
        # only see rows whose as_of_timestamp <= the bound. The macro's
        # own WHERE applies the cutoff; we then layer the filter params
        # on top. Falls back to the plain view if the macro isn't
        # installed (e.g., the migration hasn't run yet).
        if as_of:
            try:
                # The macro already has its own WHERE; we need to add
                # the user's filters as a NEW WHERE on the outer query.
                asof_clause = where.replace("WHERE ", "", 1) if where else ""
                outer_where = f"WHERE {asof_clause}" if asof_clause else ""
                return self._query(
                    f"SELECT * FROM v_nexus_strategy_pool_asof(?) {outer_where} "
                    f"ORDER BY {order_by} LIMIT ?",
                    [as_of] + params,
                )
            except Exception as exc:
                logger.debug("v_nexus_strategy_pool_asof failed (%s); falling back", exc)
        return self._query(
            f"SELECT * FROM v_nexus_strategy_pool {where} "
            f"ORDER BY {order_by} LIMIT ?", params
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
        """Regime-strategy performance mapping from v_nexus_regime_strategy_map.

        The view is sorted by Sortino (penalizes only downside volatility)
        over Sharpe — see view_migration.NEXUS_CURATED_VIEW_DDL.
        """
        if regime_label:
            return self._query(
                "SELECT * FROM v_nexus_regime_strategy_map WHERE regime_label = ?",
                [regime_label],
            )
        return self._query("SELECT * FROM v_nexus_regime_strategy_map")

    def get_top_regime_strategies_by_sortino(
        self, regime_label: str = "", min_sortino: float = 0.0, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Top strategies for a given regime, ranked by Sortino.

        Sortino is the primary ranking metric for crypto (penalizes only
        downside volatility; we welcome upside vol). This is a thin wrapper
        over ``v_nexus_regime_strategy_map`` that re-orders in SQL (the view
        itself is also Sortino-ordered, but we re-rank defensively in case
        upstream changes).

        Args:
            regime_label: Filter by regime (empty = all regimes).
            min_sortino: Minimum Sortino ratio filter (0 = no filter).
            limit: Max results.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if regime_label:
            clauses.append("regime_label = ?")
            params.append(regime_label)
        if min_sortino > 0:
            # The local quant.duckdb v_nexus_regime_strategy_map uses
            # ``sortino_ratio`` / ``sharpe_ratio``; DuckLake upstream uses
            # ``avg_sortino`` / ``avg_sharpe``. We probe the columns first
            # so the SQL doesn't reference non-existent columns (DuckDB
            # does column-resolution at parse time, not execution).
            try:
                cols = self._con_get().execute(
                    "DESCRIBE v_nexus_regime_strategy_map"
                ).fetchall()
                col_names = {c[0].lower() for c in cols}
                if "sortino_ratio" in col_names:
                    sortino_col = "sortino_ratio"
                elif "avg_sortino" in col_names:
                    sortino_col = "avg_sortino"
                elif "sortino" in col_names:
                    sortino_col = "sortino"
                else:
                    sortino_col = None
            except Exception:
                sortino_col = None
            if sortino_col:
                clauses.append(f"NULLIF({sortino_col}, 0) >= ?")
                params.append(min_sortino)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        # Same probe for the ORDER BY column.
        try:
            cols = self._con_get().execute(
                "DESCRIBE v_nexus_regime_strategy_map"
            ).fetchall()
            col_names = {c[0].lower() for c in cols}
            if "sortino_ratio" in col_names:
                sortino_col = "sortino_ratio"
            elif "avg_sortino" in col_names:
                sortino_col = "avg_sortino"
            elif "sortino" in col_names:
                sortino_col = "sortino"
            else:
                sortino_col = None
        except Exception:
            sortino_col = None
        if sortino_col:
            order_by = f"NULLIF({sortino_col}, 0) DESC NULLS LAST"
        else:
            order_by = "1"  # fallback: don't order
        return self._query(
            f"SELECT * FROM v_nexus_regime_strategy_map {where} "
            f"ORDER BY {order_by} LIMIT ?",
            params,
        )

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

    def _quant_client(self):
        """Load QuantClient via importlib to dodge the ``src`` namespace
        collision between nexus-trade/src and agentic-quant-os/src.

        Creates a synthetic package ``aqos_src`` so relative imports inside
        ``client.py`` (``from .schema import ...``) resolve correctly.

        Returns the QuantClient class or None if unavailable.
        """
        try:
            import importlib.util
            import sys
            aqos_src = os.path.expanduser(
                os.environ.get("AQOS_SRC", "~/development/agentic-quant-os/src")
            )
            if not os.path.isdir(aqos_src):
                logger.warning("AQS src directory not found: %s", aqos_src)
                return None
            pkg_name = "aqos_src_reader"
            if pkg_name not in sys.modules:
                pkg = type(sys)(pkg_name)
                pkg.__path__ = [aqos_src]
                pkg.__package__ = pkg_name
                sys.modules[pkg_name] = pkg
                for mod_name in ("schema", "db_connection"):
                    mod_path = os.path.join(aqos_src, f"{mod_name}.py")
                    if os.path.exists(mod_path):
                        full_name = f"{pkg_name}.{mod_name}"
                        spec = importlib.util.spec_from_file_location(
                            full_name, mod_path,
                            submodule_search_locations=[aqos_src],
                        )
                        if spec and spec.loader:
                            mod = importlib.util.module_from_spec(spec)
                            sys.modules[full_name] = mod
                            mod.__package__ = pkg_name
                            spec.loader.exec_module(mod)
            client_path = os.path.join(aqos_src, "client.py")
            full_name = f"{pkg_name}.client"
            spec = importlib.util.spec_from_file_location(
                full_name, client_path,
                submodule_search_locations=[aqos_src],
            )
            if not spec or not spec.loader:
                return None
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = pkg_name
            sys.modules[full_name] = mod
            spec.loader.exec_module(mod)
            return mod.QuantClient
        except Exception as exc:
            logger.warning("_quant_client() failed: %s", exc)
            return None

    def write_trade_result(self, record: dict[str, Any]) -> bool:
        """Write a trade result back to the lakehouse.

        Requires QuantClient (write access). Returns False if unavailable.
        """
        try:
            QC = self._quant_client()
            if QC is None:
                logger.warning("write_trade_result(): QuantClient unavailable")
                return False
            c = QC(db_path=self._db_path)
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
            QC = self._quant_client()
            if QC is None:
                logger.warning("write_lesson(): QuantClient unavailable")
                return False
            c = QC(db_path=self._db_path)
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
        con = self._con_get()
        if con is None:
            if not os.path.exists(self._db_path):
                result["error"] = "Database file not found"
            else:
                result["error"] = "Connection failed"
            return result
        result["connected"] = True
        for vname in [
            "v_nexus_regime",
            "v_nexus_regime_champion",
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
