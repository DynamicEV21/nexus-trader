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
import subprocess  # NEW (A3): used by _replay_trades_to_lancedb subprocess bridge
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except ImportError:
    # pandas is required for parquet reads. Defer error to call site so the
    # module still imports in the AQOS venv where pandas is installed.
    pd = None  # type: ignore[assignment]

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
# A3 (2026-06-25): backtest replay → nexus_decisions.lance
#
# For each closed round-trip trade (BUY → SELL), write a NexusDecisionRecord
# into the vector memory with:
#   - decision_sim_time  = the BUY bar time (when the trade was *decided*;
#                          this is the boundary the LLM was reasoning at).
#   - outcome            = "win" if realized_pnl > 0, else "loss".
#   - pnl_pct            = realized_pnl as % of entry notional.
#
# B2 anti-leakage: a backtest replay at sim-bar T must only see trades that
# were DECIDED at sim-bars <= T. We project the buy-bar datetime into
# ``decision_sim_time`` so the recall query's pre-filter
# (``decision_sim_time <= as_of_sim_time``) works correctly during future
# backtest replays.
# ---------------------------------------------------------------------------
_REPLAY_FLAG = os.environ.get("NEXUS_BACKTEST_REPLAY", "1") == "1"
_AQOS_VENV_PY = "/home/Zev/development/agentic-quant-os/.venv/bin/python"
_AQOS_PYTHONPATH = "/home/Zev/development/nexus-trade/src"

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
    run_id            VARCHAR,
    event_timestamp   TIMESTAMP,
    symbol            VARCHAR,
    side              VARCHAR,
    filled_quantity   DOUBLE,
    price             DOUBLE,
    trade_cost        DOUBLE,
    asset_type        VARCHAR,
    event_kind        VARCHAR,
    -- A3 (2026-06-25): memory replay fields. ``decision_sim_time`` is the
    -- BUY-bar datetime (the boundary the trade was *decided* at). It equals
    -- ``event_timestamp`` for BUY rows; for SELL rows it equals the matched
    -- BUY row's bar time. ``outcome`` is "win" / "loss" / "pending" once
    -- a round-trip closes. ``pnl_pct`` is the realized PnL as % of entry
    -- notional. ``round_trip_id`` groups entry+exit into a single decision.
    decision_sim_time TIMESTAMP,
    outcome           VARCHAR,
    pnl_pct           DOUBLE,
    round_trip_id     VARCHAR
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


def _strategy_from_run_id(run_id: str) -> str:
    """Extract the strategy name from a run_id (everything before the date).

    Example: "NexusCommitteeStrategy_2026-06-23_11-59_q3d6Ko"
             → "NexusCommitteeStrategy"
    """
    m = _RUN_ID_RE.match(f"{run_id}_stats")
    if m:
        return m.group("strategy")
    # Fallback: split on the first underscore followed by 4-digit year
    parts = run_id.split("_")
    for i, p in enumerate(parts):
        if len(p) == 4 and p.isdigit() and p.startswith(("19", "20")):
            return "_".join(parts[:i])
    return run_id.split("_")[0] if run_id else "unknown_strategy"


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
    """Create the 3 tables if they don't exist, plus idempotent migrations.

    Migrations (run on every call; safe because each is idempotent):
      * backtest_trades: add ``decision_sim_time``, ``outcome``, ``pnl_pct``,
        ``round_trip_id`` columns for A3 backtest replay.
      * backtest_runs: add ``sortino`` column for A1 Sortino-first ranking.
    """
    import duckdb
    path = db_path or _DUCKDB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(_SCHEMA_SQL)
        # Idempotent ALTERs for legacy tables that pre-date A1/A3 columns.
        # DuckDB doesn't support ``ADD COLUMN IF NOT EXISTS`` reliably across
        # versions, so we introspect the schema first.
        try:
            bt_cols = {
                row[0]
                for row in con.execute(
                    "PRAGMA table_info(backtest_trades)"
                ).fetchall()
            }
            if "decision_sim_time" not in bt_cols:
                con.execute("ALTER TABLE backtest_trades ADD COLUMN decision_sim_time TIMESTAMP")
                logger.info("backtest_trades: added 'decision_sim_time' column")
            if "outcome" not in bt_cols:
                con.execute("ALTER TABLE backtest_trades ADD COLUMN outcome VARCHAR")
                logger.info("backtest_trades: added 'outcome' column")
            if "pnl_pct" not in bt_cols:
                con.execute("ALTER TABLE backtest_trades ADD COLUMN pnl_pct DOUBLE")
                logger.info("backtest_trades: added 'pnl_pct' column")
            if "round_trip_id" not in bt_cols:
                con.execute("ALTER TABLE backtest_trades ADD COLUMN round_trip_id VARCHAR")
                logger.info("backtest_trades: added 'round_trip_id' column")
        except Exception as exc:
            logger.debug("backtest_trades migration skipped: %s", exc)
        try:
            br_cols = {
                row[0]
                for row in con.execute(
                    "PRAGMA table_info(backtest_runs)"
                ).fetchall()
            }
            if "sortino" not in br_cols:
                con.execute("ALTER TABLE backtest_runs ADD COLUMN sortino DOUBLE")
                logger.info("backtest_runs: added 'sortino' column")
        except Exception as exc:
            logger.debug("backtest_runs migration skipped: %s", exc)
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


# ---------------------------------------------------------------------------
# A3 helpers — backtest trade replay to nexus_decisions.lance
# ---------------------------------------------------------------------------

def _project_trades_to_round_trips(
    df: "pd.DataFrame",
    run_id: str,
) -> list[dict[str, Any]]:
    """Convert a LumiBot trades.parquet into closed round-trip records.

    LumiBot's trades.parquet has one row per FILL event. A round-trip is a
    BUY (entry) followed by a SELL (exit) on the same symbol. We use FIFO
    matching: each BUY opens a position; the next SELL closes the oldest
    open BUY at the matching quantity.

    The committee strategy currently doesn't execute trades (so
    trades.parquet is empty), but algo-bot backtests do produce trades
    and this routine must handle them correctly.

    Returns
    -------
    list[dict]
        Each dict has keys:
            run_id, decision_id, decision_sim_time, symbol, action,
            side, filled_quantity, entry_price, exit_price, entry_time,
            exit_time, pnl_pct, outcome, regime, indicators_snapshot
        Open (unclosed) positions get outcome="pending" and pnl_pct=0.0.
    """
    if df is None or df.empty:
        return []

    # Schema tolerance: LumiBot column names vary across versions. Lowercase
    # the columns we care about and remap common variants.
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    rename_map = {
        "filled_quantity": "filled_quantity",
        "asset.symbol": "symbol_alias",  # legacy; rarely present
        "event_timestamp": "time",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # Normalize symbol: prefer row.symbol, fall back to asset.symbol
    if "symbol" not in df.columns and "asset.symbol" in df.columns:
        df["symbol"] = df["asset.symbol"]

    # Normalize time to datetime
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
    else:
        return []  # no time column = no replayable events

    df = df.sort_values("time").reset_index(drop=True)

    # Per-symbol FIFO matching of buy → sell.
    open_positions: dict[str, list[dict[str, Any]]] = {}
    records: list[dict[str, Any]] = []

    # Best-effort regime hint: pull from the run's settings.json or detect
    # from the price action. We use "backtest" as the safe default — the
    # real regime classification would require running the full detector.
    regime = "backtest"

    for _, row in df.iterrows():
        side = str(row.get("side", "")).lower()
        sym = str(row.get("symbol", "") or "")
        qty = _safe_float(row.get("filled_quantity"))
        price = _safe_float(row.get("price"))
        ts = row.get("time")
        if pd.isna(ts):
            continue
        if not sym or qty <= 0 or price <= 0:
            continue

        if side in ("buy", "bot"):
            # Open a new position
            open_positions.setdefault(sym, []).append(
                {"entry_time": ts, "entry_price": price, "qty": qty}
            )
        elif side in ("sell", "sold"):
            # FIFO close against oldest open position for this symbol
            queue = open_positions.get(sym, [])
            if not queue:
                # Short or orphan close — record as standalone event with
                # unknown entry. Use the close time as decision_sim_time
                # so the recall filter still works.
                records.append({
                    "run_id": run_id,
                    "decision_id": f"{run_id}_short_{ts.isoformat()}_{sym}",
                    "decision_sim_time": ts,
                    "symbol": sym,
                    "action": "sell",
                    "side": "sell",
                    "filled_quantity": qty,
                    "entry_price": 0.0,
                    "exit_price": price,
                    "entry_time": None,
                    "exit_time": ts,
                    "pnl_pct": 0.0,
                    "outcome": "pending",
                    "regime": regime,
                    "indicators_snapshot": "{}",
                })
                continue

            entry = queue[0]
            entry_qty = entry["qty"]
            match_qty = min(entry_qty, qty)

            # Realized PnL as % of entry notional
            entry_notional = match_qty * entry["entry_price"]
            pnl_pct = 0.0
            if entry_notional > 0:
                pnl_pct = ((price - entry["entry_price"]) / entry["entry_price"]) * 100.0

            outcome = "win" if pnl_pct > 0 else "loss"

            rt_id = f"{run_id}_rt_{entry['entry_time'].isoformat()}_{sym}"
            records.append({
                "run_id": run_id,
                "decision_id": f"{rt_id}_dec",
                "decision_sim_time": entry["entry_time"],  # boundary the trade was decided at
                "symbol": sym,
                "action": "buy",  # original decision was BUY
                "side": "sell",  # this fill is the exit
                "filled_quantity": match_qty,
                "entry_price": entry["entry_price"],
                "exit_price": price,
                "entry_time": entry["entry_time"],
                "exit_time": ts,
                "pnl_pct": round(pnl_pct, 4),
                "outcome": outcome,
                "regime": regime,
                "indicators_snapshot": "{}",
            })

            # Decrement the queue
            if match_qty >= entry_qty - 1e-9:
                queue.pop(0)
            else:
                entry["qty"] -= match_qty

    # Remaining open positions → outcome=pending, pnl_pct=0
    for sym, queue in open_positions.items():
        for entry in queue:
            records.append({
                "run_id": run_id,
                "decision_id": f"{run_id}_open_{entry['entry_time'].isoformat()}_{sym}",
                "decision_sim_time": entry["entry_time"],
                "symbol": sym,
                "action": "buy",
                "side": "buy",
                "filled_quantity": entry["qty"],
                "entry_price": entry["entry_price"],
                "exit_price": 0.0,
                "entry_time": entry["entry_time"],
                "exit_time": None,
                "pnl_pct": 0.0,
                "outcome": "pending",
                "regime": regime,
                "indicators_snapshot": "{}",
            })

    return records


def _build_decision_record_for_memory(
    rt: dict[str, Any],
    strategy_name: str,
) -> dict[str, Any]:
    """Convert a round-trip record into a NexusDecisionRecord dict ready for embedding.

    The DNA string is the same format as the live ``NexusVectorMemory``
    store, so semantic search will surface backtest-replay decisions
    alongside live decisions when the recall query doesn't filter by
    backtest_id.
    """
    ts = rt.get("decision_sim_time")
    if hasattr(ts, "isoformat"):
        ts_iso = ts.isoformat(timespec="seconds")
    else:
        ts_iso = str(ts or "")

    symbol = rt.get("symbol", "")
    action = rt.get("action", "hold")
    regime = rt.get("regime", "backtest")
    outcome = rt.get("outcome", "pending")
    pnl_pct = float(rt.get("pnl_pct", 0.0))
    decision_id = rt.get("decision_id", "")

    thesis = (
        f"Backtest replay: {action.upper()} {symbol} @ {rt.get('entry_price', 0):.2f}, "
        f"exit @ {rt.get('exit_price', 0):.2f}, "
        f"{rt.get('filled_quantity', 0):.4f} units, "
        f"round-trip from {rt.get('entry_time')} to {rt.get('exit_time')}"
    )
    indicators = rt.get("indicators_snapshot") or "{}"

    return {
        "id": decision_id,
        "symbol": symbol,
        "action": action,
        "regime": regime,
        "thesis_summary": thesis[:500],
        "indicators_snapshot": indicators,
        "outcome": outcome,
        "pnl_pct": pnl_pct,
        "timestamp": ts_iso,
        # A3: stamp decision_sim_time so the recall pre-filter
        # (``decision_sim_time <= as_of_sim_time``) works during future
        # backtest replays.
        "decision_sim_time": ts_iso,
        "strategy_name": strategy_name,
        "backtest_id": rt.get("run_id", ""),
    }


def _replay_trades_to_lancedb(
    round_trips: list[dict[str, Any]],
    strategy_name: str,
) -> dict[str, int]:
    """Push round-trip records into nexus_decisions.lance via subprocess.

    The Lumibot venv doesn't have lancedb or sentence-transformers, so we
    spawn a subprocess in the AQOS venv to do the embedding + insert. This
    is the same pattern as ``src.memory.bridge.invoke_aqos_tool()``.

    Subprocess command:

        /home/Zev/development/agentic-quant-os/.venv/bin/python -m src.runners._replay_subprocess_runner
            --round-trips-json <tmp.json>
            --strategy-name <name>

    The runner imports ``NexusVectorMemory`` and calls
    ``batch_store_decisions``. It writes a JSON summary to stdout that we
    parse here.

    Returns
    -------
    dict
        Stats from the subprocess: {embedded, skipped, errors, total}.
        On subprocess failure: {errors: N, error_detail: str}.
    """
    if not round_trips:
        return {"embedded": 0, "skipped": 0, "errors": 0, "total": 0}

    if not os.path.exists(_AQOS_VENV_PY):
        logger.warning(
            "AQOS venv not found at %s — skipping memory replay", _AQOS_VENV_PY,
        )
        return {
            "embedded": 0, "skipped": 0,
            "errors": len(round_trips),
            "total": len(round_trips),
            "error_detail": f"AQOS venv missing at {_AQOS_VENV_PY}",
        }

    # Build the decision records (no embeddings yet — the subprocess does that)
    decision_records = [
        _build_decision_record_for_memory(rt, strategy_name)
        for rt in round_trips
    ]

    # Serialize to a temp JSON file in the project logs dir
    import tempfile, json
    tmp_dir = _PROJECT_ROOT / "logs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json",
        prefix=f"replay_{strategy_name}_",
        dir=str(tmp_dir), delete=False,
    ) as tmp:
        json.dump(decision_records, tmp, default=str)
        tmp_path = tmp.name

    try:
        cmd = [
            _AQOS_VENV_PY, "-m", "src.runners._replay_subprocess_runner",
            "--round-trips-json", tmp_path,
            "--strategy-name", strategy_name,
        ]
        logger.info(
            "Replaying %d backtest decisions via subprocess: %s",
            len(decision_records), " ".join(cmd),
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300,  # 5 min — embedding 100s of decisions on CPU is slow
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": _AQOS_PYTHONPATH},
        )
        if result.returncode != 0:
            logger.error(
                "Replay subprocess failed (rc=%d): %s",
                result.returncode, result.stderr[-2000:],
            )
            return {
                "embedded": 0, "skipped": 0,
                "errors": len(decision_records),
                "total": len(decision_records),
                "error_detail": f"subprocess rc={result.returncode}: {result.stderr[-500:]}",
            }
        # Parse last JSON line from stdout
        stdout = result.stdout.strip()
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {
            "embedded": 0, "skipped": 0,
            "errors": len(decision_records),
            "total": len(decision_records),
            "error_detail": f"could not parse subprocess stdout: {stdout[-200:]}",
        }
    except subprocess.TimeoutExpired:
        return {
            "embedded": 0, "skipped": 0,
            "errors": len(decision_records),
            "total": len(decision_records),
            "error_detail": "subprocess timeout (300s)",
        }
    except Exception as exc:
        logger.exception("Replay subprocess launch failed")
        return {
            "embedded": 0, "skipped": 0,
            "errors": len(decision_records),
            "total": len(decision_records),
            "error_detail": str(exc),
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _sync_trades(con, logs_dir: Path, run_id: str) -> int:
    """Read trades.parquet → insert into backtest_trades. Returns rows inserted.

    Populates the A3 fields (``decision_sim_time``, ``outcome``, ``pnl_pct``,
    ``round_trip_id``) from the FIFO round-trip projection so each fill is
    tagged with its outcome (win/loss/pending) and the entry-bar time the
    trade was decided at.
    """
    trades_path = _find_artifact(logs_dir, run_id, "trades")
    if trades_path is None:
        logger.warning("No trades artifact for run %s", run_id)
        return 0

    import pandas as pd
    df = pd.read_parquet(trades_path)
    if df.empty:
        logger.info("No trades in run %s (0 trades)", run_id)
        return 0

    # Project to round-trips so we can stamp outcome / pnl_pct / decision_sim_time
    round_trips = _project_trades_to_round_trips(df, run_id)
    rt_by_decision_time: dict[tuple[str, object], dict[str, Any]] = {}
    for rt in round_trips:
        key = (str(rt.get("symbol", "")), rt.get("entry_time") or rt.get("decision_sim_time"))
        rt_by_decision_time[key] = rt

    rows = []
    for _, row in df.iterrows():
        event_ts = (
            pd.to_datetime(row.get("time")).to_pydatetime()
            if pd.notna(row.get("time")) else None
        )
        sym = str(row.get("symbol", ""))
        # Look up matching round-trip to fill outcome / decision_sim_time
        rt = rt_by_decision_time.get((sym, event_ts))
        decision_sim_time = (
            rt.get("decision_sim_time") if rt else event_ts
        )
        outcome = rt.get("outcome") if rt else "pending"
        pnl_pct = rt.get("pnl_pct") if rt else 0.0
        round_trip_id = (
            rt.get("decision_id") if rt else None
        )
        # Normalize decision_sim_time to a Python datetime (or None)
        if decision_sim_time is None:
            ds_dt: Any = None
        else:
            try:
                ds_dt = pd.to_datetime(decision_sim_time).to_pydatetime()
            except Exception:
                ds_dt = None
        rows.append([
            run_id,
            event_ts,
            sym,
            str(row.get("side", "")),
            _safe_float(row.get("filled_quantity")),
            _safe_float(row.get("price")),
            _safe_float(row.get("trade_cost")),
            str(row.get("asset.asset_type", "")),
            str(row.get("event_kind", "")),
            ds_dt,
            outcome,
            float(pnl_pct) if pnl_pct is not None else 0.0,
            round_trip_id or "",
        ])

    # Delete existing rows for this run_id (idempotent)
    con.execute("DELETE FROM backtest_trades WHERE run_id = ?", [run_id])
    con.executemany(
        """
        INSERT INTO backtest_trades
        (run_id, event_timestamp, symbol, side, filled_quantity,
         price, trade_cost, asset_type, event_kind,
         decision_sim_time, outcome, pnl_pct, round_trip_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    logger.info("Inserted %d trades for run %s", len(rows), run_id)
    return len(rows)


def _sync_trades_to_memory(
    logs_dir: Path, run_id: str, strategy_name: str,
) -> dict[str, Any]:
    """A3: project trades → round-trips → push to nexus_decisions.lance.

    Reads the same trades.parquet artifact and projects it through
    ``_project_trades_to_round_trips``, then invokes the AQOS-venv
    subprocess bridge to embed + store each round-trip as a
    NexusDecisionRecord.

    Returns
    -------
    dict
        Stats: ``{round_trips, embedded, skipped, errors, replay_enabled}``.
        ``replay_enabled=False`` if NEXUS_BACKTEST_REPLAY is set to "0"
        (escape hatch for environments where the AQOS venv isn't reachable).
    """
    if not _REPLAY_FLAG:
        return {
            "round_trips": 0, "embedded": 0, "skipped": 0,
            "errors": 0, "replay_enabled": False,
        }

    trades_path = _find_artifact(logs_dir, run_id, "trades")
    if trades_path is None:
        return {
            "round_trips": 0, "embedded": 0, "skipped": 0,
            "errors": 0, "replay_enabled": True,
            "note": "no trades artifact",
        }

    import pandas as pd
    try:
        df = pd.read_parquet(trades_path)
    except Exception as exc:
        logger.warning("Failed to read trades parquet for replay (%s): %s", trades_path, exc)
        return {
            "round_trips": 0, "embedded": 0, "skipped": 0,
            "errors": 1, "replay_enabled": True,
            "error_detail": str(exc),
        }

    round_trips = _project_trades_to_round_trips(df, run_id)
    if not round_trips:
        return {
            "round_trips": 0, "embedded": 0, "skipped": 0,
            "errors": 0, "replay_enabled": True,
            "note": "no round-trips to replay (empty trades.parquet)",
        }

    stats = _replay_trades_to_lancedb(round_trips, strategy_name)
    return {
        "round_trips": len(round_trips),
        "embedded": stats.get("embedded", 0),
        "skipped": stats.get("skipped", 0),
        "errors": stats.get("errors", 0),
        "replay_enabled": True,
        "error_detail": stats.get("error_detail"),
    }


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
    strategy_name = _strategy_from_run_id(run_id)

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

    # A3 (2026-06-25): backtest replay → nexus_decisions.lance.
    # Done after the DuckDB sync so the trades table is the authoritative
    # source for the FIFO round-trip projection. Wrapped in try/except
    # because a subprocess bridge failure must not block the rest of the
    # sync summary.
    memory_replay: dict[str, Any] = {
        "round_trips": 0, "embedded": 0, "skipped": 0,
        "errors": 0, "replay_enabled": _REPLAY_FLAG,
    }
    try:
        memory_replay = _sync_trades_to_memory(_logs, run_id, strategy_name)
    except Exception as exc:
        errors.append(f"memory_replay: {exc}")
        logger.exception("Failed to replay trades to memory for %s", run_id)

    summary = {
        "run_id": run_id,
        "strategy_name": strategy_name,
        "runs_inserted": runs,
        "trades_inserted": trades,
        "observations_inserted": observations,
        "memory_replay": memory_replay,
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
