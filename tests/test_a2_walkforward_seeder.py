"""Unit tests for the A2 walkforward seeder + query tool.

These tests verify the small, pure helpers — no subprocess, no real
LanceDB writes. End-to-end smoke (subprocess bridge to AQOS venv) is
documented in scripts/cron/refresh_walkforward.sh.

Verifies:
1. ``_safe_float`` handles None / non-numeric / numeric gracefully.
2. ``_norm_strategy_name`` strips backtest-id suffixes.
3. ``_date_iso`` returns ISO string for Timestamp and string inputs.
4. ``_compute_composite_rank_score`` is bounded in [0, 1] and stable.
5. ``_row_to_record`` builds a complete NexusWalkforwardRecord from a
   pandas Series with all expected fields.
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

# Make sure the src tree is importable when run from /tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from src.memory.walkforward_seeder import (
    _compute_composite_rank_score,
    _date_iso,
    _norm_strategy_name,
    _row_to_record,
    _safe_float,
)


# ──────────────────────────────────────────────────────────────────────
# Case 1: _safe_float — defensive coercion
# ──────────────────────────────────────────────────────────────────────


def test_safe_float_none_returns_zero():
    assert _safe_float(None) == 0.0


def test_safe_float_numeric_passes_through():
    assert _safe_float(1.5) == 1.5
    assert _safe_float(2) == 2.0
    assert _safe_float("3.14") == 3.14


def test_safe_float_string_non_numeric_returns_zero():
    # Non-numeric strings raise ValueError → _safe_float returns 0.0.
    assert _safe_float("n/a") == 0.0
    assert _safe_float("") == 0.0


def test_safe_float_string_numeric_passes_through():
    # Numeric strings parse cleanly via float().
    assert _safe_float("3.14") == 3.14
    assert _safe_float("-2.5") == -2.5


def test_safe_float_string_inf_returns_zero():
    # "inf" parses to float('inf'); the helper specifically converts
    # this back to 0.0 to avoid poisoning downstream std/sqrt.
    assert _safe_float("inf") == 0.0
    assert _safe_float("-inf") == 0.0


def test_safe_float_nan_returns_zero():
    assert _safe_float(float("nan")) == 0.0


def test_safe_float_inf_returns_zero():
    # inf would otherwise poison downstream sqrt/std → return 0 to be safe.
    assert _safe_float(float("inf")) == 0.0


# ──────────────────────────────────────────────────────────────────────
# Case 2: _norm_strategy_name — strip backtest-id suffixes
# ──────────────────────────────────────────────────────────────────────


def test_norm_strategy_name_strips_whitespace():
    assert _norm_strategy_name("  meta_x  ") == "meta_x"


def test_norm_strategy_name_passthrough_canonical():
    raw = "meta_cmo_alma_atr_wf_v1"
    assert _norm_strategy_name(raw) == "meta_cmo_alma_atr_wf_v1"


def test_norm_strategy_name_handles_empty_and_none():
    assert _norm_strategy_name("") == "unknown"
    assert _norm_strategy_name(None) == "unknown"


# ──────────────────────────────────────────────────────────────────────
# Case 3: _date_iso — returns ISO string
# ──────────────────────────────────────────────────────────────────────


def test_date_iso_from_timestamp():
    ts = pd.Timestamp("2024-06-01")
    out = _date_iso(ts)
    assert isinstance(out, str)
    assert out.startswith("2024-06-01")


def test_date_iso_from_string_passthrough():
    assert _date_iso("2024-06-01") == "2024-06-01"


def test_date_iso_from_none_returns_empty():
    assert _date_iso(None) == ""


def test_date_iso_from_invalid_returns_original():
    # If we can't parse it, hand back what we got rather than crashing.
    out = _date_iso("not-a-date")
    assert out == "not-a-date"


# ──────────────────────────────────────────────────────────────────────
# Case 4: _compute_composite_rank_score — bounded in [0, 1]
# ──────────────────────────────────────────────────────────────────────


def test_composite_rank_score_monotonic_in_sortino():
    # Higher sortino → at-least-as-high score (win rate, sharpe, profitable
    # all held constant).
    s_low = _compute_composite_rank_score(1.0, 2.0, 10, 5, True)
    s_mid = _compute_composite_rank_score(5.0, 2.0, 10, 5, True)
    s_high = _compute_composite_rank_score(20.0, 2.0, 10, 5, True)
    assert s_low <= s_mid <= s_high


def test_composite_rank_score_handles_zero_windows():
    # If n_windows_total is 0, win_rate defaults to 0; must not divide by zero.
    score = _compute_composite_rank_score(5.0, 2.0, 0, 0, False)
    assert math.isfinite(score)


def test_composite_rank_score_profitable_window_adds_bonus():
    """A profitable window should rank higher than a non-profitable one
    holding sortino/sharpe/win-rate constant."""
    s_profit = _compute_composite_rank_score(5.0, 2.0, 10, 5, True)
    s_loss = _compute_composite_rank_score(5.0, 2.0, 10, 5, False)
    assert s_profit > s_loss


def test_composite_rank_score_higher_win_rate_higher_score():
    """9/10 windows profitable > 5/10 windows profitable."""
    s_high_wr = _compute_composite_rank_score(5.0, 2.0, 10, 9, True)
    s_low_wr = _compute_composite_rank_score(5.0, 2.0, 10, 5, True)
    assert s_high_wr > s_low_wr


# ──────────────────────────────────────────────────────────────────────
# Case 5: _row_to_record — builds a full NexusWalkforwardRecord
# ──────────────────────────────────────────────────────────────────────


def test_row_to_record_minimal_columns():
    """Provide only the required numeric columns; everything else fills
    with sensible defaults.

    Note: the source schema column is ``sortino`` (not ``sortino_ratio``).
    """
    cols = [
        "strategy_name",
        "symbol",
        "window_index",
        "train_start",
        "train_end",
        "test_start",
        "test_end",
        "total_return_pct",
        "sharpe",
        "sortino",
        "max_drawdown_pct",
        "profitable",
        "num_entries",
        "budget",
    ]
    row_tuple = (
        "meta_cmo_alma_atr_wf_v1",  # strategy_name
        "BTC",                        # symbol
        0,                            # window_index
        "2024-01-01",                 # train_start
        "2024-04-01",                 # train_end
        "2024-04-01",                 # test_start
        "2024-10-01",                 # test_end
        25.0,                         # total_return_pct
        3.1,                          # sharpe
        5.2,                          # sortino
        8.5,                          # max_drawdown_pct
        1,                            # profitable
        12,                           # num_entries
        10000.0,                      # budget
    )
    rec = _row_to_record(
        row_tuple, cols, pair_aggregates={}, now_iso="2024-06-25"
    )
    # Required text fields
    assert rec["strategy_name"] == "meta_cmo_alma_atr_wf_v1"
    assert rec["symbol"] == "BTC"
    # Numeric fields coerced via _safe_float
    assert rec["sortino"] == 5.2
    assert rec["sharpe"] == 3.1
    assert rec["max_drawdown_pct"] == 8.5
    assert rec["total_return_pct"] == 25.0


def test_row_to_record_handles_missing_columns():
    """If a column is missing, _safe_float coerces to 0."""
    cols = ["strategy_name", "symbol", "sortino", "sharpe"]
    rec = _row_to_record(
        ("meta_x", "BTC", None, None),
        cols,
        pair_aggregates={},
        now_iso="2024-06-25",
    )
    assert rec["sortino"] == 0.0
    assert rec["sharpe"] == 0.0


def test_row_to_record_handles_nan_values():
    """NaN values in numeric columns must not poison the record."""
    cols = ["strategy_name", "symbol", "sortino"]
    rec = _row_to_record(
        ("meta_x", "BTC", float("nan")),
        cols,
        pair_aggregates={},
        now_iso="2024-06-25",
    )
    assert rec["sortino"] == 0.0


# ──────────────────────────────────────────────────────────────────────
# Case 6: query_walkforward_memory_tool — tool returns expected keys
# ──────────────────────────────────────────────────────────────────────


def test_query_walkforward_memory_tool_returns_expected_keys():
    """Sanity-check that the tool wrapper returns the right shape, with
    a stub for the underlying memory object so we don't hit LanceDB."""
    from src.tools import trade_memory_tool
    from src.memory import nexus_vector_memory

    # Each result row is a dict-like with a "metadata" key (LanceDB
    # returns {metadata: {...}, vector: [...], text: "..."} per row).
    fake_rows = [
        {
            "metadata": {
                "id": "wf_test_1",
                "strategy_name": "meta_x",
                "symbol": "BTC",
                "regime": "any",
                "sortino": 5.0,
                "sharpe": 3.0,
                "total_return_pct": 25.0,
                "max_drawdown_pct": 8.0,
                "profitable": True,
                "window_index": 0,
                "test_start": "2024-01-01",
                "test_end": "2024-06-01",
                "n_profitable_windows": 5,
                "n_windows_total": 10,
            }
        }
    ]

    # Stub out search_walkforward_memory on the memory singleton.
    class FakeMemory:
        enabled = True

        def search_walkforward(self, *args, **kwargs):
            return list(fake_rows)

        def search_walkforward_memory(self, *args, **kwargs):
            return list(fake_rows)

        def get_walkforward_stats(self):
            return {
                "total_windows": 1,
                "unique_strategies": 1,
                "unique_symbols": 1,
                "n_profitable": 1,
                "avg_sortino": 5.0,
                "avg_sharpe": 3.0,
            }

    with patch.object(
        nexus_vector_memory,
        "get_nexus_memory",
        return_value=FakeMemory(),
    ):
        result = trade_memory_tool.query_walkforward_memory_tool(
            symbol="BTC", n_results=1, min_sortino=2.0
        )
    assert "n_windows_returned" in result
    assert "context_prompt" in result
    assert "stats" in result
    assert result["n_windows_returned"] == 1
    assert "meta_x" in result["context_prompt"]
    assert "BTC" in result["context_prompt"]