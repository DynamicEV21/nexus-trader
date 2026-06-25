"""
Counterfactual Replay Runner — B3 (2026-06-25)
===============================================

For every bar where the Nexus Committee chose HOLD (or where a candidate
strategy was not selected), replay the regime-suggested strategies forward
N bars and persist the realized outcome to
``nexus_results.duckdb.counterfactual_outcomes``.

Why this exists
---------------
The committee picks ONE strategy per bar. The other regime-suggested
strategies are "shadow" — they would have been chosen if their metrics
were better, but we have no record of their ACTUAL forward performance.

Without counterfactual data, the lakehouse's regime→strategy map is
purely historical (computed once at backtest time). With counterfactual
data, we can:

* Refresh the regime→strategy map each bar based on what WOULD have
  worked in the recent regime.
* Detect when a strategy's edge decays ("this strategy used to work in
  TRENDING_UP but stopped after bar T").
* Train downstream models (B5 lakehouse_strategy_for_regime) on
  counterfactual success rather than just historical win rate.

Design
------
We don't have a log of HOLD decisions yet (B1's attribution loop is
the first to write HOLD outcomes). For B3's first implementation we
generate a synthetic HOLD-bar set by:
  1. Reading ``nexus_lumibot_results`` to find backtest periods where
     the committee would have been live.
  2. Generating one synthetic HOLD bar per strategy-window-test_start.
  3. For each HOLD bar, looking up the top-K regime-suggested crypto
     strategies for the bar's symbol (from
     ``v_nexus_strategy_pool`` ordered by Sortino).
  4. Replaying each strategy's signal forward 50 bars using
     ``walk_forward_results`` statistics (avg_return_pct, win_rate,
     sharpe, num_entries) — an O(1) approximation rather than a
     full backtest (which would take minutes per bar).
  5. Persisting one row per (hold_bar, strategy) pair.

The replay math
---------------
For each strategy we have (avg trade return, win rate, num trades)
from ``walk_forward_results``. Forward 50 bars at the strategy's
cadence (~1 trade per 7-14 bars based on num_entries/window_length)
yields approximately::

    forward_n_trades = num_entries_per_bar * 50
    expected_return = avg_trade_return * forward_n_trades
    realized_sharpe = sharpe * sqrt(forward_n_trades / num_entries_total)
    realized_max_dd = max_drawdown * sqrt(forward_n_trades / num_entries_total)

These are approximations; the runner notes ``method='aggregate'`` so
downstream consumers know this is fast approximate replay, not full
backtest. A future ``method='full'`` path could invoke raptorbt for
exact replay.

Usage
-----
CLI: ``python -m src.runners.counterfactual_replay --symbols BTC,ETH,SOL``
API: ``from src.runners.counterfactual_replay import run_replay``
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_FORWARD_BARS = 50
DEFAULT_TOP_K_STRATEGIES = 3
DEFAULT_LOOKBACK_DAYS = 365  # how far back to scan for HOLD bars
DEFAULT_DB_PATH = os.path.expanduser(
    os.environ.get(
        "NEXUS_RESULTS_DB",
        "~/development/nexus-trade/data/nexus_results.duckdb",
    )
)
DEFAULT_LAKEHOUSE_DB = os.path.expanduser(
    os.environ.get(
        "NEXUS_LAKEHOUSE_PATH",
        "~/development/agentic-quant-os/data/quant.duckdb",
    )
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class HoldBar:
    """A bar where the committee chose HOLD (or, in synthetic mode, a
    candidate bar we want to test the alternatives against."""

    symbol: str
    bar_date: date
    regime: str = "unknown"
    committee_decision: str = "hold"  # 'hold' for synthetic bars
    committee_strategy: str = ""  # what the committee actually used
    bar_index: int = 0  # 0-based index within the backtest period


@dataclass
class StrategyMetrics:
    """Cached metrics for a strategy (used in fast forward-replay).

    These come from ``walk_forward_results`` aggregate (mean over windows)
    and are used to estimate forward-N-bar performance without running
    a full backtest.
    """

    strategy_name: str
    symbol: str
    avg_total_return_pct: float
    avg_sharpe: float
    avg_sortino: float
    avg_max_drawdown_pct: float
    avg_num_entries: float
    avg_window_days: float
    win_rate: float  # 0..1 — derived from profitable ratio
    n_windows: int

    def replay_forward(self, n_bars: int = DEFAULT_FORWARD_BARS) -> dict[str, float]:
        """Estimate forward-N-bar performance from cached metrics.

        Trade-frequency assumption: strategies average one trade per
        ``avg_window_days / avg_num_entries`` bars. Forward N bars gives
        ``n_bars / (avg_window_days / avg_num_entries)`` trades.
        """
        if self.avg_num_entries <= 0 or self.avg_window_days <= 0:
            return {
                "forward_return_pct": 0.0,
                "forward_sharpe": 0.0,
                "forward_max_dd_pct": 0.0,
                "forward_num_trades": 0,
                "forward_win_rate": self.win_rate,
            }
        bars_per_trade = max(self.avg_window_days / self.avg_num_entries, 1.0)
        fwd_trades = max(n_bars / bars_per_trade, 0.0)
        # Expected return scales linearly with number of trades.
        fwd_return = self.avg_total_return_pct * (fwd_trades / max(self.avg_num_entries, 1))
        # Sharpe scales with sqrt(time).
        fwd_sharpe = self.avg_sharpe * math.sqrt(
            fwd_trades / max(self.avg_num_entries, 1)
        )
        # Max drawdown scales sub-linearly (sqrt).
        fwd_dd = self.avg_max_drawdown_pct * math.sqrt(
            fwd_trades / max(self.avg_num_entries, 1)
        )
        return {
            "forward_return_pct": fwd_return,
            "forward_sharpe": fwd_sharpe,
            "forward_max_dd_pct": fwd_dd,
            "forward_num_trades": int(round(fwd_trades)),
            "forward_win_rate": self.win_rate,
        }


@dataclass
class CounterfactualOutcome:
    """One row to persist to ``counterfactual_outcomes``."""

    symbol: str
    hold_bar_date: date
    hold_regime: str
    strategy_name: str
    strategy_rank: int  # 1=top regime-recommended, 2=second, etc.
    forward_return_pct: float
    forward_sharpe: float
    forward_sortino: float
    forward_max_dd_pct: float
    forward_num_trades: int
    forward_win_rate: float
    method: str = "aggregate"  # 'aggregate' = fast O(1) replay
    bar_index: int = 0
    notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------
COUNTERFACTUAL_OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS counterfactual_outcomes (
    id INTEGER PRIMARY KEY DEFAULT nextval('nexus_seq'),
    symbol VARCHAR NOT NULL,
    hold_bar_date DATE NOT NULL,
    hold_regime VARCHAR,
    strategy_name VARCHAR NOT NULL,
    strategy_rank INTEGER NOT NULL,
    forward_return_pct DOUBLE,
    forward_sharpe DOUBLE,
    forward_sortino DOUBLE,
    forward_max_dd_pct DOUBLE,
    forward_num_trades INTEGER,
    forward_win_rate DOUBLE,
    method VARCHAR,
    bar_index INTEGER DEFAULT 0,
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp
)
"""


def _ensure_table(con: Any) -> None:
    """Create the counterfactual_outcomes table if it doesn't exist."""
    try:
        con.execute(COUNTERFACTUAL_OUTCOMES_DDL)
        logger.debug("counterfactual_outcomes table ensured")
    except Exception as exc:
        logger.warning("Could not create counterfactual_outcomes table: %s", exc)


def _open_con(db_path: str, read_only: bool = False):
    """Open a DuckDB connection with the nexus_seq sequence ensured."""
    import duckdb
    con = duckdb.connect(db_path, read_only=read_only)
    # nexus_seq is needed for the DEFAULT nextval() above. Create-if-missing.
    try:
        con.execute("CREATE SEQUENCE IF NOT EXISTS nexus_seq START 1")
    except Exception:
        pass
    return con


# ---------------------------------------------------------------------------
# HOLD-bar synthesis
# ---------------------------------------------------------------------------
def _build_synthetic_hold_bars(
    con: Any,
    symbols: Iterable[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[HoldBar]:
    """Generate a synthetic HOLD-bar set from ``nexus_lumibot_results``.

    For each (strategy, symbol, test_start) we treat the bar at test_start
    as a "HOLD" bar where the committee did NOT choose that strategy (it
    could have). This gives us a meaningful set of counterfactual queries
    without requiring a real HOLD-decision log.

    Args:
        con: Open DuckDB connection to nexus_results.duckdb.
        symbols: Symbols to include.
        lookback_days: Skip HOLD bars older than this many days.

    Returns:
        list of HoldBar objects (typically thousands of bars).
    """
    syms = list(symbols)
    sym_filter = ",".join([f"'{s}'" for s in syms]) or "'BTC','ETH','SOL'"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()
    rows = con.execute(
        f"""
        SELECT strategy_name, symbol, test_start, num_entries
        FROM walk_forward_results
        WHERE symbol IN ({sym_filter})
          AND test_start IS NOT NULL
          AND test_start >= ?
        ORDER BY test_start DESC
        """,
        [cutoff],
    ).fetchall()
    out: list[HoldBar] = []
    for i, r in enumerate(rows):
        strat, sym, ts, num_entries = r
        if ts is None:
            continue
        out.append(
            HoldBar(
                symbol=sym,
                bar_date=ts,
                regime="unknown",
                committee_decision="hold",
                committee_strategy=strat,
                bar_index=i,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Strategy metrics lookup
# ---------------------------------------------------------------------------
def _build_strategy_metrics(
    lakehouse_con: Any,
    symbols: Iterable[str],
    min_sortino: float = 0.0,
) -> dict[tuple[str, str], StrategyMetrics]:
    """Read aggregate backtest metrics per (strategy, symbol) from
    ``backtest_results_v2`` in quant.duckdb.

    The ``backtest_results_v2`` table holds the AQOS backtest run results
    — same source as ``v_nexus_strategy_pool`` but with finer-grained
    per-row metrics. We aggregate over all rows per (strategy, symbol)
    pair and use the means as the cached metrics for fast forward-replay.

    Returns a dict keyed by (strategy_name, symbol). Strategies with no
    backtest runs are excluded.
    """
    syms = list(symbols)
    sym_filter = ",".join([f"'{s}'" for s in syms]) or "'BTC','ETH','SOL'"
    try:
        rows = lakehouse_con.execute(
            f"""
            SELECT
                strategy_name,
                ticker AS symbol,
                AVG(total_return) AS avg_return,
                AVG(sharpe) AS avg_sharpe,
                AVG(sortino) AS avg_sortino,
                AVG(ABS(max_drawdown)) AS avg_max_dd,
                AVG(num_trades) AS avg_trades,
                AVG(win_rate) AS win_rate,
                COUNT(*) AS n_runs
            FROM backtest_results_v2
            WHERE ticker IN ({sym_filter})
              AND status IN ('winner','tested')
            GROUP BY strategy_name, ticker
            HAVING COUNT(*) >= 1 AND AVG(num_trades) > 0
            """,
        ).fetchall()
    except Exception as exc:
        logger.warning("Could not read backtest_results_v2 (%s); falling back to empty", exc)
        return {}

    out: dict[tuple[str, str], StrategyMetrics] = {}
    for r in rows:
        strat, sym, avg_ret, avg_sh, avg_so, avg_dd, avg_n, win, n = r
        if min_sortino > 0 and (avg_so is None or avg_so < min_sortino):
            continue
        # backtest_results_v2 doesn't carry avg_window_days; default to 180
        # (typical AQOS backtest length) so the bars_per_trade calc makes
        # sense.
        out[(strat, sym)] = StrategyMetrics(
            strategy_name=strat,
            symbol=sym,
            avg_total_return_pct=float(avg_ret or 0.0) * 100.0,
            avg_sharpe=float(avg_sh or 0.0),
            avg_sortino=float(avg_so or 0.0),
            avg_max_drawdown_pct=float(avg_dd or 0.0) * 100.0,
            avg_num_entries=float(avg_n or 0.0),
            avg_window_days=180.0,
            win_rate=float(win or 0.0),
            n_windows=int(n or 0),
        )
    return out


def _top_regime_strategies(
    metrics: dict[tuple[str, str], StrategyMetrics],
    symbol: str,
    regime: str,
    top_k: int = DEFAULT_TOP_K_STRATEGIES,
) -> list[StrategyMetrics]:
    """Return top-K strategies for (symbol, regime) ordered by Sortino.

    Currently the regime→strategy mapping is approximated by sorting all
    strategies for the symbol by Sortino. A future enhancement could
    use ``v_nexus_regime_strategy_map`` to filter by regime.
    """
    candidates = [m for (s, sym), m in metrics.items() if sym == symbol]
    candidates.sort(key=lambda m: m.avg_sortino, reverse=True)
    return candidates[:top_k]


# ---------------------------------------------------------------------------
# Main replay pipeline
# ---------------------------------------------------------------------------
def run_replay(
    symbols: Iterable[str] | None = None,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    forward_bars: int = DEFAULT_FORWARD_BARS,
    top_k: int = DEFAULT_TOP_K_STRATEGIES,
    db_path: str = DEFAULT_DB_PATH,
    lakehouse_db_path: str = DEFAULT_LAKEHOUSE_DB,
    write: bool = True,
) -> dict[str, Any]:
    """Run the counterfactual replay pipeline end-to-end.

    Args:
        symbols: Symbols to replay. Defaults to ['BTC', 'ETH', 'SOL'].
        lookback_days: Skip HOLD bars older than this.
        forward_bars: Forward window size in bars.
        top_k: Number of regime-recommended strategies to replay per bar.
        db_path: Path to nexus_results.duckdb (write target).
        lakehouse_db_path: Path to quant.duckdb (read source for metrics).
        write: If True, persist results to counterfactual_outcomes table.

    Returns:
        dict with summary stats: hold_bars_count, outcomes_count, per-symbol breakdown.
    """
    syms = list(symbols) if symbols else ["BTC", "ETH", "SOL"]
    logger.info(
        "Counterfactual replay: symbols=%s, lookback=%dd, fwd_bars=%d, top_k=%d, write=%s",
        syms, lookback_days, forward_bars, top_k, write,
    )

    if not os.path.exists(db_path):
        logger.error("Database not found: %s", db_path)
        return {"ok": False, "error": f"db not found: {db_path}"}
    if not os.path.exists(lakehouse_db_path):
        logger.warning(
            "Lakehouse DB not found at %s — using only walk_forward_results from %s",
            lakehouse_db_path, db_path,
        )

    # Open both connections
    con = _open_con(db_path, read_only=False)
    _ensure_table(con)
    lakehouse_con = _open_con(lakehouse_db_path, read_only=True)

    try:
        # 1) Build synthetic HOLD bars
        hold_bars = _build_synthetic_hold_bars(con, syms, lookback_days)
        logger.info("Generated %d synthetic HOLD bars", len(hold_bars))

        # 2) Build strategy metrics lookup
        metrics = _build_strategy_metrics(lakehouse_con, syms, min_sortino=0.0)
        logger.info("Loaded metrics for %d (strategy, symbol) pairs", len(metrics))

        # 3) For each HOLD bar, replay top-K strategies
        outcomes: list[CounterfactualOutcome] = []
        per_symbol: dict[str, int] = {}
        per_strategy: dict[str, int] = {}
        for hb in hold_bars:
            candidates = _top_regime_strategies(metrics, hb.symbol, hb.regime, top_k=top_k)
            for rank, sm in enumerate(candidates, start=1):
                fwd = sm.replay_forward(forward_bars)
                outcome = CounterfactualOutcome(
                    symbol=hb.symbol,
                    hold_bar_date=hb.bar_date,
                    hold_regime=hb.regime,
                    strategy_name=sm.strategy_name,
                    strategy_rank=rank,
                    forward_return_pct=fwd["forward_return_pct"],
                    forward_sharpe=fwd["forward_sharpe"],
                    forward_sortino=sm.avg_sortino * math.sqrt(
                        fwd["forward_num_trades"] / max(sm.avg_num_entries, 1)
                    ),
                    forward_max_dd_pct=fwd["forward_max_dd_pct"],
                    forward_num_trades=fwd["forward_num_trades"],
                    forward_win_rate=fwd["forward_win_rate"],
                    method="aggregate",
                    bar_index=hb.bar_index,
                    notes=(
                        f"committee_strategy={hb.committee_strategy};"
                        f" sm_n_windows={sm.n_windows}"
                    ),
                )
                outcomes.append(outcome)
                per_symbol[hb.symbol] = per_symbol.get(hb.symbol, 0) + 1
                per_strategy[sm.strategy_name] = per_strategy.get(sm.strategy_name, 0) + 1

        # 4) Persist
        if write and outcomes:
            try:
                con.executemany(
                    """
                    INSERT INTO counterfactual_outcomes
                        (symbol, hold_bar_date, hold_regime, strategy_name,
                         strategy_rank, forward_return_pct, forward_sharpe,
                         forward_sortino, forward_max_dd_pct, forward_num_trades,
                         forward_win_rate, method, bar_index, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            o.symbol, o.hold_bar_date, o.hold_regime, o.strategy_name,
                            o.strategy_rank, o.forward_return_pct, o.forward_sharpe,
                            o.forward_sortino, o.forward_max_dd_pct,
                            o.forward_num_trades, o.forward_win_rate,
                            o.method, o.bar_index, o.notes,
                        )
                        for o in outcomes
                    ],
                )
                logger.info("Persisted %d counterfactual outcomes", len(outcomes))
            except Exception as exc:
                logger.warning("Failed to persist outcomes: %s", exc)

        return {
            "ok": True,
            "hold_bars_count": len(hold_bars),
            "outcomes_count": len(outcomes),
            "per_symbol": per_symbol,
            "per_strategy": per_strategy,
            "method": "aggregate",
            "forward_bars": forward_bars,
            "top_k": top_k,
        }
    finally:
        con.close()
        lakehouse_con.close()


# ---------------------------------------------------------------------------
# Query helper
# ---------------------------------------------------------------------------
def query_counterfactual_outcomes(
    symbol: str = "",
    strategy_name: str = "",
    limit: int = 50,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Read recent counterfactual outcomes (used by ``query_counterfactuals``
    tool and the closed-loop view).

    Args:
        symbol: Filter by symbol (blank = all).
        strategy_name: Filter by strategy (blank = all).
        limit: Max rows to return (most recent first).
        db_path: Path to nexus_results.duckdb.

    Returns:
        list of dict rows.
    """
    if not os.path.exists(db_path):
        return []
    con = _open_con(db_path, read_only=True)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if strategy_name:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = con.execute(
            f"""
            SELECT symbol, hold_bar_date, hold_regime, strategy_name,
                   strategy_rank, forward_return_pct, forward_sharpe,
                   forward_sortino, forward_max_dd_pct, forward_num_trades,
                   forward_win_rate, method, bar_index, notes, created_at
            FROM counterfactual_outcomes
            {where}
            ORDER BY created_at DESC, hold_bar_date DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        keys = (
            "symbol", "hold_bar_date", "hold_regime", "strategy_name",
            "strategy_rank", "forward_return_pct", "forward_sharpe",
            "forward_sortino", "forward_max_dd_pct", "forward_num_trades",
            "forward_win_rate", "method", "bar_index", "notes", "created_at",
        )
        return [dict(zip(keys, r)) for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Counterfactual replay runner (B3)",
    )
    parser.add_argument(
        "--symbols", default="BTC,ETH,SOL",
        help="Comma-separated symbols (default: BTC,ETH,SOL)",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback window in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--forward-bars", type=int, default=DEFAULT_FORWARD_BARS,
        help=f"Forward window in bars (default: {DEFAULT_FORWARD_BARS})",
    )
    parser.add_argument(
        "--top-k", type=int, default=DEFAULT_TOP_K_STRATEGIES,
        help=f"Top-K regime-suggested strategies (default: {DEFAULT_TOP_K_STRATEGIES})",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH,
        help="nexus_results.duckdb path",
    )
    parser.add_argument(
        "--lakehouse-db", default=DEFAULT_LAKEHOUSE_DB,
        help="quant.duckdb path (for metrics lookup)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute but don't write to counterfactual_outcomes",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = run_replay(
        symbols=symbols,
        lookback_days=args.lookback_days,
        forward_bars=args.forward_bars,
        top_k=args.top_k,
        db_path=args.db,
        lakehouse_db_path=args.lakehouse_db,
        write=not args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())