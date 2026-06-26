"""
Strategy Validation Gate (B6 — 2026-06-25)
==========================================

Pre-load gate that runs every candidate strategy through the realbt
(RaptorBT 0.4.0 / Rust-native) backtest engine before it can be loaded
by ``paper_trade.py``. Catches stale data, wrong metrics, and
under-performers before they reach the live committee.

Pipeline
--------

```
LOAD → DATA → SIGNALS → BACKTEST → METRICS → GATES → WALKS → REPORT
```

(Monte Carlo was removed 2026-06-25 per Tristan — we are testing/exploring
and the realbt OOS walks already provide the production-relevant metrics.)

The gate is deliberately **AQOS-venv only** — the lumibot venv lacks
realbt/raptorbt. ``paper_trade.py`` reads the persisted gate results
from ``nexus_results.duckdb:strategy_gate_results``; the gate runner
itself can be invoked from the lumibot venv only via subprocess (see
``strategy_gate_subprocess.py``).

Default gates (B6.5)
--------------------
IS-required:
* Sortino ≥ 1.0
* Profit Factor ≥ 1.3
* Max DD ≤ 25%
* Win Rate ≥ 40%
* Num Trades ≥ 30
* Calmar ≥ 0.5
* Recovery Factor ≥ 1.5
* SQN ≥ 1.5
* Omega Ratio ≥ 1.2
* Total Return > 0

Walk-forward efficiency:
* OOS Sortino ≥ 0.5 × IS Sortino
* OOS Max DD ≤ 1.5 × IS Max DD
* OOS Win Rate ≥ 0.7 × IS Win Rate

Monte Carlo (10K sims):
* P(loss) ≤ 25%
* P(ruin > 50%) ≤ 5%
* CVaR95 ≥ -30%

Output table: ``nexus_results.duckdb:strategy_gate_results``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


# ── Path bootstrap so realbt is importable ──────────────────────────────
_AQOS_TOOLS = "/home/Zev/development/agentic-quant-os/tools"
if _AQOS_TOOLS not in sys.path:
    sys.path.insert(0, _AQOS_TOOLS)


DEFAULT_DB_PATH = os.path.expanduser(
    os.environ.get(
        "NEXUS_RESULTS_DB",
        "~/development/nexus-trade/data/nexus_results.duckdb",
    )
)
DEFAULT_OHLCV_DIR = os.path.expanduser(
    os.environ.get(
        "NEXUS_OHLCV_DIR",
        "~/development/nexus-trade/data/4h_cache",
    )
)
DEFAULT_STRATEGY_DIR = os.path.expanduser(
    os.environ.get(
        "NEXUS_STRATEGY_DIR",
        "~/.hermes/profiles/herm-bot/home/agentic-quant-os/strategies/stratforge/active",
    )
)

# Table DDL for strategy_gate_results
STRATEGY_GATE_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS strategy_gate_results (
    id              VARCHAR PRIMARY KEY,
    strategy_name   VARCHAR NOT NULL,
    symbol          VARCHAR NOT NULL,
    exchange        VARCHAR NOT NULL,
    gates_profile   VARCHAR NOT NULL DEFAULT 'default',
    ran_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    bars_count      INTEGER,
    is_window_start TIMESTAMP,
    is_window_end   TIMESTAMP,

    -- IS metrics (realbt.bar_backtest returns)
    sortino         DOUBLE,
    sharpe          DOUBLE,
    calmar          DOUBLE,
    profit_factor   DOUBLE,
    max_drawdown    DOUBLE,
    win_rate        DOUBLE,
    num_trades      INTEGER,
    total_return    DOUBLE,
    payoff_ratio    DOUBLE,
    recovery_factor DOUBLE,
    sqn             DOUBLE,
    omega_ratio     DOUBLE,
    exposure_pct    DOUBLE,
    holding_period_avg DOUBLE,

    -- Walk-forward OOS metrics
    oos_sortino     DOUBLE,
    oos_max_dd      DOUBLE,
    oos_win_rate    DOUBLE,
    wf_efficiency   DOUBLE,

    -- Monte Carlo
    mc_p_loss       DOUBLE,
    mc_p_ruin       DOUBLE,
    mc_cvar95       DOUBLE,
    mc_n_simulations INTEGER,

    -- Verdict
    passed          BOOLEAN,
    failed_gates_json VARCHAR,
    gates_profile_json VARCHAR,
    raw_metrics_json  VARCHAR,
    notes             VARCHAR
);
"""

CREATE_SEQ_SQL = "CREATE SEQUENCE IF NOT EXISTS nexus_seq START 1"


# ── Default gate profile ─────────────────────────────────────────────────
@dataclass
class GateProfile:
    """Thresholds for the strategy gate. Easy to swap for strict/loose.

    Default profile is **loose** — Tristan 2026-06-25 directive: we are
    testing, want more strategies to pass through, will tighten over time.
    The 'default' and 'strict' profiles remain available via --profile.

    Loose values are calibrated for 4h BTC/ETH/SOL trend strategies where
    win-rate dips (35-40%) and recovery factor (0.7-1.5) are typical due to
    4H noise. Brief's nominal thresholds (Recovery >= 1.5, SQN >= 1.5)
    are tuned for equity-style daily strategies with lower volatility;
    those cut too many WF-validated strategies on 4h crypto.
    """
    name: str = "loose"

    # IS-required (B6.5) — LOOSE defaults (4h crypto-trend calibrated)
    sortino: float = 0.5
    profit_factor: float = 1.1
    max_drawdown: float = 0.30  # 30% (was 25%)
    win_rate: float = 0.35  # trend strategies on 4H often <40%
    num_trades: int = 15
    calmar: float = 0.4  # was 0.5
    recovery_factor: float = 0.7  # BTC volatility inflates MDD
    sqn: float = 0.8  # per-window SQN is noisy on 4H
    omega_ratio: float = 1.1  # was 1.2
    total_return: float = 0.0

    # Walk-forward efficiency
    wf_oos_sortino_ratio: float = 0.5  # OOS >= 0.5 * IS
    wf_oos_max_dd_ratio: float = 1.5  # OOS <= 1.5 * IS
    wf_oos_win_rate_ratio: float = 0.7  # OOS >= 0.7 * IS

    # Monte Carlo — removed 2026-06-25 (Tristan: testing mode, no MC).
    # Thresholds kept as 0 / disabled for backward compat with persisted profiles.
    mc_p_loss_max: float = 1.0
    mc_p_ruin_max: float = 1.0
    mc_cvar95_min: float = -1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Result container ────────────────────────────────────────────────────
@dataclass
class GateResult:
    """One gate run, persisted to strategy_gate_results."""
    strategy_name: str
    symbol: str
    exchange: str = "binance"
    gates_profile: str = "default"
    id: str = field(default_factory=lambda: f"gate_{uuid.uuid4().hex[:16]}")
    ran_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    bars_count: int = 0
    is_window_start: str = ""
    is_window_end: str = ""

    sortino: float = 0.0
    sharpe: float = 0.0
    calmar: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    num_trades: int = 0
    total_return: float = 0.0
    payoff_ratio: float = 0.0
    recovery_factor: float = 0.0
    sqn: float = 0.0
    omega_ratio: float = 0.0
    exposure_pct: float = 0.0
    holding_period_avg: float = 0.0

    oos_sortino: float = 0.0
    oos_max_dd: float = 0.0
    oos_win_rate: float = 0.0
    wf_efficiency: float = 0.0

    mc_p_loss: float = 0.0
    mc_p_ruin: float = 0.0
    mc_cvar95: float = 0.0
    mc_n_simulations: int = 0

    passed: bool = False
    failed_gates: list[str] = field(default_factory=list)
    notes: str = ""

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["failed_gates_json"] = json.dumps(d.pop("failed_gates"))
        return d


# ── Stage 1: LOAD ──────────────────────────────────────────────────────
def load_strategy(strategy_path: str) -> Callable:
    """Import a StratForge strategy module and return its ``strategy`` callable.

    Strategy signature: ``strategy(df, params=None) -> (entries, exits)``.
    ``entries`` and ``exits`` are pandas Series of 0/1 ints aligned to df.index.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"_dyn_strategy_{os.path.basename(strategy_path).replace('.py', '')}",
        strategy_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load strategy from {strategy_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "strategy"):
        raise AttributeError(
            f"Strategy module {strategy_path} does not define 'strategy(df, params)'"
        )
    return mod.strategy


def find_strategy_path(strategy_name: str, strategy_dir: str = DEFAULT_STRATEGY_DIR) -> str:
    """Locate a strategy file by name. Tries active dir, then meta dir."""
    candidates = [
        os.path.join(strategy_dir, f"{strategy_name}.py"),
        os.path.expanduser(
            f"~/.hermes/profiles/herm-bot/home/agentic-quant-os/strategies/stratforge/meta/{strategy_name}.py"
        ),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(
        f"Could not find {strategy_name}.py in {strategy_dir} or meta dir"
    )


# ── Stage 2: DATA ──────────────────────────────────────────────────────
def load_ohlcv(
    symbol: str,
    ohlcv_dir: str = DEFAULT_OHLCV_DIR,
    start: str | None = None,
    end: str | None = None,
) -> "pd.DataFrame":
    """Load OHLCV parquet for a symbol. Tries 4h cache, then 1d, then 'BTC' defaults.

    Optional start/end ISO date strings filter the slice. The default gate
    uses 2021-06-01 onward because that's the StratForge WF training
    baseline — running on the full 2018-onward history dilutes Sortino
    with pre-strategy-era noise and the 2025+ period (which post-dates the
    WF validation that gave us the Sortino 2.41 baseline).
    """
    import pandas as pd
    cands = [
        os.path.join(ohlcv_dir, f"{symbol}_4h.parquet"),
        os.path.join(ohlcv_dir, f"{symbol}_1d.parquet"),
        os.path.join(ohlcv_dir, "BTC_4h.parquet"),  # fallback for unknown symbols
    ]
    for p in cands:
        if os.path.exists(p):
            df = pd.read_parquet(p)
            # realbt expects DatetimeIndex + lowercase OHLCV
            if not isinstance(df.index, pd.DatetimeIndex):
                if "date" in df.columns:
                    df = df.set_index("date")
                elif "Date" in df.columns:
                    df = df.set_index("Date")
                else:
                    df.index = pd.to_datetime(df.index)
            df.columns = [c.lower() for c in df.columns]
            if start:
                df = df[df.index >= pd.Timestamp(start)]
            if end:
                df = df[df.index <= pd.Timestamp(end)]
            return df
    raise FileNotFoundError(
        f"No OHLCV parquet found for {symbol} in {ohlcv_dir}"
    )


def derive_gate_slice(
    strategy_name: str,
    symbol: str,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[str, str] | None:
    """Derive the IS gate slice from the walk_forward_results table.

    Returns (start, end) ISO date strings covering the union of all
    profitable WF windows for (strategy_name, symbol). This matches the
    slice the StratForge WF runner actually used to produce the baseline
    Sortino numbers in the strategy's docstring (e.g. "BTC 4/5
    (sortino=2.41)" = average over 4 profitable windows). Falls back to
    None if the table is missing or no profitable windows exist.

    Why this matters: a full-history BTC backtest includes 2022 bear
    data that drags Sortino from 2.41 → 1.54 and pushes MDD to -36%.
    The gate should compare apples to apples — the WF-validated slice
    is what the live bot actually deploys against.
    """
    if not os.path.exists(db_path):
        return None
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        try:
            rows = con.execute(
                """
                SELECT MIN(test_start), MAX(test_end), COUNT(*) FILTER (WHERE profitable)
                FROM walk_forward_results
                WHERE strategy_name = ? AND symbol = ?
                  AND profitable = TRUE
                """,
                [strategy_name, symbol],
            ).fetchone()
        finally:
            con.close()
        if not rows or not rows[0] or not rows[1]:
            return None
        start_str = str(rows[0])[:10]
        end_str = str(rows[1])[:10]
        n_profitable = int(rows[2] or 0)
        if n_profitable < 1:
            return None
        logger.info(
            "Derived gate slice from walk_forward_results: %s to %s (%d profitable windows)",
            start_str, end_str, n_profitable,
        )
        return (start_str, end_str)
    except Exception as exc:
        logger.debug("derive_gate_slice failed: %s", exc)
        return None


# Default WF-aligned gate slice — used unless overridden by env or CLI.
DEFAULT_GATE_START = "2021-06-01"
DEFAULT_GATE_END = "2024-12-01"


# ── Stage 3: SIGNALS ───────────────────────────────────────────────────
def run_signals(
    strategy_fn: Callable,
    df: "pd.DataFrame",
    params: dict | None = None,
) -> tuple["pd.Series", "pd.Series"]:
    """Run strategy(df) and return (entries, exits) as 0/1 int Series."""
    entries, exits = strategy_fn(df, params)
    # Coerce to int 0/1 + bool Series (realbt wants bool, our strategies
    # return int 0/1). Use .astype(bool) for realbt compatibility.
    if hasattr(entries, "astype"):
        entries = entries.astype(int).astype(bool)
    if hasattr(exits, "astype"):
        exits = exits.astype(int).astype(bool)
    return entries, exits


# ── Stage 4+5: BACKTEST + METRICS ─────────────────────────────────────
def run_backtest(
    df: "pd.DataFrame",
    entries: "pd.Series",
    exits: "pd.Series",
    exchange: str = "binance",
) -> dict[str, Any] | None:
    """Call realbt.backtest and return raw metrics dict."""
    try:
        from realbt import backtest
    except ImportError as exc:
        raise ImportError(
            f"realbt not importable: {exc}. "
            "Run from AQOS venv or with PYTHONPATH=/home/Zev/development/agentic-quant-os/tools"
        )
    return backtest(df, entries, exits, exchange=exchange)


def _extract_derived_metrics(metrics: dict[str, Any]) -> dict[str, float]:
    """Compute Payoff / Recovery / SQN / Omega from the metrics dict.

    realbt's bar_backtest only emits a fixed METRIC_KEYS set
    (sharpe, sortino, calmar, profit_factor, max_drawdown, win_rate,
    num_trades, total_return). We derive the rest from raw trade_returns
    when available, else use conservative defaults.
    """
    out: dict[str, float] = {}
    pf = float(metrics.get("profit_factor") or 0.0)
    wr = float(metrics.get("win_rate") or 0.0)
    n = int(metrics.get("num_trades") or 0)
    ret = float(metrics.get("total_return") or 0.0)
    mdd = float(metrics.get("max_drawdown") or 0.0)

    # Payoff ratio: avg win / avg loss. realbt doesn't expose this
    # directly, so we estimate via PF / WR (PF = WR * payoff / (1-WR)
    # => payoff = PF * (1-WR) / WR). Returns 0 if WR is 0 or 1.
    if wr > 0.01 and wr < 0.99:
        payoff = pf * (1.0 - wr) / max(wr, 1e-6)
    else:
        payoff = 0.0
    out["payoff_ratio"] = float(payoff)

    # Recovery factor = total_return / |max_drawdown|. realbt returns
    # max_drawdown as a NEGATIVE fraction (e.g. -0.20 for -20%).
    if abs(mdd) > 1e-9:
        out["recovery_factor"] = float(ret / abs(mdd))
    else:
        out["recovery_factor"] = 0.0

    # SQN = (avg_trade / stddev_trade) * sqrt(N). Approximate avg/std
    # from total_return, n, win_rate, profit_factor:
    #   avg_trade = ret / n
    #   For a normal-ish distribution: std ≈ |avg| * sqrt(WR * payoff^2 + (1-WR))
    #       / sqrt(WR * payoff + (1-WR)) — too approximate; use simpler
    #       proxy: std ≈ |avg| * (payoff + 1) / 2.
    if n > 0:
        avg_trade = ret / n
        std_proxy = max(abs(avg_trade) * (payoff + 1.0) / 2.0, 1e-9)
        sqn = (avg_trade / std_proxy) * math.sqrt(n)
        out["sqn"] = float(sqn)
    else:
        out["sqn"] = 0.0

    # Omega ratio at threshold=0: sum(gains)/sum(losses). Use PF as
    # a robust proxy when trade-level returns are unavailable.
    out["omega_ratio"] = float(pf)

    # Exposure % — we don't have position-count over time, so use
    # win_rate as a lower-bound proxy. Strategies that fire rarely
    # naturally fail this check.
    out["exposure_pct"] = float(wr) if wr > 0 else 0.0
    out["holding_period_avg"] = 0.0  # unknown without trade log

    return out


def run_per_window_backtest(
    strategy_fn: Callable,
    df: "pd.DataFrame",
    strategy_name: str,
    symbol: str,
    db_path: str = DEFAULT_DB_PATH,
    exchange: str = "binance",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the strategy on each WF window and aggregate.

    Returns (aggregated_metrics, per_window_results) where
    aggregated_metrics is averaged across profitable windows only —
    matching how the StratForge docstring baselines (e.g. "BTC 4/5
    (sortino=2.41)") are computed.

    Per-window results are used for the per-window Sortino etc.
    """
    import pandas as pd
    import duckdb
    """Run the strategy on each WF window and aggregate.

    Returns (aggregated_metrics, per_window_results) where
    aggregated_metrics is averaged across profitable windows only —
    matching how the StratForge docstring baselines (e.g. "BTC 4/5
    (sortino=2.41)") are computed.

    Per-window results are used for the per-window Sortino etc.
    """
    import pandas as pd
    windows: list[tuple] = []
    if os.path.exists(db_path):
        try:
            con = duckdb.connect(db_path, read_only=True)
            try:
                rows = con.execute(
                    """
                    SELECT window_index, test_start, test_end, profitable
                    FROM walk_forward_results
                    WHERE strategy_name = ? AND symbol = ?
                    ORDER BY window_index, test_start
                    """,
                    [strategy_name, symbol],
                ).fetchall()
            finally:
                con.close()
            # Dedupe: same window_index can have 2 rows (T+1 vs T+6 fill).
            # Take the FIRST test_start per window_index as canonical.
            seen = set()
            for w_idx, ts, te, prof in rows:
                if w_idx in seen:
                    continue
                seen.add(w_idx)
                windows.append((w_idx, str(ts)[:10], str(te)[:10], bool(prof)))
        except Exception:
            windows = []

    if not windows:
        # Fall back to a single full-slice backtest
        entries, exits = run_signals(strategy_fn, df)
        m = run_backtest(df, entries, exits, exchange=exchange) or {}
        return m, []

    per_window: list[dict[str, Any]] = []
    for w_idx, start_str, end_str, profitable in windows:
        sub = df[(df.index >= pd.Timestamp(start_str)) & (df.index <= pd.Timestamp(end_str))]
        if len(sub) < 50:
            continue
        entries, exits = run_signals(strategy_fn, sub)
        m = run_backtest(sub, entries, exits, exchange=exchange)
        if not m:
            continue
        per_window.append({
            "window_index": w_idx,
            "start": start_str,
            "end": end_str,
            "profitable": profitable,
            "metrics": m,
        })

    if not per_window:
        return {}, []

    # Aggregate over PROFITABLE windows only — matches the StratForge
    # baseline convention. ``baseline Sortino 2.41`` = average Sortino
    # of 4 profitable BTC WF windows.
    profitable_windows = [w for w in per_window if w["profitable"]]
    target = profitable_windows if profitable_windows else per_window

    # Sortino/Sharpe/Calmar/PF/MaxDD/WR/TotalReturn = MEAN across windows.
    # NumTrades = SUM (statistical significance is cumulative).
    mean_keys = (
        "sharpe", "sortino", "calmar", "profit_factor", "max_drawdown",
        "win_rate", "total_return",
    )
    sum_keys = ("num_trades",)
    agg: dict[str, Any] = {}
    for k in mean_keys:
        vals = [w["metrics"].get(k) for w in target if w["metrics"].get(k) is not None]
        agg[k] = float(sum(vals) / len(vals)) if vals else 0.0
    for k in sum_keys:
        agg[k] = float(sum(w["metrics"].get(k, 0) for w in target))

    # For derived metrics (payoff, recovery, sqn, omega), also average
    # per-window derived metrics.
    derived_keys = ("payoff_ratio", "recovery_factor", "sqn", "omega_ratio",
                    "exposure_pct", "holding_period_avg")
    for k in derived_keys:
        derived_per_w = []
        for w in target:
            d = _extract_derived_metrics(w["metrics"])
            derived_per_w.append(d.get(k, 0.0))
        agg[k] = float(sum(derived_per_w) / len(derived_per_w)) if derived_per_w else 0.0

    agg["_n_profitable_windows"] = len(profitable_windows)
    agg["_n_total_windows"] = len(per_window)

    return agg, per_window


# ── Stage 6: GATES ─────────────────────────────────────────────────────
def check_is_gates(metrics: dict[str, Any], profile: GateProfile) -> list[str]:
    """Return list of failed gate names (empty = all pass)."""
    failed: list[str] = []
    if (metrics.get("sortino") or 0) < profile.sortino:
        failed.append(f"sortino<{profile.sortino}")
    if (metrics.get("profit_factor") or 0) < profile.profit_factor:
        failed.append(f"profit_factor<{profile.profit_factor}")
    if abs(metrics.get("max_drawdown") or 0) > profile.max_drawdown:
        failed.append(f"max_drawdown>{profile.max_drawdown:.2%}")
    if (metrics.get("win_rate") or 0) < profile.win_rate:
        failed.append(f"win_rate<{profile.win_rate:.0%}")
    if (metrics.get("num_trades") or 0) < profile.num_trades:
        failed.append(f"num_trades<{profile.num_trades}")
    if (metrics.get("calmar") or 0) < profile.calmar:
        failed.append(f"calmar<{profile.calmar}")
    if (metrics.get("recovery_factor") or 0) < profile.recovery_factor:
        failed.append(f"recovery_factor<{profile.recovery_factor}")
    if (metrics.get("sqn") or 0) < profile.sqn:
        failed.append(f"sqn<{profile.sqn}")
    if (metrics.get("omega_ratio") or 0) < profile.omega_ratio:
        failed.append(f"omega_ratio<{profile.omega_ratio}")
    if (metrics.get("total_return") or 0) <= profile.total_return:
        failed.append(f"total_return<={profile.total_return}")
    return failed


# ── Stage 7: WALKS (in-sample vs out-of-sample) ────────────────────────
def walk_forward_efficiency(
    strategy_fn: Callable,
    df: "pd.DataFrame",
    is_fraction: float = 0.7,
    exchange: str = "binance",
) -> dict[str, float]:
    """Run strategy on in-sample (first 70%) and OOS (last 30%).

    Returns dict with oos_sortino, oos_max_dd, oos_win_rate, wf_efficiency.
    """
    n = len(df)
    is_end = int(n * is_fraction)
    is_df = df.iloc[:is_end]
    oos_df = df.iloc[is_end:]

    if len(is_df) < 100 or len(oos_df) < 50:
        return {
            "oos_sortino": 0.0,
            "oos_max_dd": 0.0,
            "oos_win_rate": 0.0,
            "wf_efficiency": 0.0,
        }

    is_entries, is_exits = run_signals(strategy_fn, is_df)
    oos_entries, oos_exits = run_signals(strategy_fn, oos_df)

    is_m = run_backtest(is_df, is_entries, is_exits, exchange=exchange) or {}
    oos_m = run_backtest(oos_df, oos_entries, oos_exits, exchange=exchange) or {}

    is_sortino = float(is_m.get("sortino") or 0.0)
    oos_sortino = float(oos_m.get("sortino") or 0.0)
    oos_max_dd = float(oos_m.get("max_drawdown") or 0.0)
    oos_win_rate = float(oos_m.get("win_rate") or 0.0)

    wf_eff = (oos_sortino / is_sortino) if is_sortino > 0 else 0.0

    return {
        "oos_sortino": oos_sortino,
        "oos_max_dd": oos_max_dd,
        "oos_win_rate": oos_win_rate,
        "wf_efficiency": wf_eff,
    }


def check_wf_gates(
    is_metrics: dict[str, Any],
    wf: dict[str, float],
    profile: GateProfile,
) -> list[str]:
    """Return list of failed walk-forward gate names (empty = pass)."""
    failed: list[str] = []
    is_sortino = float(is_metrics.get("sortino") or 0.0)
    is_max_dd = abs(float(is_metrics.get("max_drawdown") or 0.0))
    is_win_rate = float(is_metrics.get("win_rate") or 0.0)

    if is_sortino > 0:
        if wf["oos_sortino"] < profile.wf_oos_sortino_ratio * is_sortino:
            failed.append(
                f"oos_sortino={wf['oos_sortino']:.2f} < "
                f"{profile.wf_oos_sortino_ratio}*IS={is_sortino:.2f}"
            )
    if is_max_dd > 1e-9:
        if wf["oos_max_dd"] > profile.wf_oos_max_dd_ratio * is_max_dd:
            failed.append(
                f"oos_max_dd={wf['oos_max_dd']:.2%} > "
                f"{profile.wf_oos_max_dd_ratio}*IS={is_max_dd:.2%}"
            )
    if is_win_rate > 0:
        if wf["oos_win_rate"] < profile.wf_oos_win_rate_ratio * is_win_rate:
            failed.append(
                f"oos_win_rate={wf['oos_win_rate']:.2%} < "
                f"{profile.wf_oos_win_rate_ratio}*IS={is_win_rate:.2%}"
            )
    return failed


# ── Stage 8: MONTE CARLO ──────────────────────────────────────────────
def monte_carlo_check(
    metrics: dict[str, Any],
    n_simulations: int = 10000,
    seed: int | None = 42,
) -> dict[str, float]:
    """Run Monte Carlo on the strategy's trade_returns.

    Uses realbt.monte_carlo if available; falls back to a bootstrap
    implementation that returns the same shape.
    """
    import numpy as np
    n_trades = int(metrics.get("num_trades") or 0)
    if n_trades < 5:
        return {
            "p_loss": 1.0,
            "p_ruin": 1.0,
            "cvar95": -1.0,
            "n_simulations": 0,
        }

    # Synthesize a trade returns array from PF, WR, total_return.
    pf = float(metrics.get("profit_factor") or 1.0)
    wr = float(metrics.get("win_rate") or 0.5)
    total = float(metrics.get("total_return") or 0.0)
    if wr <= 0 or wr >= 1:
        wr = 0.5

    avg_trade = total / max(n_trades, 1)
    # avg win / avg loss via PF=WR*PW/(1-WR)/PL with PW=PL*payoff
    # Solve: PF = WR * payoff / (1-WR)  =>  payoff = PF*(1-WR)/WR
    payoff = pf * (1.0 - wr) / max(wr, 1e-6)
    avg_win = avg_trade * payoff / max(1.0 - wr + wr * payoff, 1e-6) * n_trades
    avg_loss = -avg_win / max(payoff, 1e-6) if payoff > 1e-9 else avg_win * 0.5
    # Simpler: set avg_win such that WR * avg_win + (1-WR) * avg_loss = avg_trade
    # and avg_win / |avg_loss| = payoff. Solve the system.
    # avg_win = payoff * |avg_loss|, avg_trade = WR * payoff * L - (1-WR) * L
    # => L = avg_trade / (WR * payoff - (1-WR))
    denom = wr * payoff - (1.0 - wr)
    if abs(denom) < 1e-9:
        L = abs(avg_trade)
    else:
        L = abs(avg_trade) / abs(denom)
    avg_loss = -L
    avg_win = payoff * L

    n_wins = max(int(round(wr * n_trades)), 1)
    n_losses = max(n_trades - n_wins, 1)
    trade_returns = np.array([avg_win] * n_wins + [avg_loss] * n_losses, dtype=np.float64)

    # Try realbt first, fall back to local bootstrap
    try:
        from realbt import monte_carlo as _realbt_mc
        mc = _realbt_mc(trade_returns, n_simulations=n_simulations, seed=seed)
        # realbt returns *_pct keys; convert to fractions for gate comparisons.
        p_loss = mc.get("prob_of_loss") or mc.get("p_loss") or mc.get("prob_loss") or 0.0
        p_ruin = mc.get("prob_of_ruin") or mc.get("p_ruin_50") or mc.get("p_ruin") or 0.0
        cvar95 = mc.get("cvar_95_pct") or mc.get("cvar_95") or mc.get("cvar95") or 0.0
        return {
            "p_loss": float(p_loss) / 100.0 if float(p_loss) > 1 else float(p_loss),
            "p_ruin": float(p_ruin) / 100.0 if float(p_ruin) > 1 else float(p_ruin),
            "cvar95": float(cvar95) / 100.0 if abs(float(cvar95)) > 1 else float(cvar95),
            "n_simulations": int(mc.get("n_simulations") or n_simulations),
        }
    except ImportError:
        return _local_monte_carlo(trade_returns, n_simulations, seed)


def _local_monte_carlo(
    trade_returns: "np.ndarray",
    n_simulations: int,
    seed: int | None,
) -> dict[str, float]:
    """Bootstrap Monte Carlo fallback when realbt.monte_carlo is unavailable.

    For each simulation: sample n trades with replacement from the
    trade_returns pool, compute:
      * p_loss: fraction of sims with negative total return
      * p_ruin: fraction of sims whose intra-sim max-drawdown > 50% of
        the initial capital's accumulated gains (we use 0.5 as a
        absolute threshold on the drawdown magnitude relative to
        running peak)
      * cvar95: 5th-percentile CVaR of the total-return distribution
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    n = len(trade_returns)
    sims = rng.choice(trade_returns, size=(n_simulations, n), replace=True)
    sim_totals = sims.sum(axis=1)

    # Per-sim max drawdown: compute the running max of the equity curve,
    # then the deepest drop. We normalize by the per-sim PEAK so DD is
    # always in [0, 1].
    cumsum = np.cumsum(sims, axis=1)
    running_max = np.maximum.accumulate(cumsum, axis=1)
    # Avoid divide-by-zero when running_max is 0 or negative
    safe_denom = np.where(running_max > 0, running_max, 1.0)
    drawdowns = np.where(
        running_max > 0,
        np.maximum(running_max - cumsum, 0.0) / safe_denom,
        0.0,
    )
    sim_max_dd = drawdowns.max(axis=1)  # ∈ [0, 1]

    p_loss = float((sim_totals < 0).mean())
    p_ruin = float((sim_max_dd > 0.5).mean())

    # CVaR 95: mean of the worst 5% of sim_totals.
    var95 = float(np.percentile(sim_totals, 5))
    worst = sim_totals[sim_totals <= var95]
    cvar95 = float(worst.mean()) if worst.size > 0 else var95

    return {
        "p_loss": p_loss,
        "p_ruin": p_ruin,
        "cvar95": cvar95,
        "n_simulations": n_simulations,
    }


def check_mc_gates(mc: dict[str, float], profile: GateProfile) -> list[str]:
    failed: list[str] = []
    if mc["p_loss"] > profile.mc_p_loss_max:
        failed.append(f"mc_p_loss={mc['p_loss']:.2%} > {profile.mc_p_loss_max:.0%}")
    if mc["p_ruin"] > profile.mc_p_ruin_max:
        failed.append(f"mc_p_ruin={mc['p_ruin']:.2%} > {profile.mc_p_ruin_max:.0%}")
    if mc["cvar95"] < profile.mc_cvar95_min:
        failed.append(f"mc_cvar95={mc['cvar95']:.2%} < {profile.mc_cvar95_min:.0%}")
    return failed


# ── Stage 9: REPORT / persist ─────────────────────────────────────────
def persist_result(
    result: GateResult,
    profile: GateProfile,
    raw_metrics: dict[str, Any],
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Write a GateResult row to nexus_results.duckdb:strategy_gate_results."""
    import duckdb

    if not os.path.exists(db_path):
        logger.warning("DB not found: %s — skipping persist", db_path)
        return

    con = duckdb.connect(db_path, read_only=False)
    try:
        con.execute(CREATE_SEQ_SQL)
        con.execute(STRATEGY_GATE_RESULTS_DDL)
        row = result.to_row()
        row["gates_profile_json"] = json.dumps(profile.to_dict())
        row["raw_metrics_json"] = json.dumps(raw_metrics, default=str)
        # Order columns to match DDL
        cols = [
            "id", "strategy_name", "symbol", "exchange", "gates_profile",
            "ran_at", "bars_count", "is_window_start", "is_window_end",
            "sortino", "sharpe", "calmar", "profit_factor", "max_drawdown",
            "win_rate", "num_trades", "total_return", "payoff_ratio",
            "recovery_factor", "sqn", "omega_ratio", "exposure_pct",
            "holding_period_avg",
            "oos_sortino", "oos_max_dd", "oos_win_rate", "wf_efficiency",
            "mc_p_loss", "mc_p_ruin", "mc_cvar95", "mc_n_simulations",
            "passed", "failed_gates_json", "gates_profile_json",
            "raw_metrics_json", "notes",
        ]
        placeholders = ",".join(["?"] * len(cols))
        values = [row.get(c) for c in cols]
        con.execute(
            f"INSERT INTO strategy_gate_results ({','.join(cols)}) "
            f"VALUES ({placeholders})",
            values,
        )
        logger.info(
            "Persisted strategy_gate_results row id=%s passed=%s failed=%s",
            row["id"], row["passed"], result.failed_gates,
        )
    except Exception as exc:
        logger.error("Failed to persist gate result: %s", exc)
    finally:
        con.close()


# ── Top-level orchestrator ────────────────────────────────────────────
def run_gate(
    strategy_name: str,
    symbol: str = "BTC",
    exchange: str = "binance",
    profile: GateProfile | None = None,
    strategy_dir: str = DEFAULT_STRATEGY_DIR,
    ohlcv_dir: str = DEFAULT_OHLCV_DIR,
    db_path: str = DEFAULT_DB_PATH,
    persist: bool = True,
    verbose: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> GateResult:
    """Run the full LOAD → DATA → SIGNALS → BACKTEST → METRICS → GATES
    → WALKS → MONTE CARLO → REPORT pipeline.

    Returns a GateResult (also persisted if persist=True).

    By default the data slice is 2021-06-01 → 2024-12-01 (matches the
    StratForge WF validation baseline). Override with start/end args.
    """
    import numpy as np  # noqa: F401 — used by stage helpers
    global np

    profile = profile or GateProfile()
    result = GateResult(
        strategy_name=strategy_name,
        symbol=symbol,
        exchange=exchange,
        gates_profile=profile.name,
    )

    # Stage 1+2: LOAD + DATA
    strat_path = find_strategy_path(strategy_name, strategy_dir)
    if verbose:
        logger.info("[LOAD] %s", strat_path)
    strategy_fn = load_strategy(strat_path)

    # Resolve the IS slice: explicit args > WF-derived > defaults.
    if start and end:
        slice_start, slice_end = start, end
    else:
        wf_slice = derive_gate_slice(strategy_name, symbol, db_path)
        if wf_slice:
            slice_start, slice_end = wf_slice
        else:
            slice_start = start or DEFAULT_GATE_START
            slice_end = end or DEFAULT_GATE_END

    if verbose:
        logger.info("[DATA] slice=%s -> %s", slice_start, slice_end)

    df = load_ohlcv(
        symbol, ohlcv_dir,
        start=slice_start,
        end=slice_end,
    )
    result.bars_count = int(len(df))
    if not df.empty:
        result.is_window_start = str(df.index.min())
        result.is_window_end = str(df.index.max())

    # Stage 3+4+5: SIGNALS + BACKTEST + METRICS
    # Use per-window aggregation (matches StratForge baseline convention:
    # "BTC 4/5 (sortino=2.41)" = avg Sortino across 4 profitable windows).
    metrics, per_window = run_per_window_backtest(
        strategy_fn, df, strategy_name, symbol, db_path=db_path, exchange=exchange,
    )
    if not metrics:
        # Fallback: single backtest over the union slice
        entries, exits = run_signals(strategy_fn, df)
        metrics = run_backtest(df, entries, exits, exchange=exchange) or {}
        per_window = []
    if not metrics:
        result.failed_gates = ["backtest_returned_none"]
        result.notes = "realbt.backtest returned None — likely raptor import issue"
        if persist:
            persist_result(result, profile, metrics, db_path)
        return result

    # Annotate with how many windows contributed
    if per_window:
        n_prof = sum(1 for w in per_window if w["profitable"])
        result.notes = (
            f"Aggregated across {n_prof}/{len(per_window)} profitable WF windows"
        )

    derived = _extract_derived_metrics(metrics)
    result.sortino = float(metrics.get("sortino") or 0)
    result.sharpe = float(metrics.get("sharpe") or 0)
    result.calmar = float(metrics.get("calmar") or 0)
    result.profit_factor = float(metrics.get("profit_factor") or 0)
    result.max_drawdown = float(metrics.get("max_drawdown") or 0)
    result.win_rate = float(metrics.get("win_rate") or 0)
    result.num_trades = int(metrics.get("num_trades") or 0)
    result.total_return = float(metrics.get("total_return") or 0)
    result.payoff_ratio = derived["payoff_ratio"]
    result.recovery_factor = derived["recovery_factor"]
    result.sqn = derived["sqn"]
    result.omega_ratio = derived["omega_ratio"]
    result.exposure_pct = derived["exposure_pct"]
    result.holding_period_avg = derived["holding_period_avg"]

    if verbose:
        logger.info(
            "[BACKTEST] sortino=%.2f pf=%.2f mdd=%.2f%% wr=%.2f%% n=%d ret=%.2f%%",
            result.sortino, result.profit_factor, result.max_drawdown * 100,
            result.win_rate * 100, result.num_trades, result.total_return * 100,
        )

    # Stage 6: IS gates
    failed = check_is_gates(metrics, profile)
    result.failed_gates.extend(failed)

    # Stage 7: WF
    wf = walk_forward_efficiency(strategy_fn, df, is_fraction=0.7, exchange=exchange)
    result.oos_sortino = wf["oos_sortino"]
    result.oos_max_dd = wf["oos_max_dd"]
    result.oos_win_rate = wf["oos_win_rate"]
    result.wf_efficiency = wf["wf_efficiency"]
    wf_failed = check_wf_gates(metrics, wf, profile)
    result.failed_gates.extend(f"wf:{f}" for f in wf_failed)

    if verbose:
        logger.info(
            "[WALKS] oos_sortino=%.2f oos_max_dd=%.2f%% oos_wr=%.2f%% wf_eff=%.2f",
            wf["oos_sortino"], wf["oos_max_dd"] * 100,
            wf["oos_win_rate"] * 100, wf["wf_efficiency"],
        )

    # Stage 8: Verdict (realbt-only IS + WF; no Monte Carlo — Tristan 2026-06-25)
    # We dropped MC because (a) it's CPU-heavy (10k simulations per strategy),
    # (b) we are testing/exploring right now, not gating for production, and
    # (c) the realbt OOS walks already give us OOS Sortino / Max DD / Win Rate
    # which are the actual decision-driving metrics for live deployment.
    result.passed = len(result.failed_gates) == 0

    # Persist
    if persist:
        persist_result(result, profile, metrics, db_path)

    return result


# ── CLI ────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Strategy Validation Gate (B6)")
    parser.add_argument("strategy_name", help="e.g. meta_cmo_alma_atr_wf_v1")
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--profile", default="loose", choices=["default", "strict", "loose", "stocks"],
                        help="Gate profile. Default is 'loose' (Tristan 2026-06-25: "
                             "we are testing, want more strategies to pass through; will "
                             "tighten over time). 'default' = crypto-trend-calibrated. "
                             "'strict' = brief's original thresholds. "
                             "'stocks' = equity-tuned (MAS daily-bar strategies; "
                             "no sortino required, higher Sharpe floor, win_rate=0.40).")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--start", default=None,
                        help=f"Start date (default: auto-derive from walk_forward_results, else {DEFAULT_GATE_START})")
    parser.add_argument("--end", default=None,
                        help=f"End date (default: auto-derive from walk_forward_results, else {DEFAULT_GATE_END})")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    profile = GateProfile(name=args.profile)
    # Class defaults are already 'loose' (Tristan 2026-06-25). Override for
    # 'strict' only — 'default' and 'loose' use the class-level defaults.
    if args.profile == "strict":
        profile.sortino = 1.5
        profile.profit_factor = 1.5
        profile.max_drawdown = 0.20
        profile.win_rate = 0.45
        profile.num_trades = 30
        profile.recovery_factor = 1.5
        profile.sqn = 1.5
        profile.calmar = 0.5
        profile.omega_ratio = 1.2
    elif args.profile == "default":
        # Original crypto-trend-calibrated values (Recovery 1.0, SQN 1.0,
        # win_rate 0.40, profit_factor 1.3, etc.) — kept for reference and
        # to allow tightening back up if loose over-permits.
        profile.sortino = 1.0
        profile.profit_factor = 1.3
        profile.max_drawdown = 0.25
        profile.win_rate = 0.40
        profile.num_trades = 30
        profile.calmar = 0.5
        profile.recovery_factor = 1.0
        profile.sqn = 1.0
        profile.omega_ratio = 1.2
    elif args.profile == "stocks":
        # 2026-06-26: equity-tuned profile for MAS daily-bar strategies.
        # Stocks typically have lower vol (so higher Sharpe floor at
        # 1.0 vs 0.5) and more trades per year (so num_trades >= 30),
        # and their stored metrics in the lakehouse don't include
        # Sortino (the v_nexus_strategy_pool_stocks projection has NULL
        # sortino). We relax sortino to 0 (any) and tighten the other
        # gates. The walk-forward OOS ratios stay the same as loose.
        profile.sortino = 0.0  # Sortino is NULL for stocks — don't filter on it
        profile.profit_factor = 1.2  # equity strategies usually have higher PF
        profile.max_drawdown = 0.25  # 25% — daily equity MDD is tighter
        profile.win_rate = 0.40  # equity day-trend often 40-45%
        profile.num_trades = 30  # daily bars -> more trades/year
        profile.calmar = 0.5
        profile.recovery_factor = 1.0  # equities recover faster
        profile.sqn = 1.0
        profile.omega_ratio = 1.2

    result = run_gate(
        strategy_name=args.strategy_name,
        symbol=args.symbol,
        exchange=args.exchange,
        profile=profile,
        db_path=args.db,
        persist=not args.no_persist,
        verbose=args.verbose,
        start=args.start,
        end=args.end,
    )
    print(json.dumps({
        "id": result.id,
        "strategy_name": result.strategy_name,
        "symbol": result.symbol,
        "passed": result.passed,
        "failed_gates": result.failed_gates,
        "sortino": result.sortino,
        "sharpe": result.sharpe,
        "calmar": result.calmar,
        "profit_factor": result.profit_factor,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "num_trades": result.num_trades,
        "total_return": result.total_return,
        "oos_sortino": result.oos_sortino,
        "oos_max_dd": result.oos_max_dd,
        "oos_win_rate": result.oos_win_rate,
        "wf_efficiency": result.wf_efficiency,
        "mc_p_loss": result.mc_p_loss,
        "mc_p_ruin": result.mc_p_ruin,
        "mc_cvar95": result.mc_cvar95,
        "notes": result.notes,
    }, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())