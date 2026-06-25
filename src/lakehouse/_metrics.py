"""Performance metrics for Nexus strategy ranking.

Sortino is the primary ranking metric for Nexus crypto strategies because
it penalizes only downside volatility — which is the risk that actually
hurts PnL for a long-biased book. Sharpe penalizes both up and down vol,
which is misleading for asymmetric crypto payoffs where upside vol is
welcomed.

This module is intentionally tiny: just the two helpers we need across
the lakehouse readers and walk-forward validation. Annualization is
pluggable because bar cadence varies (4h crypto = 2190 bars/yr,
1h crypto = 8760, 1d = 365). When ``bars_per_year`` is None, returns
the raw (un-annualized) ratio.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def sortino(
    returns: pd.Series,
    mar: float = 0.0,
    bars_per_year: Optional[float] = None,
) -> float:
    """Annualized Sortino ratio.

    Sortino = mean(excess_return) / std(downside_return) * sqrt(bars_per_year).

    Parameters
    ----------
    returns : pd.Series
        Per-bar return series. NaN values are dropped. Must be a numeric
        dtype; non-numeric series raise TypeError via the ``<`` comparison.
    mar : float, default 0.0
        Minimum acceptable return (the "MAR" line). Returns below this are
        counted as downside. 0.0 is the canonical choice for absolute-return
        strategies (we are not benchmarking against a risk-free rate per-bar).
    bars_per_year : float | None, default None
        If set, multiplies the raw ratio by ``sqrt(bars_per_year)`` to
        annualize. 2190 for 4h crypto (24 * 365 / 4 ≈ 2190), 8760 for 1h,
        365 for daily. If None, returns the raw per-bar ratio without
        annualization — useful when comparing across mixed cadences.

    Returns
    -------
    float
        The annualized Sortino ratio, or:

        - ``float('inf')`` if there are no downside returns AND the mean
          return is above the MAR (a "perfect" strategy under Sortino).
        - ``0.0`` if there are no downside returns AND the mean return is
          at or below the MAR (no upside above MAR, no downside to divide by).
        - ``0.0`` if the downside deviation is zero with mean <= MAR.
        - ``0.0`` if the series is empty or all-NaN.

    Notes
    -----
    Downside deviation uses ``ddof=1`` (sample std) so a 2-point series
    doesn't blow up. With ``ddof=0`` a 1-point downside population has
    std == 0 and we always return 0; we choose ``ddof=1`` to be consistent
    with pandas default and most quant libraries.
    """
    # Drop NaN upfront so downstream comparisons don't trip on missing data
    clean = returns.dropna()
    if clean.empty:
        return 0.0

    downside = clean[clean < mar]
    mean_excess = float(clean.mean()) - mar

    if len(downside) == 0:
        # No downside observations.
        # If mean > MAR: perfect Sortino (infinite upside, no downside).
        # If mean <= MAR: zero (no upside to reward, no downside to divide).
        if mean_excess > 0:
            return float("inf")
        return 0.0

    dd = float(downside.std(ddof=1))
    # Treat near-zero downside deviation as zero. Due to floating-point
    # round-off, std(ddof=1) of a perfectly identical series can be on
    # the order of 1e-17; without this guard, dividing mean by that
    # returns a meaningless ~1e15. We use a relative tolerance against
    # the magnitude of the mean excess to detect "constant downside".
    is_zero_dd = dd == 0.0 or (
        abs(mean_excess) > 0 and dd < abs(mean_excess) * 1e-12
    )
    if is_zero_dd:
        # Downside observations exist but are all identical (e.g. constant
        # negative bar). Cannot compute a meaningful ratio.
        return float("inf") if mean_excess > 0 else 0.0

    raw = mean_excess / dd
    if bars_per_year is not None and bars_per_year > 0:
        raw *= float(bars_per_year) ** 0.5
    return float(raw)


def detect_bars_per_year(index: pd.DatetimeIndex) -> float:
    """Infer ``bars_per_year`` from the median bar spacing of an OHLCV index.

    Used so the Sortino helper can be called without the caller having to
    know the bar cadence ahead of time. Robust to weekend/holiday gaps in
    daily bars (we use ``total_seconds() / n`` rather than per-pair diff,
    so a weekend gap averages out over the whole series).

    Parameters
    ----------
    index : pd.DatetimeIndex
        A (sorted, increasing) DatetimeIndex of bar timestamps. Any
        timezone-aware or naive index is accepted; we only use the
        total second span.

    Returns
    -------
    float
        Estimated bars-per-year. 365.25 if the index has fewer than 2
        timestamps (insufficient data). Otherwise
        ``365.25 * 86400 / median_seconds_per_bar``.

        Typical values:
        - 4h crypto   ≈ 2190 (24 * 365.25 / 4)
        - 1h crypto   ≈ 8766
        - 1d equity   ≈ 365 (weekends don't matter much for monthly+ views)
        - 1m crypto   ≈ 525960

    Notes
    -----
    Uses the *span / count* heuristic instead of per-pair ``diff().median()``
    because per-pair diffs overweight gaps (overnight, weekend, exchange
    downtime). For uniform 4h crypto data the two heuristics agree to
    within 1 bar/year.
    """
    if len(index) < 2:
        return 365.0

    # total_seconds() returns float; on tz-aware indexes, .total_seconds()
    # works the same way (we don't care about absolute epoch, just span).
    span_seconds = (index[-1] - index[0]).total_seconds()
    if span_seconds <= 0:
        return 365.0

    n_bars = len(index) - 1
    median_seconds = span_seconds / max(n_bars, 1)
    if median_seconds <= 0:
        return 365.0

    seconds_per_year = 365.25 * 86400.0
    return seconds_per_year / median_seconds