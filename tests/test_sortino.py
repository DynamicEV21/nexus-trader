"""Unit tests for src/lakehouse/_metrics.py.

Four cases per the Sortino refactor brief:
1. Hand-crafted returns series → known Sortino value
2. detect_bars_per_year on 4h index → ~2190
3. detect_bars_per_year on 1d index → ~365
4. Edge cases: all-positive → inf; all-negative → 0; constant → 0
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.lakehouse._metrics import detect_bars_per_year, sortino


# ──────────────────────────────────────────────────────────────────────
# Case 1: hand-crafted series with a known answer
# ──────────────────────────────────────────────────────────────────────


def test_sortino_hand_crafted_series():
    """Mixed-sign series with a verifiable Sortino.

    Returns: [+0.02, -0.01, +0.03, -0.005, +0.015]
    Mean  = 0.01
    Downside = [-0.01, -0.005]
    Downside std (ddof=1):
        mean = -0.0075, deviations = [-0.0025, +0.0025], sq = 6.25e-6 each
        var = 1.25e-5, std = sqrt(1.25e-5) ≈ 0.0035355339
    Raw Sortino = 0.01 / 0.0035355339 ≈ 2.8284271
    Annualized (bars_per_year=2190) → ≈ 2.8284271 * sqrt(2190) ≈ 132.39

    We test the unannualized value (raw ratio) directly for clarity, then
    assert annualization scales by sqrt(bars_per_year).
    """
    returns = pd.Series([0.02, -0.01, 0.03, -0.005, 0.015])
    raw = sortino(returns)
    # Raw = mean / downside_std
    # mean = 0.01; downside = [-0.01, -0.005]; std(ddof=1) = sqrt(1.25e-5)
    expected_raw = 0.01 / math.sqrt(1.25e-5)
    assert math.isclose(raw, expected_raw, rel_tol=1e-9), (
        f"raw Sortino {raw} != expected {expected_raw}"
    )

    # Annualized at 4h crypto cadence (2190 bars/yr) must equal raw * sqrt(2190)
    annual = sortino(returns, bars_per_year=2190)
    assert math.isclose(annual, raw * math.sqrt(2190), rel_tol=1e-9), (
        f"annualized Sortino {annual} != raw*sqrt(2190) {raw * math.sqrt(2190)}"
    )


def test_sortino_with_mar_above_zero():
    """When MAR > 0, returns equal to MAR are NOT downside.

    Series [0.01, 0.01, 0.01, 0.01] with MAR=0.01: no downside returns,
    mean == MAR → return 0.0 (mean is not strictly above MAR).
    """
    returns = pd.Series([0.01, 0.01, 0.01, 0.01])
    assert sortino(returns, mar=0.01) == 0.0

    # With MAR=0.0, no downside → mean > 0 → inf
    assert sortino(returns, mar=0.0) == float("inf")


def test_sortino_mar_above_mean_returns_zero():
    """If MAR > mean and there are observations below MAR, the ratio is negative.

    Series [-0.05, -0.05, -0.05] with MAR=0.0:
        mean = -0.05, downside_std = 0 (all identical)
        → mean <= MAR → 0.0
    Series [-0.05, -0.05, -0.04] with MAR=0.0:
        mean = -0.0466667, downside = [-0.05, -0.05, -0.04]
        downside std > 0, mean < 0 → negative ratio.
    """
    # All identical downside → 0
    s1 = pd.Series([-0.05, -0.05, -0.05])
    assert sortino(s1, mar=0.0) == 0.0

    # Mixed downside → negative ratio (not 0)
    s2 = pd.Series([-0.05, -0.05, -0.04])
    val = sortino(s2, mar=0.0)
    assert val < 0.0, f"expected negative Sortino, got {val}"


def test_sortino_drops_nan():
    """NaN values should be dropped, not propagated."""
    s = pd.Series([0.02, np.nan, -0.01, 0.03, np.nan, -0.005])
    val = sortino(s)
    # Equivalent to: [0.02, -0.01, 0.03, -0.005]
    expected = sortino(pd.Series([0.02, -0.01, 0.03, -0.005]))
    assert math.isclose(val, expected, rel_tol=1e-12)


# ──────────────────────────────────────────────────────────────────────
# Case 2: detect_bars_per_year on 4h index → ~2190
# ──────────────────────────────────────────────────────────────────────


def test_detect_bars_per_year_4h():
    """A 4h bar index should yield ~2190 bars/year (24*365.25/4)."""
    # Build a 4h index spanning ~30 days
    idx = pd.date_range("2026-01-01", periods=180, freq="4h")
    bpy = detect_bars_per_year(idx)
    expected = 24.0 * 365.25 / 4.0  # 2191.5
    assert math.isclose(bpy, expected, rel_tol=1e-3), (
        f"4h bars/year {bpy} != expected {expected}"
    )
    # And it should be close to 2190 per the brief
    assert 2185 <= bpy <= 2195, f"4h bars/year {bpy} not in [2185, 2195]"


def test_detect_bars_per_year_4h_short_window():
    """Even a short 4h window should converge close to 2190."""
    idx = pd.date_range("2026-01-01", periods=24, freq="4h")  # 4 days
    bpy = detect_bars_per_year(idx)
    expected = 24.0 * 365.25 / 4.0
    assert math.isclose(bpy, expected, rel_tol=1e-6)


# ──────────────────────────────────────────────────────────────────────
# Case 3: detect_bars_per_year on 1d index → ~365
# ──────────────────────────────────────────────────────────────────────


def test_detect_bars_per_year_1d():
    """A 1d bar index should yield ~365 bars/year."""
    idx = pd.date_range("2025-01-01", periods=365, freq="1D")
    bpy = detect_bars_per_year(idx)
    expected = 365.25
    assert math.isclose(bpy, expected, rel_tol=1e-3), (
        f"1d bars/year {bpy} != expected {expected}"
    )
    # Brief target: ~365
    assert 360 <= bpy <= 370


def test_detect_bars_per_year_1d_short_window():
    """A 1d index of 14 days still gives ~365.25 (span/count heuristic)."""
    idx = pd.date_range("2026-01-01", periods=14, freq="1D")
    bpy = detect_bars_per_year(idx)
    assert math.isclose(bpy, 365.25, rel_tol=1e-6)


# ──────────────────────────────────────────────────────────────────────
# Case 4: edge cases
# ──────────────────────────────────────────────────────────────────────


def test_sortino_all_positive_returns_inf():
    """All-positive returns with no downside → Sortino = inf."""
    s = pd.Series([0.01, 0.02, 0.005, 0.03, 0.015])
    assert sortino(s) == float("inf")


def test_sortino_all_negative_constant_returns_zero():
    """All-equal-negative returns → downside_std = 0, mean <= 0 → 0."""
    s = pd.Series([-0.01, -0.01, -0.01, -0.01])
    assert sortino(s) == 0.0


def test_sortino_constant_zero_returns_zero():
    """All-zero returns → no downside below MAR, mean == MAR → 0."""
    s = pd.Series([0.0, 0.0, 0.0, 0.0])
    assert sortino(s) == 0.0


def test_sortino_constant_positive_returns_inf():
    """All-equal-positive returns → no downside, mean > MAR → inf."""
    s = pd.Series([0.02, 0.02, 0.02])
    assert sortino(s) == float("inf")


def test_sortino_empty_series_returns_zero():
    """Empty series → 0.0 (no data, no signal)."""
    s = pd.Series([], dtype=float)
    assert sortino(s) == 0.0


def test_sortino_all_nan_returns_zero():
    """All-NaN series → 0.0 after dropna."""
    s = pd.Series([np.nan, np.nan, np.nan])
    assert sortino(s) == 0.0


def test_detect_bars_per_year_empty_index_returns_default():
    """Empty index → fallback 365.0."""
    idx = pd.DatetimeIndex([])
    assert detect_bars_per_year(idx) == 365.0


def test_detect_bars_per_year_single_bar_returns_default():
    """Single timestamp → fallback 365.0 (insufficient data)."""
    idx = pd.DatetimeIndex(["2026-01-01"])
    assert detect_bars_per_year(idx) == 365.0


# ──────────────────────────────────────────────────────────────────────
# Bonus: roundtrip property — annualized Sortino is bars_per_year invariant
# when the bar cadence matches the annualization factor
# ──────────────────────────────────────────────────────────────────────


def test_sortino_4h_annualized_matches_canonical_value():
    """For a known 4h-crypto strategy, annualized Sortino should be
    raw * sqrt(2190) within 1e-9."""
    np.random.seed(42)
    # Simulate 1 year of 4h bars (~2190 returns)
    rets = pd.Series(np.random.normal(0.001, 0.02, 2190))
    raw = sortino(rets)
    annual = sortino(rets, bars_per_year=2190)
    assert math.isclose(annual, raw * math.sqrt(2190), rel_tol=1e-9)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])