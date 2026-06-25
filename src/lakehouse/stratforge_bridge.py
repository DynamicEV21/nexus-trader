"""
StratForge Database Bridge
==========================

Reads strategies from the StratForge DuckDB lakehouse and writes LumiBot
backtest results to a *separate* Nexus results database.

Connection model (avoids DuckDB lock contention with the herm-bot profile
which holds an open connection to the StratForge DB):

- **Reads** (winners, source code, metadata) open *read-only* connections to
  the StratForge DB. DuckDB allows any number of concurrent read-only readers,
  so these never conflict with another process's read-write handle.
- **Writes** go to a dedicated file, ``nexus_results.duckdb``, which is owned
  exclusively by Nexus. No other process writes there, so there is no lock
  contention.

Usage:
    from nexus_trade.lakehouse.stratforge_bridge import StratForgeBridge

    bridge = StratForgeBridge()

    # Read (from StratForge DB, read-only)
    winners = bridge.get_winners(min_composite=60, limit=20)
    source_code = bridge.get_strategy_source("accel_band_ppo_multi")

    # Write (to nexus_results.duckdb)
    bridge.record_backtest_result(
        strategy_name="accel_band_ppo_multi",
        symbol="BTC",
        total_return_pct=565.67,
        sharpe=2.386,
        ...
    )
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────

# StratForge lakehouse — READ ONLY. Another process (herm-bot) holds a
# read-write handle here, so we must never open this file read-write.
SF_DB_PATH = os.path.expanduser(
    "~/.hermes/profiles/herm-bot/home/agentic-quant-os/data/quant.duckdb"
)

# Separate DB for Nexus results — avoids lock conflicts with StratForge's process
NEXUS_DB_PATH = "/home/Zev/development/nexus-trade/data/nexus_results.duckdb"

# Nexus-owned results DB — we have exclusive write access. Keeping results in a
# separate file eliminates DuckDB's "different configuration" / lock errors
# that occur when mixing read-only and read-write handles to the same file
# while another process also has it open.
NEXUS_DB_PATH = "/home/Zev/development/nexus-trade/data/nexus_results.duckdb"

# ─── Schema ────────────────────────────────────────────────────────────

CREATE_NEXUS_RESULTS = """
CREATE TABLE IF NOT EXISTS nexus_lumibot_results (
    id INTEGER DEFAULT nextval('nexus_seq'),
    strategy_name VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    backtest_start DATE,
    backtest_end DATE,
    budget DOUBLE,
    total_return_pct DOUBLE,
    max_drawdown_pct DOUBLE,
    sharpe DOUBLE,
    cagr DOUBLE,
    volatility DOUBLE,
    romad DOUBLE,
    num_entries INTEGER,
    num_exits INTEGER,
    source_composite DOUBLE,
    source_wf_pass BOOLEAN,
    source_wf_avg_sharpe DOUBLE,
    lumibot_version VARCHAR DEFAULT '4.5.25',
    backtest_engine VARCHAR DEFAULT 'PandasDataBacktesting',
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (id)
);
"""

CREATE_SEQ = """
CREATE SEQUENCE IF NOT EXISTS nexus_seq START 1;
"""


class StratForgeBridge:
    """Bridge between the StratForge DuckDB lakehouse (read) and the Nexus
    results DB (write).

    Read operations use *read-only* connections to the StratForge DB, which are
    safe to open concurrently with another process's read-write handle. Write
    operations target a separate, Nexus-owned database file so they never
    contend with the StratForge writer.
    """

    def __init__(
        self,
        sf_db_path: str = SF_DB_PATH,
        nexus_db_path: str = NEXUS_DB_PATH,
    ):
        self.sf_db_path = sf_db_path
        self.nexus_db_path = nexus_db_path

        # Persistent read-only connection to the StratForge lakehouse.
        # Multiple read-only connections are allowed alongside another
        # process's read-write connection.
        self._read_con: duckdb.DuckDBPyConnection = self._open_read_connection(sf_db_path)

        # Persistent read-write connection to the Nexus results DB.
        self._write_con: duckdb.DuckDBPyConnection = self._open_write_connection(nexus_db_path)

        logger.debug(
            f"StratForgeBridge: read={sf_db_path} (read_only), "
            f"write={nexus_db_path}"
        )

    # ── Connection helpers ──────────────────────────────────────────────

    @staticmethod
    def _open_read_connection(path: str) -> duckdb.DuckDBPyConnection:
        """Open a read-only connection to the StratForge DB.

        Read-only connections never conflict with another process's
        read-write handle. A fresh connection is opened on each call site that
        needs one if the persistent one died; here we open the persistent one
        once.
        """
        try:
            return duckdb.connect(path, read_only=True)
        except Exception as e:
            logger.error(f"Cannot open StratForge DB read-only ({path}): {e}")
            raise

    @staticmethod
    def _open_write_connection(path: str) -> duckdb.DuckDBPyConnection:
        """Open a read-write connection to the Nexus results DB (exclusive)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = duckdb.connect(path)
        con.execute(CREATE_SEQ)
        con.execute(CREATE_NEXUS_RESULTS)
        return con

    def _read_query(self, query: str, params: list = None) -> pd.DataFrame:
        """Execute a read query against the StratForge DB (read-only)."""
        try:
            if params:
                return self._read_con.execute(query, params).fetchdf()
            return self._read_con.execute(query).fetchdf()
        except Exception as e:
            # Read-only connections can be invalidated if the underlying file
            # is replaced. Reopen once and retry.
            logger.warning(f"Read query failed ({e}); reopening read connection.")
            self._read_con = self._open_read_connection(self.sf_db_path)
            if params:
                return self._read_con.execute(query, params).fetchdf()
            return self._read_con.execute(query).fetchdf()

    # ── Read Operations (StratForge DB) ─────────────────────────────────

    def get_winners(
        self,
        min_composite: float = 0,
        wf_pass_only: bool = False,
        limit: int = 50,
    ) -> pd.DataFrame:
        """Get winning strategies from StratForge.

        Args:
            min_composite: Minimum composite_score filter.
            wf_pass_only: Only return strategies that passed walk-forward.
            limit: Maximum number of results.

        Returns:
            DataFrame with strategy_name, composite_score, status, wf_pass,
            wf_avg_sharpe, and source_code availability.
        """
        query = """
            SELECT DISTINCT ON (strategy_name)
                strategy_name,
                composite_score,
                status,
                wf_pass,
                wf_avg_sharpe,
                source_repo,
                LENGTH(source_code) as code_length,
                params_json
            FROM backtest_results_v2
            WHERE status = 'winner'
              AND composite_score >= ?
              AND source_code IS NOT NULL
              AND LENGTH(source_code) > 100
        """
        params = [min_composite]

        if wf_pass_only:
            query += " AND wf_pass = TRUE"

        query += " ORDER BY strategy_name, is_best_version DESC NULLS LAST LIMIT ?"
        params.append(limit)

        return self._read_query(query, params)

    def get_strategy_source(self, strategy_name: str) -> Optional[str]:
        """Get the source code for a strategy from the DB."""
        query = """
            SELECT source_code
            FROM backtest_results_v2
            WHERE strategy_name = ?
              AND source_code IS NOT NULL
              AND LENGTH(source_code) > 100
            ORDER BY is_best_version DESC NULLS LAST
            LIMIT 1
        """
        df = self._read_query(query, [strategy_name])
        if df.empty:
            return None
        return df.iloc[0]["source_code"]

    def get_strategy_params(self, strategy_name: str) -> dict:
        """Get the params_json for a strategy."""
        query = """
            SELECT params_json
            FROM backtest_results_v2
            WHERE strategy_name = ?
            ORDER BY is_best_version DESC NULLS LAST
            LIMIT 1
        """
        df = self._read_query(query, [strategy_name])
        if df.empty or df.iloc[0]["params_json"] is None:
            return {}
        try:
            return json.loads(df.iloc[0]["params_json"])
        except (json.JSONDecodeError, TypeError):
            return {}

    def get_strategy_metadata(self, strategy_name: str) -> Optional[dict]:
        """Get full metadata for a strategy including WF scores."""
        query = """
            SELECT *
            FROM backtest_results_v2
            WHERE strategy_name = ?
            ORDER BY is_best_version DESC NULLS LAST
            LIMIT 1
        """
        df = self._read_query(query, [strategy_name])
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        # Convert numpy types to Python native
        return {k: v.item() if hasattr(v, 'item') else v for k, v in row.items()}

    def get_all_winner_names(self) -> list[str]:
        """Get a list of all winner strategy names."""
        df = self._read_query(
            "SELECT DISTINCT strategy_name FROM backtest_results_v2 WHERE status = 'winner' ORDER BY strategy_name"
        )
        return df["strategy_name"].tolist() if not df.empty else []

    # ── Write Operations (Nexus DB) ─────────────────────────────────────

    def record_backtest_result(
        self,
        strategy_name: str,
        symbol: str,
        total_return_pct: float,
        max_drawdown_pct: float = None,
        sharpe: float = None,
        cagr: float = None,
        volatility: float = None,
        romad: float = None,
        num_entries: int = None,
        num_exits: int = None,
        backtest_start: str = None,
        backtest_end: str = None,
        budget: float = 10000,
        source_composite: float = None,
        source_wf_pass: bool = None,
        source_wf_avg_sharpe: float = None,
        notes: str = None,
    ) -> int:
        """Record a LumiBot backtest result in the nexus_lumibot_results table
        (Nexus DB).

        Returns the inserted row ID, or -1 on failure.
        """
        con = self._write_con
        try:
            # Look up source metadata from the StratForge DB if not provided.
            if source_composite is None or source_wf_pass is None:
                try:
                    meta = self.get_strategy_metadata(strategy_name)
                    if meta:
                        if source_composite is None:
                            source_composite = meta.get("composite_score")
                        if source_wf_pass is None:
                            source_wf_pass = meta.get("wf_pass")
                        if source_wf_avg_sharpe is None:
                            source_wf_avg_sharpe = meta.get("wf_avg_sharpe")
                except Exception as meta_err:
                    logger.debug(f"Source metadata lookup skipped: {meta_err}")

            result = con.execute(
                """
                INSERT INTO nexus_lumibot_results (
                    strategy_name, symbol, backtest_start, backtest_end, budget,
                    total_return_pct, max_drawdown_pct, sharpe, cagr, volatility, romad,
                    num_entries, num_exits,
                    source_composite, source_wf_pass, source_wf_avg_sharpe,
                    notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                [
                    strategy_name, symbol, backtest_start, backtest_end, budget,
                    total_return_pct, max_drawdown_pct, sharpe, cagr, volatility, romad,
                    num_entries, num_exits,
                    source_composite, source_wf_pass, source_wf_avg_sharpe,
                    notes,
                ],
            ).fetchone()
            row_id = result[0] if result else None
            logger.info(
                f"Recorded: {strategy_name}/{symbol} → "
                f"return={total_return_pct}%, sharpe={sharpe} (id={row_id})"
            )
            return row_id if row_id is not None else -1
        except Exception as e:
            logger.error(f"Failed to record result: {e}")
            return -1

    # ── Read Operations (Nexus DB) ──────────────────────────────────────

    def _nexus_query(self, query: str, params: list = None) -> pd.DataFrame:
        """Execute a query against the Nexus results DB (read-write conn)."""
        if params:
            return self._write_con.execute(query, params).fetchdf()
        return self._write_con.execute(query).fetchdf()

    def get_nexus_results(self, strategy_name: str = None) -> pd.DataFrame:
        """Get all Nexus backtest results, optionally filtered by strategy."""
        if strategy_name:
            return self._nexus_query(
                "SELECT * FROM nexus_lumibot_results WHERE strategy_name = ? ORDER BY created_at DESC",
                [strategy_name],
            )
        return self._nexus_query(
            "SELECT * FROM nexus_lumibot_results ORDER BY created_at DESC"
        )

    def get_best_nexus_results(self, min_sharpe: float = 0, limit: int = 20) -> pd.DataFrame:
        """Get the best-performing strategies in Nexus backtests."""
        return self._nexus_query(
            """
            SELECT * FROM nexus_lumibot_results
            WHERE sharpe >= ?
            ORDER BY sharpe DESC
            LIMIT ?
            """,
            [min_sharpe, limit],
        )

    # ── Walk-Forward Results (Nexus DB) ─────────────────────────────────

    def record_walk_forward_result(self, wf_row: dict) -> int:
        """Record a walk-forward validation summary row.

        ``wf_row`` should contain keys matching the ``walk_forward_results``
        schema. Creates the table if it does not exist. Includes both
        ``sharpe`` and ``sortino`` (Sortino is the primary ranking metric
        for crypto OOS evaluation; Sharpe is kept as a tiebreaker).
        """
        con = self._write_con
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS walk_forward_results (
                id INTEGER DEFAULT nextval('nexus_seq'),
                strategy_name VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                window_index INTEGER,
                train_start DATE,
                train_end DATE,
                test_start DATE,
                test_end DATE,
                total_return_pct DOUBLE,
                sharpe DOUBLE,
                sortino DOUBLE,
                max_drawdown_pct DOUBLE,
                profitable BOOLEAN,
                num_entries INTEGER,
                budget DOUBLE,
                created_at TIMESTAMP DEFAULT current_timestamp,
                PRIMARY KEY (id)
            );
            """
        )
        # Defensive: if the table was created earlier without a sortino column,
        # add it (idempotent ALTER).
        try:
            cols = {row[1] for row in con.execute(
                "PRAGMA table_info(walk_forward_results)"
            ).fetchall()}
            if "sortino" not in cols:
                con.execute(
                    "ALTER TABLE walk_forward_results ADD COLUMN sortino DOUBLE"
                )
        except Exception as exc:
            logger.debug("sortino column add skipped: %s", exc)

        try:
            result = con.execute(
                """
                INSERT INTO walk_forward_results (
                    strategy_name, symbol, window_index,
                    train_start, train_end, test_start, test_end,
                    total_return_pct, sharpe, sortino, max_drawdown_pct, profitable,
                    num_entries, budget
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                [
                    wf_row["strategy_name"],
                    wf_row["symbol"],
                    wf_row.get("window_index"),
                    wf_row.get("train_start"),
                    wf_row.get("train_end"),
                    wf_row.get("test_start"),
                    wf_row.get("test_end"),
                    wf_row.get("total_return_pct"),
                    wf_row.get("sharpe"),
                    wf_row.get("sortino"),  # NEW: Sortino alongside Sharpe
                    wf_row.get("max_drawdown_pct"),
                    wf_row.get("profitable"),
                    wf_row.get("num_entries"),
                    wf_row.get("budget", 10000),
                ],
            ).fetchone()
            return result[0] if result else -1
        except Exception as e:
            logger.error(f"Failed to record WF result: {e}")
            return -1

    def close(self) -> None:
        """Close both connections."""
        try:
            self._read_con.close()
        except Exception:
            pass
        try:
            self._write_con.close()
        except Exception:
            pass
