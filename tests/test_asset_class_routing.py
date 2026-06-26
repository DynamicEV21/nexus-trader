"""Asset-class routing for lakehouse views (2026-06-26).

Verifies that ``get_strategy_pool`` + ``lakehouse_strategy_candidates``
correctly route to ``v_nexus_strategy_pool_crypto`` /
``v_nexus_strategy_pool_stocks`` based on the ``asset_class`` parameter,
and that the asof macros work for both pools. Also verifies the
view_migration ``check_missing_views`` accepts the asset-class views
as present after migration.
"""
from __future__ import annotations

import os
import sys
import unittest

# Ensure src on path regardless of cwd
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


class TestAssetClassRouting(unittest.TestCase):
    """Verify reader + tools + migration handle asset_class routing."""

    @classmethod
    def setUpClass(cls) -> None:
        # Suppress the vector-memory guard's RuntimeError by guarding
        # the import behind a try/except. The reader works without
        # the committee module which is what fires the guard.
        try:
            from src.lakehouse.reader import get_reader
            cls.reader = get_reader()
        except RuntimeError as exc:
            if "vector memory" in str(exc).lower():
                raise unittest.SkipTest(
                    f"Skipping: lumibot venv has lancedb installed ({exc})"
                ) from exc
            raise

    def test_crypto_pool_returns_rows(self) -> None:
        """v_nexus_strategy_pool_crypto has rows (BTC/ETH/SOL universe)."""
        rows = self.reader.get_strategy_pool(
            asset_class="crypto", min_composite=0, limit=5,
        )
        self.assertGreater(len(rows), 0, "crypto pool should have rows")
        # Crypto rows have ticker in BTC/ETH/SOL/ALL/MULTI
        tickers = {r.get("ticker") for r in rows}
        self.assertTrue(
            tickers & {"BTC", "ETH", "SOL", "ALL", "MULTI", "BTC/USDT", "ETH/USDT", "SOL/USDT"},
            f"crypto pool has no crypto ticker: {tickers}",
        )

    def test_stocks_pool_returns_rows(self) -> None:
        """v_nexus_strategy_pool_stocks has 20 MAS strategies."""
        rows = self.reader.get_strategy_pool(
            asset_class="stocks", min_composite=0, limit=5,
        )
        self.assertGreater(len(rows), 0, "stocks pool should have rows")
        # Stocks rows have ticker='STOCKS' (the curated view's projection)
        tickers = {r.get("ticker") for r in rows}
        self.assertIn("STOCKS", tickers, f"stocks pool missing STOCKS ticker: {tickers}")

    def test_default_asset_class_is_crypto(self) -> None:
        """No asset_class → defaults to crypto (back-compat)."""
        rows_default = self.reader.get_strategy_pool(min_composite=0, limit=3)
        rows_explicit = self.reader.get_strategy_pool(
            asset_class="crypto", min_composite=0, limit=3,
        )
        self.assertEqual(
            [r.get("strategy_name") for r in rows_default],
            [r.get("strategy_name") for r in rows_explicit],
        )

    def test_legacy_asset_class_empty_returns_legacy_pool(self) -> None:
        """asset_class='' falls back to v_nexus_strategy_pool."""
        rows = self.reader.get_strategy_pool(asset_class="", min_composite=0, limit=3)
        self.assertGreater(len(rows), 0, "legacy pool should have rows")

    def test_asof_macro_works_for_crypto(self) -> None:
        """v_nexus_strategy_pool_crypto_asof macro applies created_at filter."""
        rows = self.reader.get_strategy_pool(
            asset_class="crypto",
            as_of="2026-06-26T00:00:00",
            min_composite=0,
            limit=3,
        )
        self.assertGreater(len(rows), 0, "crypto + as_of should have rows")

    def test_asof_macro_works_for_stocks(self) -> None:
        """v_nexus_strategy_pool_stocks_asof macro applies created_at filter."""
        rows = self.reader.get_strategy_pool(
            asset_class="stocks",
            as_of="2026-06-26T00:00:00",
            min_composite=0,
            limit=3,
        )
        self.assertGreater(len(rows), 0, "stocks + as_of should have rows")

    def test_stocks_ranking_falls_back_to_sharpe(self) -> None:
        """Stocks pool has NULL sortino; reader ranks by sharpe DESC."""
        rows = self.reader.get_strategy_pool(
            asset_class="stocks", min_composite=0, sort_by="sortino", limit=5,
        )
        self.assertGreater(len(rows), 0)
        # Verify rows are sharpe-sorted DESC (since sortino is NULL)
        sharpes = [r.get("sharpe") for r in rows if r.get("sharpe") is not None]
        if len(sharpes) >= 2:
            self.assertEqual(
                sharpes, sorted(sharpes, reverse=True),
                "stocks with sort_by=sortino should fall back to sharpe DESC",
            )

    def test_migration_recognizes_asset_class_views(self) -> None:
        """check_missing_views accepts the crypto/stocks views + asof macros."""
        try:
            from src.lakehouse.view_migration import (
                NEXUS_ASSET_CLASS_VIEW_NAMES,
                NEXUS_ASOF_MACRO_NAMES,
                check_missing_views,
            )
        except RuntimeError as exc:
            if "vector memory" in str(exc).lower():
                self.skipTest(f"venv guard: {exc}")
                return
            raise

        missing = check_missing_views()
        # The asset-class views + their asof macros MUST NOT be in the
        # missing set after a successful migration. Legacy asof macros
        # (referencing views without as_of_timestamp) ARE expected to be
        # missing and are NOT in scope for this test.
        for required in (
            "v_nexus_strategy_pool_crypto",
            "v_nexus_strategy_pool_stocks",
            "v_nexus_strategy_pool_crypto_asof",
            "v_nexus_strategy_pool_stocks_asof",
        ):
            self.assertNotIn(
                required, missing,
                f"{required} should be installed after view migration",
            )


class TestLakehouseToolsAssetClass(unittest.TestCase):
    """Verify nexus_tools.lakehouse_strategy_candidates routes asset_class."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            # Import the module so we can reach the tool via the module
            # namespace. Storing the function as a class attribute
            # makes Python bind it as a method on instance access,
            # which corrupts the call signature. Module-level access
            # avoids that.
            import src.lakehouse.nexus_tools as _nt
            cls._nt = _nt
        except RuntimeError as exc:
            if "vector memory" in str(exc).lower():
                raise unittest.SkipTest(
                    f"Skipping: lumibot venv has lancedb installed ({exc})"
                ) from exc
            raise

    def _call(self, **kwargs):
        # The tool reads ``_get_sim_time()`` from the strategy context.
        # In tests there's no live strategy, so we register a fixed
        # sim_time before each call (and clear after).
        from src.tools import _strategy_context as sc
        sc.register_sim_time("2026-06-26T00:00:00")
        try:
            return self._nt.lakehouse_strategy_candidates(**kwargs)
        finally:
            sc.clear_sim_time()

    def test_crypto_default(self) -> None:
        result = self._call(asset_class="crypto", limit=3)
        self.assertIn("strategies", result)
        self.assertIn("asset_class", result)
        self.assertEqual(result["asset_class"], "crypto")
        self.assertGreater(len(result["strategies"]), 0)

    def test_stocks_explicit(self) -> None:
        result = self._call(asset_class="stocks", limit=3)
        self.assertEqual(result["asset_class"], "stocks")
        self.assertGreater(len(result["strategies"]), 0)

    def test_stocks_auto_min_composite(self) -> None:
        """Stocks should NOT silently drop rows because of crypto default."""
        # If we pass the default min_composite=49, the tool should
        # auto-relax to 0 for stocks.
        result_default = self._call(asset_class="stocks", limit=3)
        result_explicit = self._call(
            asset_class="stocks", min_composite=0, limit=3,
        )
        self.assertEqual(
            len(result_default["strategies"]),
            len(result_explicit["strategies"]),
            "stocks auto-relax of min_composite should match explicit 0",
        )


if __name__ == "__main__":
    unittest.main()