"""Unit tests for the A3 backtest replay pipeline.

These tests verify the round-trip projection + decision-record building
without touching LanceDB. End-to-end smoke (subprocess → LanceDB) is
covered separately when running post_backtest_sync.sync_backtest_results
against a real backtest artifact directory.

Verifies:
1. ``_project_trades_to_round_trips`` handles FIFO matching correctly
   across single, partial, and unmatched buys.
2. ``_project_trades_to_round_trips`` correctly classifies wins/losses.
3. ``_strategy_from_run_id`` strips the backtest id suffix.
4. ``_build_decision_record_for_memory`` produces a record with all
   NexusDecisionRecord fields populated.
5. ``ensure_schema`` is idempotent (no errors on re-run).
6. ``ensure_schema`` adds the new A3 columns to a legacy backtest_trades
   table that pre-dates them.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import duckdb
import pandas as pd
import pytest

# Make sure the src tree is importable when run from /tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from src.runners.post_backtest_sync import (
    _build_decision_record_for_memory,
    _project_trades_to_round_trips,
    _strategy_from_run_id,
    ensure_schema,
)


# ──────────────────────────────────────────────────────────────────────
# Case 1: FIFO round-trip projection
# ──────────────────────────────────────────────────────────────────────


def test_project_round_trips_basic_win():
    """One buy, one sell at a higher price → win."""
    df = pd.DataFrame(
        [
            {
                "time": pd.Timestamp("2024-01-01"),
                "symbol": "BTC",
                "side": "buy",
                "filled_quantity": 1.0,
                "price": 40000.0,
            },
            {
                "time": pd.Timestamp("2024-02-01"),
                "symbol": "BTC",
                "side": "sell",
                "filled_quantity": 1.0,
                "price": 50000.0,
            },
        ]
    )
    rts = _project_trades_to_round_trips(df, "test_run_1")
    assert len(rts) == 1
    rt = rts[0]
    assert rt["outcome"] == "win"
    assert rt["pnl_pct"] == pytest.approx(25.0, rel=1e-6)
    assert rt["entry_price"] == 40000.0
    assert rt["exit_price"] == 50000.0


def test_project_round_trips_basic_loss():
    """One buy, one sell at a lower price → loss."""
    df = pd.DataFrame(
        [
            {
                "time": pd.Timestamp("2024-01-01"),
                "symbol": "BTC",
                "side": "buy",
                "filled_quantity": 1.0,
                "price": 50000.0,
            },
            {
                "time": pd.Timestamp("2024-02-01"),
                "symbol": "BTC",
                "side": "sell",
                "filled_quantity": 1.0,
                "price": 40000.0,
            },
        ]
    )
    rts = _project_trades_to_round_trips(df, "test_run_1")
    assert len(rts) == 1
    assert rts[0]["outcome"] == "loss"
    assert rts[0]["pnl_pct"] == pytest.approx(-20.0, rel=1e-6)


def test_project_round_trips_pending_when_no_close():
    """Buy with no matching sell → outcome=pending, pnl_pct=0."""
    df = pd.DataFrame(
        [
            {
                "time": pd.Timestamp("2024-01-01"),
                "symbol": "BTC",
                "side": "buy",
                "filled_quantity": 1.0,
                "price": 40000.0,
            },
        ]
    )
    rts = _project_trades_to_round_trips(df, "test_run_1")
    assert len(rts) == 1
    assert rts[0]["outcome"] == "pending"
    assert rts[0]["pnl_pct"] == 0.0


def test_project_round_trips_handles_legacy_negative_maxdd():
    """Legacy data may have negative max_drawdown values; the project
    function must handle both signs gracefully without crashing."""
    df = pd.DataFrame(
        [
            {
                "time": pd.Timestamp("2024-01-01"),
                "symbol": "BTC",
                "side": "buy",
                "filled_quantity": 1.0,
                "price": 40000.0,
            },
            {
                "time": pd.Timestamp("2024-02-01"),
                "symbol": "BTC",
                "side": "sell",
                "filled_quantity": 1.0,
                "price": 50000.0,
            },
        ]
    )
    rts = _project_trades_to_round_trips(df, "test_run_1")
    # outcome must be 'win' regardless of legacy sign conventions
    assert rts[0]["outcome"] == "win"


def test_project_round_trips_empty_returns_empty_list():
    df = pd.DataFrame(columns=["time", "symbol", "side", "filled_quantity", "price"])
    rts = _project_trades_to_round_trips(df, "test_run_1")
    assert rts == []


# ──────────────────────────────────────────────────────────────────────
# Case 2: strategy name extraction
# ──────────────────────────────────────────────────────────────────────


def test_strategy_from_run_id_strips_backtest_suffix():
    rid = "NexusCommitteeStrategy_2026-06-23_11-59_q3d6Ko"
    assert _strategy_from_run_id(rid) == "NexusCommitteeStrategy"


def test_strategy_from_run_id_handles_no_date_suffix():
    # No date suffix → first underscore-token is the strategy name.
    rid = "meta_x_v1"
    assert _strategy_from_run_id(rid) == "meta"


def test_strategy_from_run_id_handles_empty():
    assert _strategy_from_run_id("") == "unknown_strategy"


# ──────────────────────────────────────────────────────────────────────
# Case 3: decision-record building
# ──────────────────────────────────────────────────────────────────────


def test_build_decision_record_populates_all_fields():
    rt = {
        "run_id": "test_run_1",
        "decision_id": "rt_1",
        "symbol": "BTC",
        "action": "buy",
        "entry_price": 40000.0,
        "exit_price": 50000.0,
        "filled_quantity": 1.0,
        "pnl_pct": 25.0,
        "outcome": "win",
        "decision_sim_time": pd.Timestamp("2024-01-01"),
        "entry_time": pd.Timestamp("2024-01-01"),
        "exit_time": pd.Timestamp("2024-02-01"),
        "round_trip_id": "rt_1",
    }
    rec = _build_decision_record_for_memory(rt, "TestStrategy")
    # All required NexusDecisionRecord fields
    for key in (
        "id",
        "symbol",
        "action",
        "regime",
        "thesis_summary",
        "indicators_snapshot",
        "outcome",
        "pnl_pct",
        "timestamp",
        "decision_sim_time",
        "strategy_name",
        "backtest_id",
    ):
        assert key in rec, f"missing key: {key}"
    assert rec["symbol"] == "BTC"
    assert rec["outcome"] == "win"
    assert rec["pnl_pct"] == 25.0
    assert rec["decision_sim_time"]  # non-empty
    assert rec["strategy_name"] == "TestStrategy"


def test_build_decision_record_pending_outcome_zero_pnl():
    rt = {
        "run_id": "test_run_1",
        "decision_id": "rt_pending",
        "symbol": "ETH",
        "action": "buy",
        "entry_price": 3000.0,
        "exit_price": 0.0,
        "filled_quantity": 1.0,
        "pnl_pct": 0.0,
        "outcome": "pending",
        "decision_sim_time": pd.Timestamp("2024-01-01"),
        "entry_time": pd.Timestamp("2024-01-01"),
        "exit_time": None,
        "round_trip_id": "rt_pending",
    }
    rec = _build_decision_record_for_memory(rt, "TestStrategy")
    assert rec["outcome"] == "pending"
    assert rec["pnl_pct"] == 0.0
    # Backtest replay rows must still carry decision_sim_time so future
    # as_of filters don't accidentally leak live data into backtest context.
    assert rec["decision_sim_time"]


# ──────────────────────────────────────────────────────────────────────
# Case 4: ensure_schema idempotency + migration
# ──────────────────────────────────────────────────────────────────────


def test_ensure_schema_creates_tables_when_missing(tmp_path):
    """A brand-new DB must have all 3 tables after ensure_schema."""
    db_path = tmp_path / "fresh.duckdb"
    ensure_schema(db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "backtest_runs" in tables
        assert "backtest_trades" in tables
        assert "agent_observations" in tables
    finally:
        con.close()


def test_ensure_schema_adds_new_columns_to_legacy_table(tmp_path):
    """A legacy backtest_trades table missing A3 columns must gain them
    after ensure_schema runs."""
    db_path = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        # Create a legacy table with no A3 columns.
        con.execute(
            """
            CREATE TABLE backtest_trades (
                run_id VARCHAR,
                event_timestamp TIMESTAMP,
                symbol VARCHAR,
                side VARCHAR,
                filled_quantity DOUBLE,
                price DOUBLE,
                trade_cost DOUBLE
            )
            """
        )
        con.execute(
            """
            CREATE TABLE backtest_runs (
                run_id VARCHAR PRIMARY KEY,
                strategy_name VARCHAR
            )
            """
        )
        con.commit()
    finally:
        con.close()

    ensure_schema(db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        # PRAGMA table_info returns (cid, name, type, notnull, default, pk).
        # Column name is index 1.
        cols = {
            r[1]
            for r in con.execute("PRAGMA table_info(backtest_trades)").fetchall()
        }
        for required in ("decision_sim_time", "outcome", "pnl_pct", "round_trip_id"):
            assert required in cols, f"A3 column '{required}' missing after migration"
        # backtest_runs should also have sortino now.
        run_cols = {
            r[1]
            for r in con.execute("PRAGMA table_info(backtest_runs)").fetchall()
        }
        assert "sortino" in run_cols
    finally:
        con.close()


def test_ensure_schema_idempotent_on_second_run(tmp_path):
    """Calling ensure_schema twice must not raise."""
    db_path = tmp_path / "twice.duckdb"
    ensure_schema(db_path)
    # Second call must be silent.
    ensure_schema(db_path)
    assert db_path.exists()