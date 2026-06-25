"""
Tests for the Strategy Validation Gate (B6 — 2026-06-25).

Verifies:
1. The pipeline stages all run without exception.
2. Failing strategies get flagged.
3. Passing strategies get approved.
4. The default profile rejects strategies with too-low win rate or
   recovery factor; the loose profile accepts them.
5. Monte Carlo and walk-forward efficiency work end-to-end.
6. Persisted rows land in nexus_results.duckdb.strategy_gate_results.

Run from the AQOS venv (realbt lives there):

    /home/Zev/development/agentic-quant-os/.venv/bin/python -m pytest \\
        /home/Zev/development/nexus-trade/tests/test_b6_strategy_gate.py -x -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make sure we can import realbt from AQOS tools
_AQOS_TOOLS = "/home/Zev/development/agentic-quant-os/tools"
if _AQOS_TOOLS not in sys.path:
    sys.path.insert(0, _AQOS_TOOLS)
_NEXUS_SRC = "/home/Zev/development/nexus-trade/src"
if _NEXUS_SRC not in sys.path:
    sys.path.insert(0, _NEXUS_SRC)

import numpy as np
import pandas as pd

from src.validation.strategy_gate import (
    GateProfile,
    GateResult,
    STRATEGY_GATE_RESULTS_DDL,
    check_is_gates,
    check_mc_gates,
    check_wf_gates,
    load_ohlcv,
    monte_carlo_check,
    run_gate,
    run_per_window_backtest,
    walk_forward_efficiency,
)


def _is_realbt_available() -> bool:
    try:
        import realbt  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_is_realbt_available(), "realbt not installed")
class TestGateHelpers(unittest.TestCase):
    """Unit tests for the gate's pure-function helpers."""

    def test_is_gates_pass(self):
        m = {
            "sortino": 1.5, "profit_factor": 1.5, "max_drawdown": 0.10,
            "win_rate": 0.50, "num_trades": 50, "calmar": 1.0,
            "recovery_factor": 2.0, "sqn": 2.0, "omega_ratio": 1.5,
            "total_return": 0.5,
        }
        # Need to also include derived metrics via _extract
        from src.validation.strategy_gate import _extract_derived_metrics
        d = _extract_derived_metrics(m)
        m.update(d)
        failed = check_is_gates(m, GateProfile())
        self.assertEqual(failed, [], f"Expected no failures, got {failed}")

    def test_is_gates_fail(self):
        m = {
            "sortino": 0.5, "profit_factor": 0.8, "max_drawdown": 0.50,
            "win_rate": 0.20, "num_trades": 5, "calmar": 0.1,
            "recovery_factor": 0.5, "sqn": 0.5, "omega_ratio": 0.5,
            "total_return": -0.2,
        }
        from src.validation.strategy_gate import _extract_derived_metrics
        d = _extract_derived_metrics(m)
        m.update(d)
        failed = check_is_gates(m, GateProfile())
        self.assertGreater(len(failed), 0)
        # Each gate name should appear at least once
        all_text = " ".join(failed)
        for needle in ("sortino", "profit_factor", "max_drawdown", "win_rate",
                       "num_trades", "calmar", "recovery_factor", "sqn",
                       "omega_ratio", "total_return"):
            self.assertIn(needle, all_text, f"Missing {needle} in failures")

    def test_mc_gates_pass(self):
        mc = {"p_loss": 0.10, "p_ruin": 0.02, "cvar95": -0.20, "n_simulations": 10000}
        failed = check_mc_gates(mc, GateProfile())
        self.assertEqual(failed, [])

    def test_mc_gates_fail(self):
        mc = {"p_loss": 0.50, "p_ruin": 0.20, "cvar95": -0.80, "n_simulations": 10000}
        failed = check_mc_gates(mc, GateProfile())
        self.assertEqual(len(failed), 3)

    def test_monte_carlo_low_trades_returns_ones(self):
        """If num_trades < 5, MC returns worst-case (avoid div by zero)."""
        mc = monte_carlo_check({"num_trades": 2}, n_simulations=100)
        self.assertEqual(mc["p_loss"], 1.0)
        self.assertEqual(mc["p_ruin"], 1.0)


@unittest.skipUnless(_is_realbt_available(), "realbt not installed")
class TestGateEndToEnd(unittest.TestCase):
    """End-to-end smoke tests using the live StratForge strategy."""

    DB_PATH = "/home/Zev/development/nexus-trade/data/nexus_results.duckdb"
    STRATEGY_DIR = (
        "/home/Zev/.hermes/profiles/herm-bot/home/agentic-quant-os/"
        "strategies/stratforge/active"
    )

    def setUp(self):
        if not os.path.exists(self.DB_PATH):
            self.skipTest(f"DB not found: {self.DB_PATH}")
        if not os.path.exists(self.STRATEGY_DIR):
            self.skipTest(f"Strategy dir not found: {self.STRATEGY_DIR}")

    def test_load_strategy(self):
        from src.validation.strategy_gate import load_strategy, find_strategy_path
        path = find_strategy_path("meta_cmo_alma_atr_wf_v1")
        strat = load_strategy(path)
        self.assertTrue(callable(strat))

    def test_load_ohlcv(self):
        df = load_ohlcv("BTC")
        self.assertGreater(len(df), 100)
        self.assertIn("close", df.columns)

    def test_walk_forward_efficiency(self):
        from src.validation.strategy_gate import (
            find_strategy_path, load_strategy,
        )
        strat = load_strategy(find_strategy_path("meta_cmo_alma_atr_wf_v1"))
        df = load_ohlcv("BTC")
        wf = walk_forward_efficiency(strat, df, is_fraction=0.7)
        self.assertIn("oos_sortino", wf)
        self.assertIn("oos_max_dd", wf)
        self.assertIn("oos_win_rate", wf)
        self.assertIn("wf_efficiency", wf)

    def test_run_per_window_backtest(self):
        from src.validation.strategy_gate import (
            find_strategy_path, load_strategy,
        )
        strat = load_strategy(find_strategy_path("meta_cmo_alma_atr_wf_v1"))
        df = load_ohlcv("BTC")
        agg, per_window = run_per_window_backtest(
            strat, df, "meta_cmo_alma_atr_wf_v1", "BTC",
            db_path=self.DB_PATH,
        )
        self.assertGreater(len(per_window), 0, "Expected WF windows from DB")
        self.assertGreater(agg.get("num_trades", 0), 0)
        # The strategy has Sortino >= 1.0 on realbt (avg across profitable WF windows)
        self.assertGreater(agg.get("sortino", 0), 1.0)

    def test_run_gate_default_profile_persists(self):
        result = run_gate(
            "meta_cmo_alma_atr_wf_v1", symbol="BTC",
            exchange="binance", profile=GateProfile(name="default"),
            db_path=self.DB_PATH, persist=True, verbose=False,
        )
        # We expect it to either pass (loose profile) or fail (default).
        # Either way the row must be persisted with the right schema.
        self.assertIsNotNone(result.id)
        self.assertGreater(result.num_trades, 0)
        self.assertGreater(result.mc_n_simulations, 0)
        # Verify it landed in the DB
        import duckdb
        con = duckdb.connect(self.DB_PATH, read_only=True)
        try:
            row = con.execute(
                "SELECT passed FROM strategy_gate_results WHERE id = ?",
                [result.id],
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], result.passed)
        finally:
            con.close()

    def test_run_gate_loose_profile_passes(self):
        """The known-good meta_cmo_alma_atr_wf_v1 should pass loose profile."""
        loose = GateProfile(name="loose")
        # CLI's --profile loose also adjusts these; replicate here.
        loose.win_rate = 0.35
        loose.recovery_factor = 0.7
        loose.sqn = 0.8
        loose.sortino = 0.5
        loose.profit_factor = 1.1
        loose.num_trades = 15
        # Per-window trend strategies on 4H BTC/ETH have WR < 40% by
        # design (payoff > 2). Loose profile lowers that + recovery
        # to keep crypto-trend strategies pass-able.
        result = run_gate(
            "meta_cmo_alma_atr_wf_v1", symbol="BTC",
            exchange="binance", profile=loose,
            db_path=self.DB_PATH, persist=True, verbose=False,
        )
        self.assertTrue(result.passed, f"Expected PASS, failed: {result.failed_gates}")
        self.assertGreaterEqual(result.sortino, 0.5)
        self.assertGreaterEqual(result.profit_factor, 1.1)
        self.assertGreaterEqual(result.num_trades, 15)


@unittest.skipUnless(_is_realbt_available(), "realbt not installed")
class TestGateSynthetic(unittest.TestCase):
    """Synthetic strategies that should always pass or always fail."""

    SYNERGY_DIR = tempfile.mkdtemp(prefix="test_strategies_")

    def setUp(self):
        os.makedirs(self.SYNERGY_DIR, exist_ok=True)

    def _write_strategy(self, name: str, always_enter: bool, win_rate: float = 0.9):
        """Write a synthetic strategy file."""
        if always_enter:
            body = """
def strategy(df, params=None):
    import pandas as pd
    n = len(df)
    # Enter every bar, exit next bar (1-trade-per-bar simulation).
    entries = pd.Series([1] * n, index=df.index)
    exits = pd.Series([1] * n, index=df.index)
    return entries, exits
"""
        else:
            body = """
def strategy(df, params=None):
    import pandas as pd
    n = len(df)
    entries = pd.Series([0] * n, index=df.index)
    exits = pd.Series([0] * n, index=df.index)
    return entries, exits
"""
        path = os.path.join(self.SYNERGY_DIR, f"{name}.py")
        with open(path, "w") as f:
            f.write(body)
        return path

    def test_no_signals_strategy_fails(self):
        """A strategy that never trades should fail (no trades = gate fail)."""
        self._write_strategy("never_trades", always_enter=False)
        result = run_gate(
            "never_trades", symbol="BTC",
            strategy_dir=self.SYNERGY_DIR,
            ohlcv_dir="/home/Zev/development/nexus-trade/data/4h_cache",
            db_path="/home/Zev/development/nexus-trade/data/nexus_results.duckdb",
            persist=False, profile=GateProfile(name="default"),
        )
        # realbt returns None for no-trade strategies; the gate flags
        # that as "backtest_returned_none" rather than num_trades=0.
        self.assertFalse(result.passed)
        self.assertTrue(
            any("backtest_returned_none" in f or "num_trades<" in f
                for f in result.failed_gates),
            f"Expected backtest/num_trades gate failure, got {result.failed_gates}",
        )


if __name__ == "__main__":
    unittest.main()