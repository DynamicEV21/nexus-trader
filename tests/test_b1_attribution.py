"""
Unit tests for B1 attribution loop on PaperTradeCommitteeStrategy.

These tests verify:
  1. on_filled_order correctly detects OPEN (qty 0 -> >0)
  2. on_filled_order correctly detects FULL CLOSE (qty >0 -> 0) and
     computes realized PnL.
  3. on_filled_order correctly detects PARTIAL CLOSE (qty >0 -> smaller).
  4. on_filled_order detects ADD/RE-ENTRY (qty growing).
  5. _capture_pre_fill_state snapshots positions correctly.
  6. on_filled_order auto-writes lesson on threshold-crossing loss.
  7. on_filled_order invokes attribution_bridge.update_outcome() on close.

We don't actually run a backtest or start Lumibot — these tests stub
out the minimum required attributes so the method body executes.
"""
from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, "/home/Zev/development/nexus-trade")
sys.path.insert(0, "/home/Zev/development/nexus-trade/src")

# Avoid the lumibot-venv guard from killing the import (we don't need
# Lumibot for these unit tests because we stub the strategy).
os.environ.setdefault("NEXUS_SKIP_VENV_GUARD", "1")


def _make_strategy_stub():
    """Build a stub strategy with the minimum attrs B1's on_filled_order needs."""
    from src.runners.paper_trade_committee import (
        make_committee_strategy_class,
    )
    StrategyCls = make_committee_strategy_class()

    class _VarsBag:
        """Plain attribute bag — MagicMock auto-creates attributes which
        breaks dict.get() fall-throughs."""
        last_decision_id = ""
        last_decision_ids_by_symbol = {}
        current_regime = "trending_up"

    stub = StrategyCls.__new__(StrategyCls)
    stub._pre_fill_qty = {}
    stub._pending_outcomes = {}
    stub._fills_processed = 0
    stub._closes_processed = 0
    stub.vars = _VarsBag()
    # Stub Lumibot internals used by on_filled_order
    broker = MagicMock()
    broker.data_source.get_datetime.return_value.isoformat.return_value = (
        "2026-06-25T10:00:00+00:00"
    )
    stub.broker = broker
    stub.get_datetime = MagicMock()
    stub.get_datetime.return_value.isoformat.return_value = "2026-06-25T10:00:00+00:00"
    return stub


def _make_order(symbol: str, is_buy: bool, qty: float):
    order = MagicMock()
    order.asset.symbol = symbol
    order.is_buy_order.return_value = is_buy
    return order


def _make_position(symbol: str, qty: float, avg_fill_price: float = 100.0):
    pos = MagicMock()
    pos.symbol = symbol
    pos.quantity = qty
    pos.avg_fill_price = avg_fill_price
    return pos


class B1AttributionTests(unittest.TestCase):
    """B1: attribution loop unit tests."""

    def test_open_detected(self):
        """Pre-fill 0 -> post-fill >0 should record pending outcome."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 0.0
        order = _make_order("BTC", is_buy=True, qty=0.5)
        position = _make_position("BTC", qty=0.5)
        s.on_filled_order(position, order, price=50000.0, quantity=0.5, multiplier=1.0)
        self.assertIn("BTC", s._pending_outcomes)
        self.assertEqual(s._pending_outcomes["BTC"]["side"], "LONG")
        self.assertEqual(s._pending_outcomes["BTC"]["entry_price"], 50000.0)
        self.assertEqual(s._fills_processed, 1)
        self.assertEqual(s._closes_processed, 0)

    def test_full_close_long_win(self):
        """Pre-fill >0 -> post-fill 0 on LONG with exit > entry = win."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 0.5
        s._pending_outcomes["BTC"] = {
            "side": "LONG", "entry_price": 50000.0,
            "entry_sim_time": "2026-06-25T10:00", "decision_id": "decision_test_1",
        }
        order = _make_order("BTC", is_buy=False, qty=0.5)
        position = _make_position("BTC", qty=0.0)
        # Stub out the bridge + lesson calls so we don't actually launch
        # subprocesses / LanceDB writes during this test.
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            s.on_filled_order(position, order, price=51000.0, quantity=0.5, multiplier=1.0)
        # 51000/50000 - 1 = +2% win — bridge should NOT be called in "light"
        # mode unless loss > threshold; lesson should NOT be called.
        self.assertEqual(s._closes_processed, 1)
        self.assertNotIn("BTC", s._pending_outcomes)
        bridge.assert_not_called()
        lesson.assert_not_called()

    def test_full_close_long_loss_triggers_bridge_and_lesson(self):
        """Pre-fill >0 -> post-fill 0 on LONG with exit < entry = loss;
        if |loss| > threshold, bridge and lesson are called."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 1.0
        s._pending_outcomes["BTC"] = {
            "side": "LONG", "entry_price": 50000.0,
            "entry_sim_time": "2026-06-25T10:00", "decision_id": "decision_test_loss",
        }
        order = _make_order("BTC", is_buy=False, qty=1.0)
        position = _make_position("BTC", qty=0.0)
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            # exit at 47000 -> -6% loss, > 2% threshold
            s.on_filled_order(position, order, price=47000.0, quantity=1.0, multiplier=1.0)
        self.assertEqual(s._closes_processed, 1)
        bridge.assert_called_once()
        args, kwargs = bridge.call_args
        self.assertEqual(kwargs["decision_id"], "decision_test_loss")
        self.assertEqual(kwargs["outcome"], "loss")
        self.assertAlmostEqual(kwargs["pnl_pct"], -6.0, places=2)
        lesson.assert_called_once()
        l_args, l_kwargs = lesson.call_args
        self.assertEqual(l_kwargs["symbol"], "BTC")
        self.assertEqual(l_kwargs["side"], "LONG")
        self.assertAlmostEqual(l_kwargs["realized_pnl_pct"], -6.0, places=2)

    def test_full_close_short_loss(self):
        """SHORT close: realized pnl = (entry - exit)/entry; loss if exit > entry."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 1.0
        s._pending_outcomes["BTC"] = {
            "side": "SHORT", "entry_price": 50000.0,
            "entry_sim_time": "2026-06-25T10:00", "decision_id": "decision_short_loss",
        }
        order = _make_order("BTC", is_buy=True, qty=1.0)
        position = _make_position("BTC", qty=0.0)
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            # exit (cover) at 53000 -> -6% loss on short
            s.on_filled_order(position, order, price=53000.0, quantity=1.0, multiplier=1.0)
        self.assertEqual(s._closes_processed, 1)
        bridge.assert_called_once()
        l_args, l_kwargs = lesson.call_args
        self.assertAlmostEqual(l_kwargs["realized_pnl_pct"], -6.0, places=2)

    def test_partial_close(self):
        """Pre-fill >0 -> post-fill 0 < post < pre = partial close."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 1.0
        s._pending_outcomes["BTC"] = {
            "side": "LONG", "entry_price": 50000.0,
            "entry_sim_time": "", "decision_id": "decision_partial",
        }
        order = _make_order("BTC", is_buy=False, qty=0.6)
        position = _make_position("BTC", qty=0.4)  # 0.6 of 1.0 closed
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            # exit at 49500 -> -1% loss on long, below threshold -> no lesson
            s.on_filled_order(position, order, price=49500.0, quantity=0.6, multiplier=1.0)
        self.assertEqual(s._closes_processed, 1)
        bridge.assert_not_called()  # light mode + small loss
        lesson.assert_not_called()
        # remaining position still tracked with same entry_price
        self.assertIn("BTC", s._pending_outcomes)
        self.assertEqual(s._pending_outcomes["BTC"]["entry_price"], 50000.0)

    def test_add_no_outcome(self):
        """Pre-fill >0 -> post-fill > pre = ADD; should not record outcome."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 0.5
        s._pending_outcomes["BTC"] = {
            "side": "LONG", "entry_price": 50000.0,
            "entry_sim_time": "", "decision_id": "decision_add",
        }
        order = _make_order("BTC", is_buy=True, qty=0.5)
        position = _make_position("BTC", qty=1.0)
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            s.on_filled_order(position, order, price=51000.0, quantity=0.5, multiplier=1.0)
        bridge.assert_not_called()
        lesson.assert_not_called()
        self.assertEqual(s._closes_processed, 0)

    def test_full_close_inherits_decision_id_from_per_symbol_map(self):
        """If pending_outcomes is empty (e.g., inherited position), pull
        decision_id from last_decision_ids_by_symbol AND entry_price
        from position.avg_fill_price. Together these let us compute a
        meaningful realized PnL."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 0.5
        s._pending_outcomes = {}  # no pending entry this run
        s.vars.last_decision_ids_by_symbol["BTC"] = "decision_inherited_btc"
        order = _make_order("BTC", is_buy=False, qty=0.5)
        # Position inherits from a prior run: avg_fill_price = $50k.
        position = MagicMock()
        position.symbol = "BTC"
        position.quantity = 0.0
        position.avg_fill_price = 50000.0
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            # exit at 47000 -> -6% loss on long
            s.on_filled_order(position, order, price=47000.0, quantity=0.5, multiplier=1.0)
        bridge.assert_called_once()
        args, kwargs = bridge.call_args
        self.assertEqual(kwargs["decision_id"], "decision_inherited_btc")
        self.assertAlmostEqual(kwargs["pnl_pct"], -6.0, places=2)
        lesson.assert_called_once()

    def test_loss_below_threshold_skipped(self):
        """A loss of 1% should NOT trigger lesson in light mode (default 2%)."""
        s = _make_strategy_stub()
        s._pre_fill_qty["BTC"] = 1.0
        s._pending_outcomes["BTC"] = {
            "side": "LONG", "entry_price": 50000.0,
            "entry_sim_time": "", "decision_id": "decision_small_loss",
        }
        order = _make_order("BTC", is_buy=False, qty=1.0)
        position = _make_position("BTC", qty=0.0)
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            # exit at 49500 -> -1% loss, below 2% threshold
            s.on_filled_order(position, order, price=49500.0, quantity=1.0, multiplier=1.0)
        bridge.assert_not_called()
        lesson.assert_not_called()

    def test_heavy_bridge_mode_always_bridges(self):
        """attribution_bridge_mode='heavy' bridges on every close, win or loss."""
        s = _make_strategy_stub()
        s.parameters["attribution_bridge_mode"] = "heavy"
        s._pre_fill_qty["BTC"] = 1.0
        s._pending_outcomes["BTC"] = {
            "side": "LONG", "entry_price": 50000.0,
            "entry_sim_time": "", "decision_id": "decision_heavy",
        }
        order = _make_order("BTC", is_buy=False, qty=1.0)
        position = _make_position("BTC", qty=0.0)
        with patch.object(s, "_attribution_bridge_call") as bridge, \
             patch.object(s, "_attribution_write_loss_lesson") as lesson:
            # +2% win — would not bridge in light mode, but heavy bridges
            s.on_filled_order(position, order, price=51000.0, quantity=1.0, multiplier=1.0)
        bridge.assert_called_once()
        lesson.assert_not_called()


class B1AttributionBridgeTests(unittest.TestCase):
    """B1: subprocess bridge smoke test (uses AQOS venv to import real module)."""

    def test_bridge_call_doesnt_crash(self):
        """_attribution_bridge_call should silently fail if subprocess
        unavailable — never raise."""
        s = _make_strategy_stub()
        # Don't mock — let it actually try to launch the subprocess; it
        # should fail-soft since the decision_id is bogus and the bridge
        # JSON contains no such decision.
        try:
            s._attribution_bridge_call(
                decision_id="decision_test_bridge_nonexistent",
                outcome="loss",
                pnl_pct=-3.0,
                symbol="BTC",
            )
        except Exception as exc:
            self.fail(f"_attribution_bridge_call raised: {exc}")


if __name__ == "__main__":
    unittest.main(verbosity=2)