"""Tests for the AQS DuckLake retry helper.

The committee bots crashed at 2026-06-25 10:10:20 PDT when concurrent
stratforge sweeps in agentic-quant-os triggered DuckLake's optimistic
concurrency check (CheckForConflicts assertion failure). The fix is a
retry helper with exponential backoff that wraps all execute_write calls.

These tests verify:
1. _retry_write returns True on first-attempt success
2. _retry_write returns True after some failures (then success)
3. _retry_write returns False after exhausting all attempts
4. Backoff schedule is 0.1s, 0.3s, 0.9s (exponential, factor 3)
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch


class AQSRetryTests(unittest.TestCase):
    """B6.6 — verify the DuckLake retry helper behaves correctly."""

    def test_retry_write_succeeds_first_attempt(self):
        """No retries needed when write succeeds on attempt 1."""
        from src.memory.aqs_sync import _retry_write
        client = MagicMock()
        client.execute_write.return_value = None
        ok = _retry_write(client, "INSERT INTO t VALUES (1)", [1])
        self.assertTrue(ok)
        self.assertEqual(client.execute_write.call_count, 1)

    def test_retry_write_succeeds_after_two_failures(self):
        """Two failures, then success — should return True on attempt 3."""
        from src.memory.aqs_sync import _retry_write
        client = MagicMock()
        client.execute_write.side_effect = [
            IOError("DuckLake conflict (simulated)"),
            IOError("DuckLake conflict (simulated)"),
            None,  # third call succeeds
        ]
        with patch("src.memory.aqs_sync._time.sleep") as mock_sleep:
            ok = _retry_write(client, "INSERT INTO t VALUES (1)", [1])
        self.assertTrue(ok)
        self.assertEqual(client.execute_write.call_count, 3)
        # Backoff schedule: 0.1s after attempt 1, 0.3s after attempt 2
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertAlmostEqual(mock_sleep.call_args_list[0].args[0], 0.1, places=4)
        self.assertAlmostEqual(mock_sleep.call_args_list[1].args[0], 0.3, places=4)

    def test_retry_write_returns_false_after_exhausting_attempts(self):
        """All 4 attempts fail — should return False."""
        from src.memory.aqs_sync import _retry_write
        client = MagicMock()
        client.execute_write.side_effect = IOError("persistent DuckLake failure")
        with patch("src.memory.aqs_sync._time.sleep"):
            ok = _retry_write(client, "INSERT INTO t VALUES (1)", [1])
        self.assertFalse(ok)
        self.assertEqual(client.execute_write.call_count, 4)

    def test_retry_write_backoff_schedule(self):
        """Verify the backoff delays: 0.1, 0.3, 0.9 (factor 3, 3 retries)."""
        from src.memory.aqs_sync import _retry_write
        client = MagicMock()
        client.execute_write.side_effect = IOError("always fails")
        with patch("src.memory.aqs_sync._time.sleep") as mock_sleep:
            _retry_write(client, "INSERT INTO t VALUES (1)", [1])
        # 3 sleep calls (between 4 attempts)
        self.assertEqual(mock_sleep.call_count, 3)
        self.assertAlmostEqual(mock_sleep.call_args_list[0].args[0], 0.1, places=4)
        self.assertAlmostEqual(mock_sleep.call_args_list[1].args[0], 0.3, places=4)
        self.assertAlmostEqual(mock_sleep.call_args_list[2].args[0], 0.9, places=4)

    def test_retry_write_with_no_parameters(self):
        """Verify the no-params path works."""
        from src.memory.aqs_sync import _retry_write
        client = MagicMock()
        client.execute_write.return_value = None
        ok = _retry_write(client, "DELETE FROM t WHERE id = 1")
        self.assertTrue(ok)
        # Should call with no parameters kwarg
        client.execute_write.assert_called_once_with("DELETE FROM t WHERE id = 1")


if __name__ == "__main__":
    unittest.main()