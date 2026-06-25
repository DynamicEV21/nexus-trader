"""
Walk-Forward Seeder — Project ``walk_forward_results`` into ``nexus_walkforward.lance``
======================================================================================

Reads the per-window OOS results from ``nexus_results.duckdb::walk_forward_results``
and projects them into the ``nexus_walkforward`` LanceDB collection as
embeddable, semantically-searchable OOS evidence for the committee PM.

Why a **separate** table from ``nexus_decisions``?
- Per-window OOS evidence is aggregate (one row per 6-month OOS test
  window) and lives in a different search space than per-trade decisions.
  Combining them would return 1 hit per OOS window when the PM asks
  "show me similar past decisions" — drowning the actual per-trade
  decision recall.
- Walk-forward memory is regenerated on a weekly cadence and is
  **wipe-and-rebuild** semantically, while ``nexus_decisions`` is
  append-only and grows organically. Different lifecycles justify
  different tables.

Why a **separate** process from Lumibot?
- Lumibot venv does NOT have ``lancedb`` or ``sentence-transformers``
  installed. Adding them would balloon Lumibot's dependency tree and
  conflict with the runtime guard in ``committee_smoke.py``. So this
  module runs in the AQOS venv via subprocess (``src/memory/bridge.py``
  pattern) when called from a Lumibot-context. From AQOS/AQS context,
  it can be called in-process.

Usage
-----
    # In-process (AQOS venv):
    from src.memory.walkforward_seeder import seed_walkforward_memory
    summary = seed_walkforward_memory(rebuild=True)

    # Subprocess from Lumibot venv:
    from src.memory.walkforward_seeder import seed_via_subprocess
    summary = seed_via_subprocess(rebuild=True)
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.environ.get(
    "NEXUS_PROJECT_ROOT",
    "/home/Zev/development/nexus-trade",
))
_DUCKDB_PATH = _PROJECT_ROOT / "data" / "nexus_results.duckdb"
_AQOS_VENV_PY = "/home/Zev/development/agentic-quant-os/.venv/bin/python"

# Ensure project importable for in-process callers. Add the project root
# (so `import src.memory.xxx` works) AND the src dir (so callers that do
# `from memory.xxx` work after the file is relocated).
_PROJECT_ROOT_STR = str(_PROJECT_ROOT.resolve())
_SRC_DIR_STR = str((_PROJECT_ROOT / "src").resolve())
for p in (_PROJECT_ROOT_STR, _SRC_DIR_STR):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Pure helpers (no external deps, safe in any venv)
# ---------------------------------------------------------------------------

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Defensively coerce a value to float.

    Returns ``default`` if:
      - the value is ``None``
      - the value is not numeric / not parseable as float
      - the resulting float is NaN or infinite (would otherwise poison
        downstream ``std``, ``sqrt``, and Sortino math)
    """
    if val is None:
        return default
    try:
        result = float(val)
    except (TypeError, ValueError):
        return default
    # Guard against NaN/inf — these would silently corrupt Sortino/Sharpe
    # calculations downstream.
    if not math.isfinite(result):
        return default
    return result


def _norm_strategy_name(name: str) -> str:
    """Normalize strategy name for stable IDs (strip version suffixes).

    Strategy names in the WF table look like ``meta_cmo_alma_atr_wf_v1`` and
    ``donchian_wpr_chop_breakout_wf_v1``. We keep them as-is — already
    canonicalized in the StratForge DB. Only strip leading/trailing whitespace.
    """
    if not name:
        return "unknown"
    return str(name).strip()


def _date_iso(val: Any) -> str:
    """Format a date-like value as ISO8601 string. Returns '' on failure."""
    if val is None:
        return ""
    try:
        if hasattr(val, "isoformat"):
            return val.isoformat()[:10]
        return str(val)[:10]
    except Exception:
        return ""


def _compute_composite_rank_score(
    sortino: float,
    sharpe: float,
    n_windows_total: int,
    n_profitable_windows: int,
    profitable_this_window: bool,
) -> float:
    """Composite Sortino-weighted score for re-ranking recall results.

    Formula::

        composite = sortino * (n_profitable_windows / max(n_windows_total, 1))
                  + 0.5 * sharpe
                  + (0.25 if profitable_this_window else -0.10)

    - Sortino is the primary signal (penalizes only downside volatility).
    - Win-rate across all windows is a multiplier (a strategy that's
      profitable in 9/10 windows ranks higher than 3/10 with the same
      Sortino).
    - Sharpe is a half-weight tiebreaker.
    - Single-window profitability adds a small bonus to break ties.
    """
    if n_windows_total <= 0:
        win_rate = 0.0
    else:
        win_rate = n_profitable_windows / n_windows_total
    bonus = 0.25 if profitable_this_window else -0.10
    return (
        sortino * win_rate
        + 0.5 * sharpe
        + bonus
    )


def _row_to_record(
    row: tuple,
    col_names: list[str],
    pair_aggregates: dict[tuple[str, str], dict[str, float]],
    now_iso: str,
) -> dict[str, Any]:
    """Convert a ``walk_forward_results`` DuckDB row into a walkforward record.

    Row columns (positional):
        id, strategy_name, symbol, window_index, train_start, train_end,
        test_start, test_end, total_return_pct, sharpe, sortino,
        max_drawdown_pct, profitable, num_entries, budget, created_at
    """
    rec = dict(zip(col_names, row))

    strategy = _norm_strategy_name(rec.get("strategy_name", ""))
    symbol = str(rec.get("symbol", "") or "")
    window_index = int(rec.get("window_index") or 0)
    test_start_iso = _date_iso(rec.get("test_start"))

    # Build the unique record id
    rec_id = (
        f"wf_{strategy}_{symbol}_w{window_index}_{test_start_iso or 'unknown'}"
    )

    # Pull pair-level aggregates (computed once and passed in)
    pair_key = (strategy, symbol)
    pair = pair_aggregates.get(pair_key, {})
    n_windows_total = int(pair.get("n_windows", 0))
    n_profitable_windows = int(pair.get("n_profitable", 0))
    avg_sortino = _safe_float(pair.get("avg_sortino"))
    avg_sharpe = _safe_float(pair.get("avg_sharpe"))

    sharpe_val = _safe_float(rec.get("sharpe"))
    total_return_pct = _safe_float(rec.get("total_return_pct"))
    max_drawdown_pct = _safe_float(rec.get("max_drawdown_pct"))
    sortino_val = _safe_float(rec.get("sortino"))

    # Backfill Sortino from a defensible heuristic when the source row has
    # NULL/0 (legacy data before 2026-06-25 when the sortino column was
    # added to walk_forward_results). We use the same heuristic as
    # walk_forward_validation._compute_sortino() so the seeded values are
    # consistent with future runs.
    #
    # NOTE: max_drawdown_pct is stored as a POSITIVE magnitude in
    # walk_forward_results (matches Lumibot convention). We use abs() to
    # be safe against either sign convention in the source data.
    if sortino_val == 0.0 and sharpe_val != 0.0:
        if abs(max_drawdown_pct) > 0 and total_return_pct > 0:
            calmar_like = abs(total_return_pct / max_drawdown_pct)
            # Empirical: Sortino ≈ Sharpe * min(1.5, max(1.0, calmar/2))
            # Calmar=2 → Sortino ≈ Sharpe * 1.0
            # Calmar=10 → Sortino ≈ Sharpe * 1.5 (capped)
            ratio = min(1.5, max(1.0, calmar_like / 2.0))
            sortino_val = round(sharpe_val * ratio, 3)
        else:
            # Flat or losing window: Sortino ≈ Sharpe (no extra upside vs Sharpe)
            sortino_val = round(sharpe_val, 3)

    profitable_val = bool(rec.get("profitable"))

    composite = _compute_composite_rank_score(
        sortino=sortino_val,
        sharpe=sharpe_val,
        n_windows_total=n_windows_total,
        n_profitable_windows=n_profitable_windows,
        profitable_this_window=profitable_val,
    )

    return {
        "id": rec_id,
        "strategy_name": strategy,
        "symbol": symbol,
        "regime": "unknown",  # placeholder; future seeder pass could derive from window dates
        "window_index": window_index,
        "train_start": _date_iso(rec.get("train_start")),
        "train_end": _date_iso(rec.get("train_end")),
        "test_start": test_start_iso,
        "test_end": _date_iso(rec.get("test_end")),
        "total_return_pct": total_return_pct,
        "sharpe": sharpe_val,
        "sortino": sortino_val,
        "max_drawdown_pct": max_drawdown_pct,
        "profitable": profitable_val,
        "num_entries": int(rec.get("num_entries") or 0),
        "budget": _safe_float(rec.get("budget")),
        "n_windows_total": n_windows_total,
        "n_profitable_windows": n_profitable_windows,
        "avg_sortino_across_windows": round(avg_sortino, 4),
        "avg_sharpe_across_windows": round(avg_sharpe, 4),
        "composite_rank_score": round(composite, 4),
        "timestamp": now_iso,
    }


# ---------------------------------------------------------------------------
# DuckDB read — pair aggregates + per-row scan
# ---------------------------------------------------------------------------

def _read_walkforward_from_duckdb(
    db_path: Path,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, float]]]:
    """Read all walk-forward rows + per-pair aggregates from DuckDB.

    Returns:
        (records, pair_aggregates)
        - records: list of walkforward record dicts ready for embedding
        - pair_aggregates: {(strategy, symbol): {n_windows, n_profitable,
            avg_sortino, avg_sharpe}}
    """
    import duckdb

    if not db_path.exists():
        raise FileNotFoundError(
            f"nexus_results.duckdb not found at {db_path}. "
            "Run walk_forward_validation first to generate OOS windows."
        )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        # Inspect schema — older DBs may not have sortino column
        try:
            col_names = [row[0] for row in con.execute(
                "DESCRIBE walk_forward_results"
            ).fetchall()]
        except Exception as exc:
            raise FileNotFoundError(
                f"walk_forward_results table missing in {db_path}: {exc}"
            )

        if not col_names:
            raise ValueError(
                "walk_forward_results has no columns (table empty or missing)"
            )

        has_sortino = "sortino" in col_names

        # Pull every row. Use SELECT * so we don't have to track schema drift.
        rows = con.execute(
            "SELECT * FROM walk_forward_results ORDER BY strategy_name, symbol, window_index"
        ).fetchall()

        # Pair-level aggregates (avg Sortino / Sharpe, n_windows, n_profitable)
        if has_sortino:
            agg_rows = con.execute("""
                SELECT strategy_name, symbol,
                       COUNT(*) as n_windows,
                       SUM(CASE WHEN profitable THEN 1 ELSE 0 END) as n_profitable,
                       AVG(sortino) as avg_sortino,
                       AVG(sharpe) as avg_sharpe
                FROM walk_forward_results
                GROUP BY strategy_name, symbol
            """).fetchall()
        else:
            # Legacy schema — sortino column missing; avg_sortino will be 0
            # (it'll be ignored in recall via min_sortino filter).
            agg_rows = con.execute("""
                SELECT strategy_name, symbol,
                       COUNT(*) as n_windows,
                       SUM(CASE WHEN profitable THEN 1 ELSE 0 END) as n_profitable,
                       AVG(NULL) as avg_sortino,
                       AVG(sharpe) as avg_sharpe
                FROM walk_forward_results
                GROUP BY strategy_name, symbol
            """).fetchall()

        pair_aggregates: dict[tuple[str, str], dict[str, float]] = {}
        for ar in agg_rows:
            key = (_norm_strategy_name(ar[0]), str(ar[1] or ""))
            pair_aggregates[key] = {
                "n_windows": int(ar[2] or 0),
                "n_profitable": int(ar[3] or 0),
                "avg_sortino": _safe_float(ar[4]),
                "avg_sharpe": _safe_float(ar[5]),
            }

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        records = [
            _row_to_record(r, col_names, pair_aggregates, now_iso)
            for r in rows
        ]
        return records, pair_aggregates

    finally:
        con.close()


# ---------------------------------------------------------------------------
# Main seeder
# ---------------------------------------------------------------------------

def seed_walkforward_memory(
    *,
    rebuild: bool = False,
    db_path: Path | None = None,
    lancedb_dir: str | None = None,
    only_profitable: bool = False,
    min_windows: int = 1,
) -> dict[str, Any]:
    """Seed the ``nexus_walkforward`` LanceDB table from ``walk_forward_results``.

    Parameters
    ----------
    rebuild : bool
        If True, drop all existing rows first (full re-seed). Default False
        (idempotent upsert — only new or changed windows are added).
    db_path : Path | None
        Override DuckDB path (defaults to nexus-trade/data/nexus_results.duckdb).
    lancedb_dir : str | None
        Override LanceDB persist directory.
    only_profitable : bool
        If True, only seed windows with ``profitable = True``. Default False.
    min_windows : int
        Skip (strategy, symbol) pairs with fewer than this many windows.
        Default 1 = seed everything. Use 3+ to bias toward well-tested
        strategies at recall time.

    Returns
    -------
    dict
        Summary: ``{read, eligible, embedded, skipped, errors, table, rebuild}``.
    """
    from src.memory.nexus_vector_memory import get_nexus_memory  # in-process

    db = db_path or _DUCKDB_PATH
    summary: dict[str, Any] = {
        "read": 0,
        "eligible": 0,
        "embedded": 0,
        "skipped": 0,
        "errors": 0,
        "table": "nexus_walkforward",
        "rebuild": rebuild,
    }

    try:
        records, pair_aggregates = _read_walkforward_from_duckdb(db)
    except Exception as exc:
        logger.exception("Failed to read walk-forward results from DuckDB")
        summary["errors"] = 1
        summary["error_detail"] = str(exc)
        return summary

    summary["read"] = len(records)

    # Apply filters
    eligible: list[dict[str, Any]] = []
    for rec in records:
        if only_profitable and not rec.get("profitable"):
            continue
        if rec.get("n_windows_total", 0) < min_windows:
            continue
        eligible.append(rec)
    summary["eligible"] = len(eligible)

    if not eligible:
        logger.warning(
            "No walkforward records eligible after filters "
            "(read=%d, only_profitable=%s, min_windows=%d)",
            summary["read"], only_profitable, min_windows,
        )
        return summary

    mem = get_nexus_memory()
    if lancedb_dir:
        mem._persist_dir = lancedb_dir  # override before ensure_db
        mem._db = None
        mem._walkforward_table = None

    if not mem.enabled:
        logger.warning("Nexus vector memory disabled — cannot seed walkforward")
        summary["errors"] = len(eligible)
        summary["error_detail"] = "vector memory disabled (lancedb/sentence-transformers unavailable)"
        return summary

    if rebuild:
        try:
            mem._ensure_db()
            if mem._walkforward_table is not None:
                existing_count = mem._walkforward_table.count_rows()
                if existing_count > 0:
                    # Walkforward memory is regenerated weekly; safest wipe is
                    # delete all rows. We don't drop the table itself because
                    # the schema is stable across runs.
                    mem._walkforward_table.delete("True")
                    logger.info(
                        "Rebuild: deleted %d existing walkforward rows",
                        existing_count,
                    )
        except Exception as exc:
            logger.warning("Failed to clear walkforward table for rebuild: %s", exc)

    try:
        stats = mem.batch_store_walkforward_records(eligible, batch_size=64)
        summary["embedded"] = stats["embedded"]
        summary["skipped"] = stats["skipped"]
        summary["errors"] = stats["errors"]
        logger.info(
            "Walkforward seed complete: read=%d eligible=%d embedded=%d skipped=%d errors=%d",
            summary["read"], summary["eligible"], summary["embedded"],
            summary["skipped"], summary["errors"],
        )
    except Exception as exc:
        logger.exception("Walkforward seed batch_store failed")
        summary["errors"] += len(eligible)
        summary["error_detail"] = str(exc)

    return summary


# ---------------------------------------------------------------------------
# Subprocess wrapper for Lumibot venv callers
# ---------------------------------------------------------------------------

_SEEDER_SCRIPT_NAME = "_walkforward_seeder_runner.py"


def _write_seeder_runner_script(target_dir: Path) -> Path:
    """Write a small driver script that imports the seeder and runs it.

    We avoid passing complex Python via -c on the command line (escaping
    is brittle) and instead write a temp script under the project so the
    subprocess can import src.memory.walkforward_seeder normally.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    script_path = target_dir / _SEEDER_SCRIPT_NAME
    script_path.write_text(
        '''#!/usr/bin/env python
"""Auto-generated subprocess driver for walkforward_seeder.seed_walkforward_memory."""
import json, sys
sys.path.insert(0, "/home/Zev/development/nexus-trade/src")
from src.memory.walkforward_seeder import seed_walkforward_memory

rebuild = "--rebuild" in sys.argv
only_profitable = "--only-profitable" in sys.argv
min_windows = 1
for i, a in enumerate(sys.argv):
    if a == "--min-windows" and i + 1 < len(sys.argv):
        try: min_windows = int(sys.argv[i + 1])
        except: pass

summary = seed_walkforward_memory(
    rebuild=rebuild,
    only_profitable=only_profitable,
    min_windows=min_windows,
)
print(json.dumps(summary, indent=2, default=str))
sys.exit(0 if summary.get("errors", 0) == 0 else 1)
'''
    )
    return script_path


def seed_via_subprocess(
    *,
    rebuild: bool = False,
    only_profitable: bool = False,
    min_windows: int = 1,
    timeout: int = 600,
) -> dict[str, Any]:
    """Run the walkforward seeder in the AQOS venv via subprocess.

    This is the safe entry point when called from the Lumibot venv
    (which lacks lancedb and sentence-transformers). Returns the same
    summary dict as ``seed_walkforward_memory``.

    Parameters
    ----------
    rebuild : bool
        Drop existing rows and re-seed.
    only_profitable : bool
        Only seed profitable windows.
    min_windows : int
        Skip pairs with fewer than N windows.
    timeout : int
        Subprocess timeout in seconds (default 600 = 10 min).

    Returns
    -------
    dict
        Summary from the subprocess (parsed from JSON stdout).
        On failure, returns ``{"errors": 1, "error_detail": str}``.
    """
    if not os.path.exists(_AQOS_VENV_PY):
        return {
            "errors": 1,
            "error_detail": f"AQOS venv python not found at {_AQOS_VENV_PY}",
        }

    script_path = _write_seeder_runner_script(_PROJECT_ROOT / "logs")
    if not script_path.exists():
        return {
            "errors": 1,
            "error_detail": f"Could not write seeder runner script at {script_path}",
        }

    cmd = [
        _AQOS_VENV_PY,
        str(script_path),
    ]
    if rebuild:
        cmd.append("--rebuild")
    if only_profitable:
        cmd.append("--only-profitable")
    if min_windows > 1:
        cmd.extend(["--min-windows", str(min_windows)])

    logger.info("Launching walkforward seeder subprocess: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(_PROJECT_ROOT),
        )
        if result.returncode != 0:
            logger.error(
                "Seeder subprocess failed (rc=%d): %s",
                result.returncode, result.stderr[-2000:],
            )
            return {
                "errors": 1,
                "error_detail": f"subprocess rc={result.returncode}",
                "stderr_tail": result.stderr[-2000:],
            }
        # Last JSON block in stdout
        stdout = result.stdout.strip()
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            # Sometimes the subprocess prints logs before the JSON block;
            # find the last line that parses as JSON.
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            return {
                "errors": 1,
                "error_detail": "could not parse subprocess stdout as JSON",
                "stdout_tail": stdout[-2000:],
            }
    except subprocess.TimeoutExpired:
        return {
            "errors": 1,
            "error_detail": f"subprocess timed out after {timeout}s",
        }
    except Exception as exc:
        return {
            "errors": 1,
            "error_detail": f"subprocess launch failed: {exc}",
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Seed nexus_walkforward.lance from walk_forward_results DuckDB table"
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Drop existing rows and re-seed from scratch",
    )
    parser.add_argument(
        "--only-profitable", action="store_true",
        help="Only seed profitable windows",
    )
    parser.add_argument(
        "--min-windows", type=int, default=1,
        help="Skip (strategy, symbol) pairs with fewer than N windows (default 1)",
    )
    parser.add_argument(
        "--db", default=None,
        help="Override DuckDB path",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    summary = seed_walkforward_memory(
        rebuild=args.rebuild,
        db_path=Path(args.db) if args.db else None,
        only_profitable=args.only_profitable,
        min_windows=args.min_windows,
    )
    print(json.dumps(summary, indent=2, default=str))
    sys.exit(0 if summary.get("errors", 0) == 0 else 1)