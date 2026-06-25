"""
Post-Backtest Sync — Project LumiBot parquet artifacts into nexus_results.duckdb
=================================================================================

After every LumiBot backtest, flat parquet files are written into ``logs/``.
This module reads those artifacts and projects them into 3 queryable DuckDB
tables for cross-run analysis and cross-agent consumption.

Tables created
--------------
* ``backtest_runs``     — one row per backtest run (summary stats + metadata)
* ``backtest_trades``   — one row per trade event
* ``agent_observations`` — one row per AI agent call (observability)

All inserts are **idempotent** — re-running sync on the same backtest directory
will INSERT OR REPLACE, not duplicate.

Usage
-----
    from src.runners.post_backtest_sync import sync_backtest_results
    summary = sync_backtest_results(Path("logs/NexusCommitteeStrategy_2026-06-23_11-59_q3d6Ko_stats.parquet"))

Or auto-discover the latest run:

    from src.runners.post_backtest_sync import sync_latest
    summary = sync_latest()
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.environ.get(
    "NEXUS_PROJECT_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
))
_LOGS_DIR = _PROJECT_ROOT / "logs"
_DUCKDB_PATH = _PROJECT_ROOT / "data" / "nexus_results.duckdb"

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id           VARCHAR PRIMARY KEY,
    strategy_name    VARCHAR,
    backtest_start   TIMESTAMP,
    backtest_end     TIMESTAMP,
    budget           DOUBLE,
    final_value      DOUBLE,
    total_return     DOUBLE,
    max_drawdown     DOUBLE,
    sharpe           DOUBLE,
    sortino          DOUBLE,
    run_timestamp    TIMESTAMP,
    settings_json    VARCHAR,
    source_file      VARCHAR
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id           VARCHAR,
    event_timestamp  TIMESTAMP,
    symbol           VARCHAR,
    side             VARCHAR,
    filled_quantity  DOUBLE,
    price            DOUBLE,
    trade_cost       DOUBLE,
    asset_type       VARCHAR,
    event_kind       VARCHAR
);

CREATE TABLE IF NOT EXISTS agent_observations (
    run_id           VARCHAR,
    agent_name       VARCHAR,
    call_index       INTEGER,
    timestamp        TIMESTAMP,
    model            VARCHAR,
    summary          TEXT,
    tool_sequence    TEXT,
    tool_call_count  INTEGER,
    call_total_tokens INTEGER,
    call_latency_ms  INTEGER,
    context_text     TEXT,
    thinking_text    TEXT,
    warning_messages TEXT
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# File naming pattern: {StrategyName}_{YYYY-MM-DD}_{HH-MM}_{6char}_{artifact}.ext
_RUN_ID_RE = re.compile(
    r"^(?P<strategy>.+?)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2})_(?P<tag>[A-Za-z0-9]{6})_"
)


def _parse_run_id(filename: str) -> str:
    """Extract the run_id prefix from a LumiBot artifact filename.

    Example: "NexusCommitteeStrategy_2026-06-23_11-59_q3d6Ko_stats.parquet"
             → "NexusCommitteeStrategy_2026-06-23_11-59_q3d6Ko"
    """
    m = _RUN_ID_RE.match(filename)
    if m:
        return f"{m.group('strategy')}_{m.group('date')}_{m.group('time')}_{m.group('tag')}"
    # Fallback: strip known suffixes
    for suffix in ("_stats", "_trades", "_trade_events", "_agent_detail",
                   "_indicators", "_tearsheet", "_tearsheet_metrics",
                   "_settings", "_logs"):
        if suffix in filename:
            return filename.split(suffix)[0]
    return filename.rsplit(".", 1)[0]


def _find_artifact(logs_dir: Path, run_id: str, artifact: str) -> Path | None:
    """Find a specific artifact file for a given run_id."""
    pattern = f"{run_id}_{artifact}"
    matches = list(logs_dir.glob(f"{pattern}.*"))
    # Prefer .parquet over .csv
    for m in matches:
        if m.suffix == ".parquet":
            return m
    return matches[0] if matches else None


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convert to float, returning default on failure."""
    try:
        if val is None or (isinstance(val, float) and val != val):  # NaN check
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def ensure_schema(db_path: Path | None = None) -> None:
    """Create the 3 tables if they don't exist."""
    import duckdb
    path = db_path or _DUCKDB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(_SCHEMA_SQL)
        logger.info("Schema ensured in %s", path)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Sync functions
# ---------------------------------------------------------------------------

def _sync_stats(con, logs_dir: Path, run_id: str) -> int:
    """Read stats.parquet → insert into backtest_runs. Returns rows inserted."""
    stats_path = _find_artifact(logs_dir, run_id, "stats")
    if stats_path is None:
        logger.warning("No stats artifact for run %s", run_id)
        return 0

    import pandas as pd
    df = pd.read_parquet(stats_path)
    if df.empty:
        logger.warning("Stats parquet is empty for run %s", run_id)
        return 0

    # Compute summary metrics from the per-timestep stats
    final_row = df.iloc[-1]
    final_value = _safe_float(final_row.get("portfolio_value"))
    budget = _safe_float(final_row.get("cash_deposits_total"), 10000.0)
    if budget == 0:
        budget = 10000.0  # Default if no deposits recorded

    total_return = _safe_float(final_row.get("return"))
    # If return column is NaN/zero, compute from portfolio values
    if total_return == 0 and final_value > 0 and budget > 0:
        total_return = (final_value - budget) / budget

    # Compute max drawdown from portfolio_value column
    pv = df["portfolio_value"].dropna()
    max_dd = 0.0
    if len(pv) > 1:
        peak = pv.expanding().max()
        drawdown = (pv - peak) / peak
        max_dd = _safe_float(drawdown.min(), 0.0)

    # Compute Sharpe + Sortino from per-bar returns.
    # Sortino is the primary ranking metric (penalizes only downside volatility,
    # which is the risk that matters to a long-biased crypto book). MAR (minimum
    # acceptable return) is 0 — we don't subtract a risk-free rate for per-bar
    # returns because (a) the per-bar series already represents *excess*
    # portfolio returns, and (b) crypto risk-free rate ≈ 0 for a daily horizon.
    returns = df["return"].dropna()
    returns = returns[returns != 0]
    sharpe = 0.0
    sortino = 0.0
    # Annualization factor: assume 6 bars/day (4h), 365 days = 2190 bars/yr.
    # This matches the regime-strategy-map and backtest_results_v2 tables.
    bars_per_year = 2190
    if len(returns) > 2:
        mean_ret = _safe_float(returns.mean())
        std_ret = _safe_float(returns.std())
        if std_ret > 0:
            sharpe = _safe_float(mean_ret / std_ret * (bars_per_year ** 0.5))
        # Sortino: downside-only deviation (returns < 0), MAR = 0.
        # Downside deviation = sqrt(mean(min(returns - MAR, 0)^2))
        # With MAR=0 this simplifies to sqrt(mean(returns < 0)^2)).
        downside = returns[returns < 0]
        if len(downside) > 1:
            downside_dev = _safe_float(downside.std())
            if downside_dev > 0:
                sortino = _safe_float(mean_ret / downside_dev * (bars_per_year ** 0.5))

    # Read settings.json for metadata
    settings_path = _find_artifact(logs_dir, run_id, "settings")
    settings_json = "{}"
    backtest_start = None
    backtest_end = None
    if settings_path:
        try:
            with open(settings_path) as f:
                settings = json.load(f)
            settings_json = json.dumps(settings, default=str)
            bs = settings.get("backtesting_start")
            be = settings.get("backtesting_end")
            if bs:
                backtest_start = pd.to_datetime(bs).to_pydatetime()
            if be:
                backtest_end = pd.to_datetime(be).to_pydatetime()
            if "budget" in settings:
                budget = _safe_float(settings["budget"], budget)
        except Exception as exc:
            logger.warning("Failed to read settings.json: %s", exc)

    # Parse run_id for timestamp
    m = _RUN_ID_RE.match(f"{run_id}_stats")
    run_timestamp = datetime.utcnow()
    if m:
        date_str = m.group("date")
        time_str = m.group("time").replace("-", ":")
        try:
            run_timestamp = datetime.fromisoformat(f"{date_str}T{time_str}:00")
        except ValueError:
            pass

    con.execute(
        """
        INSERT OR REPLACE INTO backtest_runs
        (run_id, strategy_name, backtest_start, backtest_end, budget,
         final_value, total_return, max_drawdown, sharpe, sortino,
         run_timestamp, settings_json, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_id, m.group("strategy") if m else run_id,
         backtest_start, backtest_end, budget,
         final_value, total_return, max_dd, sharpe, sortino,
         run_timestamp, settings_json, str(stats_path)],
    )
    logger.info("Inserted run %s: final_value=%.2f, return=%.4f", run_id, final_value, total_return)
    return 1


def _sync_trades(con, logs_dir: Path, run_id: str) -> int:
    """Read trades.parquet → insert into backtest_trades. Returns rows inserted."""
    trades_path = _find_artifact(logs_dir, run_id, "trades")
    if trades_path is None:
        logger.warning("No trades artifact for run %s", run_id)
        return 0

    import pandas as pd
    df = pd.read_parquet(trades_path)
    if df.empty:
        logger.info("No trades in run %s (0 trades)", run_id)
        return 0

    rows = []
    for _, row in df.iterrows():
        rows.append([
            run_id,
            pd.to_datetime(row.get("time")).to_pydatetime() if pd.notna(row.get("time")) else None,
            str(row.get("symbol", "")),
            str(row.get("side", "")),
            _safe_float(row.get("filled_quantity")),
            _safe_float(row.get("price")),
            _safe_float(row.get("trade_cost")),
            str(row.get("asset.asset_type", "")),
            str(row.get("event_kind", "")),
        ])

    # Delete existing rows for this run_id (idempotent)
    con.execute("DELETE FROM backtest_trades WHERE run_id = ?", [run_id])
    con.executemany(
        """
        INSERT INTO backtest_trades
        (run_id, event_timestamp, symbol, side, filled_quantity,
         price, trade_cost, asset_type, event_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    logger.info("Inserted %d trades for run %s", len(rows), run_id)
    return len(rows)


def _sync_agent_detail(con, logs_dir: Path, run_id: str) -> int:
    """Read agent_detail.parquet → insert into agent_observations. Returns rows inserted."""
    agent_path = _find_artifact(logs_dir, run_id, "agent_detail")
    if agent_path is None:
        logger.warning("No agent_detail artifact for run %s", run_id)
        return 0

    import pandas as pd
    df = pd.read_parquet(agent_path)
    if df.empty:
        logger.info("No agent observations in run %s", run_id)
        return 0

    # Filter to call_summary rows only — these have the per-call aggregate
    summary_mask = df.get("is_call_summary", False) if "is_call_summary" in df.columns else False
    summaries = df[summary_mask].copy() if hasattr(summary_mask, '__len__') and len(summary_mask) > 0 else df.iloc[0:0]

    if len(summaries) == 0:
        # Fallback: take every row that has an agent_name
        summaries = df[df["agent_name"].notna()] if "agent_name" in df.columns else df.iloc[0:0]

    rows = []
    for _, row in summaries.iterrows():
        rows.append([
            run_id,
            str(row.get("agent_name", "")),
            int(row.get("call_index", 0)) if pd.notna(row.get("call_index")) else 0,
            pd.to_datetime(row.get("timestamp")).to_pydatetime() if pd.notna(row.get("timestamp")) else None,
            str(row.get("model", "")),
            str(row.get("summary", ""))[:5000] if pd.notna(row.get("summary")) else "",
            str(row.get("tool_sequence", ""))[:1000] if pd.notna(row.get("tool_sequence")) else "",
            int(row.get("tool_call_count", 0)) if pd.notna(row.get("tool_call_count")) else 0,
            int(row.get("call_total_tokens", 0)) if pd.notna(row.get("call_total_tokens")) else 0,
            int(row.get("call_latency_ms", 0)) if pd.notna(row.get("call_latency_ms")) else 0,
            str(row.get("context_text", ""))[:2000] if pd.notna(row.get("context_text")) else "",
            str(row.get("thinking_text", ""))[:2000] if pd.notna(row.get("thinking_text")) else "",
            str(row.get("warning_messages", ""))[:1000] if pd.notna(row.get("warning_messages")) else "",
        ])

    # Delete existing rows for this run_id (idempotent)
    con.execute("DELETE FROM agent_observations WHERE run_id = ?", [run_id])
    if rows:
        con.executemany(
            """
            INSERT INTO agent_observations
            (run_id, agent_name, call_index, timestamp, model, summary,
             tool_sequence, tool_call_count, call_total_tokens, call_latency_ms,
             context_text, thinking_text, warning_messages)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    logger.info("Inserted %d agent observations for run %s", len(rows), run_id)
    return len(rows)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_backtest_results(
    backtest_dir: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
    logs_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Sync a single backtest run's artifacts into nexus_results.duckdb.

    Parameters
    ----------
    backtest_dir : Path | str | None
        Can be:
        - A full path to any artifact file in the run (e.g. "..._stats.parquet")
        - A run_id string (e.g. "NexusCommitteeStrategy_2026-06-23_11-59_q3d6Ko")
        - None: auto-discover the latest run in logs_dir

    db_path : Path | str | None
        Override the DuckDB path (defaults to nexus-trade/data/nexus_results.duckdb)

    logs_dir : Path | str | None
        Override the logs directory (defaults to nexus-trade/logs/)

    Returns
    -------
    dict
        Summary: ``{run_id, runs_inserted, trades_inserted, observations_inserted, errors}``
    """
    import duckdb

    _db = Path(db_path) if db_path else _DUCKDB_PATH
    _logs = Path(logs_dir) if logs_dir else _LOGS_DIR

    ensure_schema(_db)

    # Determine run_id
    if backtest_dir is None:
        # Auto-discover latest run
        stats_files = sorted(_logs.glob("*_stats.parquet"), reverse=True)
        if not stats_files:
            return {"run_id": None, "runs_inserted": 0, "trades_inserted": 0,
                    "observations_inserted": 0, "errors": ["No stats files found"]}
        run_id = _parse_run_id(stats_files[0].name)
    elif isinstance(backtest_dir, str) and "/" not in backtest_dir and "\\" not in backtest_dir:
        # It's a run_id
        run_id = backtest_dir
    else:
        # It's a path to a file — extract run_id
        p = Path(backtest_dir)
        run_id = _parse_run_id(p.name)

    logger.info("Syncing backtest run: %s", run_id)

    con = duckdb.connect(str(_db))
    errors: list[str] = []
    runs = trades = observations = 0

    try:
        runs = _sync_stats(con, _logs, run_id)
    except Exception as exc:
        errors.append(f"stats: {exc}")
        logger.exception("Failed to sync stats for %s", run_id)

    try:
        trades = _sync_trades(con, _logs, run_id)
    except Exception as exc:
        errors.append(f"trades: {exc}")
        logger.exception("Failed to sync trades for %s", run_id)

    try:
        observations = _sync_agent_detail(con, _logs, run_id)
    except Exception as exc:
        errors.append(f"agent_detail: {exc}")
        logger.exception("Failed to sync agent_detail for %s", run_id)

    con.close()

    summary = {
        "run_id": run_id,
        "runs_inserted": runs,
        "trades_inserted": trades,
        "observations_inserted": observations,
        "errors": errors,
    }
    logger.info("Sync complete: %s", summary)
    return summary


def sync_all_runs(
    *,
    db_path: Path | str | None = None,
    logs_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Sync ALL backtest runs found in the logs directory.

    Returns a list of summary dicts, one per run.
    """
    _logs = Path(logs_dir) if logs_dir else _LOGS_DIR

    # Find all unique run_ids from stats.parquet files
    stats_files = sorted(_logs.glob("*_stats.parquet"))
    run_ids = sorted(set(_parse_run_id(f.name) for f in stats_files))

    results = []
    for run_id in run_ids:
        result = sync_backtest_results(run_id, db_path=db_path, logs_dir=logs_dir)
        results.append(result)

    logger.info("Synced %d runs total", len(results))
    return results


def sync_latest(*, db_path: Path | str | None = None, logs_dir: Path | str | None = None) -> dict[str, Any]:
    """Sync only the latest backtest run (most recent stats.parquet)."""
    return sync_backtest_results(None, db_path=db_path, logs_dir=logs_dir)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync LumiBot backtest artifacts → nexus_results.duckdb")
    parser.add_argument("run", nargs="?", default=None,
                        help="Run ID or artifact path (default: auto-discover latest)")
    parser.add_argument("--all", action="store_true", help="Sync all runs")
    parser.add_argument("--db", default=None, help="Override DuckDB path")
    parser.add_argument("--logs", default=None, help="Override logs directory")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.all:
        results = sync_all_runs(db_path=args.db, logs_dir=args.logs)
        for r in results:
            print(json.dumps(r, indent=2, default=str))
        print(f"\nTotal: {len(results)} runs synced")
    else:
        result = sync_backtest_results(args.run, db_path=args.db, logs_dir=args.logs)
        print(json.dumps(result, indent=2, default=str))
