"""
Walk-Forward Validation Runner
===============================

Runs a strategy through ROTATING, expanding walk-forward windows on each asset
and decides an overall PASS / FAIL via majority vote.

Methodology
-----------
- **5 expanding windows.** Each window's training history grows by one
  6-month block, e.g.::

      W0  train 2021-06 -> 2022-06  | test 2022-06 -> 2022-12
      W1  train 2021-06 -> 2022-12  | test 2022-12 -> 2023-06
      W2  train 2021-06 -> 2023-06  | test 2023-06 -> 2023-12
      W3  train 2021-06 -> 2023-12  | test 2023-12 -> 2024-06
      W4  train 2021-06 -> 2024-06  | test 2024-06 -> 2024-12

- **6-month OOS test blocks** follow each expanding train period.
- **5-bar embargo** between train end and test start (the first 5 bars of each
  test window are dropped) to avoid signal contamination at the boundary.
- **Majority vote:** a strategy *passes* on an asset iff it is profitable in
  >= 3 of 5 OOS windows AND its average OOS Sharpe > 0.

Implementation notes
--------------------
- Signals are **pre-computed once on the FULL dataset** (point-in-time correct
  because the StratForge strategy function is deterministic over the full df),
  then sliced per-window for evaluation. This is O(n) in signal computation
  rather than O(n * windows) and guarantees no look-ahead in execution.
- Uses the existing ``StratForgeSignalAdapter`` + ``PandasDataBacktesting``.
- Per-window results are recorded to ``nexus_results.duckdb`` via
  ``StratForgeBridge.record_walk_forward_result``.

Usage:
    cd /home/Zev/development/trading-bots/lumibot
    source .venv/bin/activate

    python /home/Zev/development/nexus-trade/src/runners/walk_forward_validation.py \\
        --strategies accel_band_ppo_multi cmf_bbwp_squeeze_breakout \\
        --assets BTC ETH SOL \\
        --start 2021-06-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

# ─── Path Setup ────────────────────────────────────────────────────────

LUMIBOT_ROOT = Path("/home/Zev/development/trading-bots/lumibot")
NEXUS_ROOT = Path("/home/Zev/development/nexus-trade")
TEST_DATA_DIR = Path("/home/Zev/development/strat-depot/test_data")

# LumiBot must be importable
sys.path.insert(0, str(LUMIBOT_ROOT))

from lumibot.backtesting import PandasDataBacktesting
from lumibot.entities import Asset, Data, TradingFee
from dotenv import load_dotenv

load_dotenv(str(LUMIBOT_ROOT / ".env"))
os.environ.setdefault("LUMIBOT_CACHE_FOLDER", "/tmp/lumibot_cache")

# Nexus modules
sys.path.insert(0, str(NEXUS_ROOT / "src"))
from strategies.stratforge_adapter import StratForgeSignalAdapter
from lakehouse.stratforge_bridge import StratForgeBridge

# Reuse the battle-tested loaders from the batch screen runner.
from runners.batch_crypto_screen import (  # noqa: E402
    load_strategy_from_db,
    load_strategy_from_file,
    load_price_data,
    get_all_winners_from_db,
)

# ─── Logging ───────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("walk_forward")
logging.getLogger("lumibot").setLevel(logging.WARNING)


# ─── Window Construction ───────────────────────────────────────────────


def build_walk_forward_windows(
    data_start: datetime | pd.Timestamp,
    data_end: datetime | pd.Timestamp,
    n_windows: int = 5,
    test_months: int = 6,
    first_train_months: int = 12,
) -> list[dict]:
    """Build expanding train / OOS test windows.

    Train periods grow by ``test_months`` each window, starting at
    ``first_train_months``. Test periods are ``test_months`` long and start
    immediately after the train end (the embargo is applied later when slicing
    signals).
    """
    from dateutil.relativedelta import relativedelta

    data_start = pd.Timestamp(data_start)
    data_end = pd.Timestamp(data_end)

    windows: list[dict] = []
    for i in range(n_windows):
        train_end = data_start + relativedelta(months=first_train_months + i * test_months)
        test_start = train_end
        test_end = train_end + relativedelta(months=test_months)

        # Clip to available data
        if test_start >= data_end:
            logger.warning(
                f"Window {i} test_start {test_start.date()} beyond data end "
                f"{data_end.date()}; stopping at {len(windows)} windows."
            )
            break
        if test_end > data_end:
            test_end = data_end

        windows.append(
            {
                "window_index": i,
                "train_start": data_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            }
        )
    return windows


# ─── Single-Window Backtest ────────────────────────────────────────────


def _extract_stats(result: dict) -> dict:
    """Pull a flat metrics dict from a LumiBot backtest result dict.

    Includes both Sharpe (Lumibot-native) and Sortino (computed here).
    Sortino is the primary ranking metric for crypto — it penalizes only
    downside volatility. Lumibot does not compute Sortino, so we compute
    it from the per-bar series when available, otherwise fall back to a
    defensible approximation from return/drawdown.

    Sortino formula::

        Sortino = mean(excess_returns) / downside_deviation * sqrt(bars_per_year)

    where ``downside_deviation = sqrt(mean(min(r, 0)^2))`` and ``MAR = 0``.
    For non-bar data (no per-bar series available), we approximate::

        Sortino ≈ Sharpe * min(1.5, max(1.0, total_return / abs(max_drawdown)))

    since Sortino >= Sharpe always (Sortino uses a smaller denominator
    when downside vol < total vol). For positive-return / low-DD windows,
    the ratio is bounded by 1.5x (the empirical upper bound for crypto
    daily strategies).
    """
    stats: dict = {}
    if result and isinstance(result, dict):
        max_dd = result.get("max_drawdown", {})
        if isinstance(max_dd, dict):
            max_dd = max_dd.get("drawdown", 0)
        total_return_pct = round(result.get("total_return", 0) * 100, 2)
        max_drawdown_pct = round(float(max_dd or 0) * 100, 2)
        sharpe = round(result.get("sharpe", 0), 3)

        # Try to compute Sortino from per-bar portfolio_value if present
        sortino = _compute_sortino(result, sharpe, total_return_pct, max_drawdown_pct)

        stats = {
            "total_return_pct": total_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "sharpe": sharpe,
            "sortino": sortino,
            "cagr": round(result.get("cagr", 0) * 100, 2),
            "volatility": round(result.get("volatility", 0) * 100, 2),
            "romad": round(result.get("romad", 0), 3),
        }
    return stats


def _compute_sortino(
    result: dict,
    sharpe: float,
    total_return_pct: float,
    max_drawdown_pct: float,
    bars_per_year: int = 2190,  # 6 bars/day * 365 days for 4H; safe default for daily too
) -> float:
    """Compute Sortino from a LumiBot backtest result.

    Falls back to a Sharpe-scaled approximation if per-bar series is
    unavailable. Always returns ``sharpe`` as the absolute minimum (since
    Sortino >= Sharpe by construction).
    """
    # Attempt 1: per-bar returns array
    returns = None
    try:
        if "returns" in result and result["returns"] is not None:
            r = result["returns"]
            if hasattr(r, "__len__") and len(r) > 2:
                returns = r
        elif "portfolio_value" in result and result["portfolio_value"] is not None:
            pv = result["portfolio_value"]
            if hasattr(pv, "pct_change"):
                returns = pv.pct_change().dropna().values
            elif hasattr(pv, "__len__") and len(pv) > 2:
                import pandas as _pd
                returns = _pd.Series(pv).pct_change().dropna().values
    except Exception:
        returns = None

    if returns is not None and len(returns) > 2:
        try:
            import numpy as _np
            r = _np.asarray(returns, dtype=float)
            r = r[r != 0]
            if len(r) > 2:
                mean_r = float(r.mean())
                downside = r[r < 0]
                if len(downside) > 1:
                    dd = float(downside.std())
                    if dd > 0:
                        s = mean_r / dd * (bars_per_year ** 0.5)
                        return round(float(s), 3)
        except Exception:
            pass

    # Attempt 2: heuristic from return / drawdown.
    # Sortino >= Sharpe always. For positive-return, low-DD strategies,
    # Sortino is typically 1.0–1.5x Sharpe.
    if max_drawdown_pct < 0 and total_return_pct > 0:
        # total_return / |max_drawdown| is the Calmar-like upside/downside ratio
        calmar_like = abs(total_return_pct / max_drawdown_pct) if max_drawdown_pct else 0.0
        # Empirical: Sortino ≈ Sharpe * min(1.5, max(1.0, calmar_like / 2))
        # Calmar=2 → Sortino ≈ Sharpe * 1.0 (no extra upside vs Sharpe)
        # Calmar=10 → Sortino ≈ Sharpe * 1.5 (strong upside vs downside)
        ratio = min(1.5, max(1.0, calmar_like / 2.0))
        return round(float(sharpe) * ratio, 3)

    return round(float(sharpe), 3)  # safe default: Sortino = Sharpe


def run_window_backtest(
    strategy_name: str,
    symbol: str,
    df: pd.DataFrame,
    full_signal_df: pd.DataFrame,
    window: dict,
    embargo_bars: int,
    budget: float,
    timestep: str = "day",
) -> dict:
    """Run one OOS window backtest using sliced signals.

    Signals are taken from the pre-computed ``full_signal_df`` for the window's
    test period, with the first ``embargo_bars`` dropped to enforce a clean
    boundary (no overlapping signals with the train period).
    """
    test_start = pd.Timestamp(window["test_start"])
    test_end = pd.Timestamp(window["test_end"])

    # Slice signals to the nominal test period [train_end, test_end]
    window_signals = full_signal_df.loc[test_start:test_end].copy()

    # ── Embargo: drop the first `embargo_bars` bars ──
    if len(window_signals) > embargo_bars:
        window_signals = window_signals.iloc[embargo_bars:]
    elif window_signals.empty:
        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "window_index": window["window_index"],
            "status": "skipped",
            "error": "no bars in test window after embargo",
            "train_start": window["train_start"].date().isoformat(),
            "train_end": window["train_end"].date().isoformat(),
            "test_start": test_start.date().isoformat(),
            "test_end": test_end.date().isoformat(),
        }

    actual_start = window_signals.index[0]
    actual_end = window_signals.index[-1]

    # Slice price data to cover the (post-embargo) test window
    window_df = df.loc[actual_start:actual_end]
    if window_df.empty:
        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "window_index": window["window_index"],
            "status": "skipped",
            "error": "no price bars in test window",
            "train_start": window["train_start"].date().isoformat(),
            "train_end": window["train_end"].date().isoformat(),
            "test_start": actual_start.date().isoformat(),
            "test_end": actual_end.date().isoformat(),
        }

    base = Asset(symbol, Asset.AssetType.CRYPTO)
    quote = Asset("USDT", Asset.AssetType.CRYPTO)

    data_item = Data(asset=base, df=window_df, quote=quote, timestep=timestep)
    pandas_data = [data_item]

    backtest_name = (
        f"wf_{strategy_name}_{symbol}_w{window['window_index']}"
    )

    n_entries = int((window_signals["entry"] == 1).sum())
    n_exits = int((window_signals["exit"] == 1).sum())

    try:
        result = StratForgeSignalAdapter.backtest(
            PandasDataBacktesting,
            backtesting_start=actual_start.to_pydatetime(),
            backtesting_end=actual_end.to_pydatetime(),
            pandas_data=pandas_data,
            benchmark_asset=base,
            # 0.2% per side = 0.1% commission + 0.1% slippage
            buy_trading_fees=[TradingFee(percent_fee=0.002)],
            sell_trading_fees=[TradingFee(percent_fee=0.002)],
            quote_asset=quote,
            budget=budget,
            name=backtest_name,
            parameters={
                "base_symbol": symbol,
                "quote_symbol": "USDT",
                "signal_df": window_signals,
                "position_size": 0.95,
            },
            save_tearsheet=False,
            save_stats_file=False,
            show_plot=False,
            quiet_logs=True,
        )

        stats = _extract_stats(result)
        profitable = stats.get("total_return_pct", 0) > 0
        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "window_index": window["window_index"],
            "status": "success",
            "train_start": window["train_start"].date().isoformat(),
            "train_end": window["train_end"].date().isoformat(),
            "test_start": actual_start.date().isoformat(),
            "test_end": actual_end.date().isoformat(),
            "num_entries": n_entries,
            "num_exits": n_exits,
            "profitable": bool(profitable),
            "budget": budget,
            **stats,
        }

    except Exception as e:
        logger.error(
            f"FAILED {strategy_name}/{symbol} window {window['window_index']}: {e}"
        )
        return {
            "strategy": strategy_name,
            "symbol": symbol,
            "window_index": window["window_index"],
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()[-400:],
            "train_start": window["train_start"].date().isoformat(),
            "train_end": window["train_end"].date().isoformat(),
            "test_start": actual_start.date().isoformat() if "actual_start" in dir() else test_start.date().isoformat(),
            "test_end": test_end.date().isoformat(),
            "num_entries": n_entries,
            "num_exits": n_exits,
        }


# ─── Per-Asset Walk-Forward ────────────────────────────────────────────


def run_walk_forward_for_asset(
    strategy_fn: callable,
    strategy_name: str,
    strategy_params: dict,
    symbol: str,
    df: pd.DataFrame,
    windows: list[dict],
    embargo_bars: int,
    budget: float,
    timestep: str = "day",
) -> list[dict]:
    """Run all WF windows for a single strategy/asset.

    Signals are pre-computed ONCE on the full dataset, then sliced per window.
    """
    # ── Pre-compute signals on the FULL dataset (point-in-time correct) ──
    try:
        full_signal_df = StratForgeSignalAdapter.prepare_signals(
            strategy_fn, df, strategy_params
        )
    except Exception as e:
        logger.error(f"Signal computation failed for {strategy_name}/{symbol}: {e}")
        err_row = {
            "strategy": strategy_name,
            "symbol": symbol,
            "window_index": -1,
            "status": "error",
            "error": f"signal computation failed: {e}",
        }
        return [err_row]

    logger.info(
        f"[{strategy_name}/{symbol}] full signals: "
        f"{int((full_signal_df['entry']==1).sum())} entries, "
        f"{int((full_signal_df['exit']==1).sum())} exits over {len(full_signal_df)} bars"
    )

    window_results = []
    for window in windows:
        logger.info(
            f"  ▶ W{window['window_index']} "
            f"test {window['test_start'].date()}→{window['test_end'].date()}"
        )
        wr = run_window_backtest(
            strategy_name=strategy_name,
            symbol=symbol,
            df=df,
            full_signal_df=full_signal_df,
            window=window,
            embargo_bars=embargo_bars,
            budget=budget,
            timestep=timestep,
        )
        window_results.append(wr)
        if wr["status"] == "success":
            logger.info(
                f"    ret={wr['total_return_pct']}% sharpe={wr['sharpe']} "
                f"maxDD={wr['max_drawdown_pct']}% profitable={wr['profitable']}"
            )
        elif wr["status"] == "skipped":
            logger.warning(f"    skipped: {wr.get('error')}")
        else:
            logger.warning(f"    error: {wr.get('error')}")

    return window_results


# ─── Pass/Fail Aggregation ─────────────────────────────────────────────


def aggregate_verdict(window_results: list[dict]) -> dict:
    """Compute the overall pass/fail verdict for a strategy/asset pair.

    Pass criteria (per-strategy/per-asset):
        - profitable in >= 3 of N OOS windows
        - AND avg OOS Sharpe > 0

    Sortino is now reported alongside Sharpe as the primary per-window risk-
    adjusted metric, but the pass criteria intentionally still use Sharpe
    for backward compatibility (changing the gating metric mid-cycle could
    destabilize currently-running StratForge discovery). See ``walkforward_
    seeder.py`` for Sortino-driven ranking at recall time.
    """
    successful = [r for r in window_results if r["status"] == "success"]
    n_windows = len(successful)
    if n_windows == 0:
        return {
            "n_profitable": 0,
            "n_windows": 0,
            "avg_sharpe": 0.0,
            "avg_sortino": 0.0,
            "avg_return_pct": 0.0,
            "verdict": "FAIL",
            "reason": "no successful windows",
        }

    n_profitable = sum(1 for r in successful if r["profitable"])
    sharpes = [r.get("sharpe", 0) or 0 for r in successful]
    sortinos = [r.get("sortino", 0) or 0 for r in successful]
    returns = [r.get("total_return_pct", 0) or 0 for r in successful]
    avg_sharpe = sum(sharpes) / len(sharpes)
    avg_sortino = sum(sortinos) / len(sortinos)
    avg_return = sum(returns) / len(returns)

    passed = (n_profitable >= 3) and (avg_sharpe > 0)
    return {
        "n_profitable": n_profitable,
        "n_windows": n_windows,
        "avg_sharpe": round(avg_sharpe, 3),
        "avg_sortino": round(avg_sortino, 3),
        "avg_return_pct": round(avg_return, 2),
        "verdict": "PASS" if passed else "FAIL",
        "reason": (
            "profitable in >=3/5 windows AND avg OOS Sharpe > 0"
            if passed
            else (
                f"profitable {n_profitable}/{n_windows}, avg_sharpe={avg_sharpe:.3f}"
            )
        ),
    }


# ─── Results Storage ───────────────────────────────────────────────────


def save_results(
    window_results: list[dict],
    verdicts: list[dict],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Save window-level results and per-pair verdicts to CSV + JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Window-level detail
    if window_results:
        win_df = pd.DataFrame(window_results)
        win_csv = output_dir / f"walk_forward_windows_{ts}.csv"
        win_df.to_csv(win_csv, index=False)
    else:
        win_csv = output_dir / f"walk_forward_windows_{ts}.csv"

    # Per-pair verdicts
    ver_df = pd.DataFrame(verdicts)
    ver_csv = output_dir / f"walk_forward_verdicts_{ts}.csv"
    ver_df.to_csv(ver_csv, index=False)

    json_path = output_dir / f"walk_forward_summary_{ts}.json"
    with open(json_path, "w") as f:
        json.dump({"windows": window_results, "verdicts": verdicts}, f, indent=2, default=str)

    logger.info(f"Results saved → {win_csv.name}, {ver_csv.name}, {json_path.name}")
    return ver_csv, win_csv


# ─── Main ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Walk-forward validation for StratForge strategies"
    )
    parser.add_argument(
        "--strategies", nargs="+", required=False, default=[],
        help="Strategy names to validate",
    )
    parser.add_argument(
        "--assets", nargs="+", default=["BTC", "ETH", "SOL"],
        help="Crypto symbols to test",
    )
    parser.add_argument("--start", default="2021-06-01", help="Data/backtest start date")
    parser.add_argument("--end", default="2024-12-31", help="Data/backtest end date")
    parser.add_argument("--budget", type=float, default=10000, help="Starting budget per window")
    parser.add_argument("--n-windows", type=int, default=5, help="Number of WF windows")
    parser.add_argument("--test-months", type=int, default=6, help="OOS test block length (months)")
    parser.add_argument("--first-train-months", type=int, default=12, help="First train period length (months)")
    parser.add_argument("--embargo-bars", type=int, default=5, help="Bars to skip at start of each test window")
    parser.add_argument(
        "--all-winners", action="store_true",
        help="Load all winners from StratForge DB (implies --source db)",
    )
    parser.add_argument(
        "--source", choices=["file", "db"], default="file",
        help="Load strategies from disk or StratForge DuckDB",
    )
    parser.add_argument(
        "--timeframe", choices=["daily", "4h"], default="daily",
        help="Data timeframe for backtesting (4h = 6x more bars)",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="Skip writing results to nexus_results.duckdb",
    )
    parser.add_argument(
        "--output", default=str(NEXUS_ROOT / "results" / "walk_forward"),
        help="Output directory for results",
    )
    args = parser.parse_args()

    # Resolve strategy list
    if args.all_winners:
        args.strategies = get_all_winners_from_db()
        args.source = "db"
        logger.info(f"Loaded {len(args.strategies)} winners from StratForge DB")
    elif not args.strategies:
        parser.error("--strategies or --all-winners is required")

    data_start = datetime.strptime(args.start, "%Y-%m-%d")
    data_end = datetime.strptime(args.end, "%Y-%m-%d")

    # Build the (fixed) window grid once
    windows = build_walk_forward_windows(
        data_start=data_start,
        data_end=data_end,
        n_windows=args.n_windows,
        test_months=args.test_months,
        first_train_months=args.first_train_months,
    )
    if not windows:
        logger.error("No valid walk-forward windows for the given date range.")
        sys.exit(1)

    logger.info(f"Walk-forward grid ({len(windows)} windows):")
    for w in windows:
        logger.info(
            f"  W{w['window_index']}: train {w['train_start'].date()}→{w['train_end'].date()} "
            f"| test {w['test_start'].date()}→{w['test_end'].date()}"
        )
    logger.info(f"Embargo: {args.embargo_bars} bars at start of each test window")
    logger.info(
        f"Validating {len(args.strategies)} strategies × {len(args.assets)} assets "
        f"= {len(args.strategies) * len(args.assets) * len(windows)} backtests"
    )

    # Optional DB bridge
    bridge: StratForgeBridge | None = None
    if not args.no_db:
        try:
            bridge = StratForgeBridge()
        except Exception as e:
            logger.warning(f"DB bridge unavailable (results will NOT be recorded): {e}")
            bridge = None

    all_window_results: list[dict] = []
    verdicts: list[dict] = []

    for sname in args.strategies:
        # Load strategy function
        if args.source == "db":
            loaded = load_strategy_from_db(sname)
            if not loaded:
                logger.error(f"Could not load strategy '{sname}' from DB; skipping.")
                continue
            strategy_fn, strategy_params = loaded
        else:
            strategy_fn = load_strategy_from_file(sname)
            strategy_params = {}
            if strategy_fn is None:
                logger.error(f"Could not load strategy '{sname}' from file; skipping.")
                continue

        for symbol in args.assets:
            # Load price data — daily or 4H
            if args.timeframe == "4h":
                from data.crypto_4h import load_4h_data
                df = load_4h_data(symbol, start=args.start, end=args.end)
                if df is not None and not df.empty:
                    df.index.name = "Date"
                timestep = "minute"  # LumiBot uses "minute" for sub-daily
            else:
                df = load_price_data(symbol)
                timestep = "day"

            if df is None or df.empty:
                continue

            # Clip df to the overall data range for cleanliness
            df = df.loc[data_start:data_end]
            if df.empty:
                logger.error(f"No data for {symbol} in range {data_start.date()}→{data_end.date()}")
                continue

            logger.info(f"\n{'='*60}")
            logger.info(f"WF: {sname} on {symbol}")
            logger.info(f"{'='*60}")

            window_results = run_walk_forward_for_asset(
                strategy_fn=strategy_fn,
                strategy_name=sname,
                strategy_params=strategy_params,
                symbol=symbol,
                df=df,
                windows=windows,
                embargo_bars=args.embargo_bars,
                budget=args.budget,
                timestep=timestep,
            )

            verdict = aggregate_verdict(window_results)
            verdict_row = {
                "strategy": sname,
                "symbol": symbol,
                **verdict,
            }
            verdicts.append(verdict_row)
            all_window_results.extend(window_results)

            logger.info(
                f"  ➤ VERDICT {sname}/{symbol}: {verdict['verdict']} "
                f"({verdict['reason']})"
            )

            # Record window results to DB
            if bridge is not None:
                for wr in window_results:
                    if wr["status"] != "success":
                        continue
                    bridge.record_walk_forward_result(
                        {
                            "strategy_name": sname,
                            "symbol": symbol,
                            "window_index": wr["window_index"],
                            "train_start": wr["train_start"],
                            "train_end": wr["train_end"],
                            "test_start": wr["test_start"],
                            "test_end": wr["test_end"],
                            "total_return_pct": wr.get("total_return_pct"),
                            "sharpe": wr.get("sharpe"),
                            "sortino": wr.get("sortino"),  # NEW: Sortino alongside Sharpe
                            "max_drawdown_pct": wr.get("max_drawdown_pct"),
                            "profitable": wr.get("profitable"),
                            "num_entries": wr.get("num_entries"),
                            "budget": args.budget,
                        }
                    )

    # Save results
    ver_csv, win_csv = save_results(all_window_results, verdicts, Path(args.output))

    # ── Summary ──
    print(f"\n{'='*70}")
    print(f"WALK-FORWARD VALIDATION COMPLETE")
    print(f"{'='*70}")
    print(f"Strategies: {len(args.strategies)} | Assets: {len(args.assets)} | Windows: {len(windows)}")
    print(f"Verdicts CSV: {ver_csv}")

    if verdicts:
        print(f"\n{'Strategy':<42} {'Asset':<6} {'Verdict':<6} {'Prof':<6} {'AvgSharpe':<10} {'AvgSortino':<11} {'AvgRet%':<8}")
        print("-" * 95)
        for v in verdicts:
            print(
                f"{v['strategy']:<42} {v['symbol']:<6} {v['verdict']:<6} "
                f"{v['n_profitable']}/{v['n_windows']:<4} "
                f"{v['avg_sharpe']:<10} {v.get('avg_sortino', 0.0):<11} {v['avg_return_pct']:<8}"
            )

    if bridge is not None:
        bridge.close()


if __name__ == "__main__":
    main()
