"""
View migration — ensure the 4 Nexus curated views + asof macros exist.

Background
----------
The agentic-quant-os ``schema.py`` defines 4 nexus curated views
(``v_nexus_regime_strategy_map``, ``v_nexus_catalyst_digest``,
``v_nexus_failures``, ``v_nexus_experience``) that are created in DuckLake
when ``get_all_ddl()`` is run. The standalone ``quant.duckdb`` file
inherits the underlying tables (``regime_strategy_map``, ``catalyst_grades``,
``failures``, ``experience_bank``) but NOT the views, so direct readers
see ``CatalogException: v_nexus_regime_strategy_map does not exist`` and
log warnings (cosmetic, but noisy).

This module is a one-shot migration: it re-creates the 4 views in any
open DuckDB connection (write mode). It is idempotent (``CREATE OR
REPLACE VIEW``) and safe to call multiple times.

B2 anti-leakage (2026-06-25): also installs 5 ``*_asof`` macros
(``v_nexus_strategy_pool_asof``, ``v_nexus_regime_strategy_map_asof``,
``v_nexus_catalyst_digest_asof``, ``v_nexus_failures_asof``,
``v_nexus_experience_asof``) that wrap the corresponding view with an
``as_of`` cutoff parameter. The agent tools read the active sim-time
from ``_strategy_context.get_sim_time()`` and call these macros so
the LLM never sees rows that were created AFTER the current decision
bar (look-ahead bias prevention).

The migration is invoked automatically by ``NexusLakehouseReader._con_get()``
on first read after a fresh ``quant.duckdb`` is created, OR by the
``AQSWriter`` on first write, OR directly via the CLI:

    python -m src.lakehouse.view_migration
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# The 4 nexus curated views that are NOT auto-created in quant.duckdb.
# Schema mirrors agentic-quant-os/src/schema.py:NEXUS_CURATED_VIEWS, with
# fallbacks for column name drift between the local quant.duckdb and
# the DuckLake instance. Each entry is a list of DDL candidates; the
# migration tries them in order and uses the first that succeeds.
NEXUS_CURATED_VIEW_DDL: list[list[str]] = [
    # 5. Regime-strategy mapping: which strategies historically work in which
    # regimes. Upstream uses sample_count + avg_sharpe + avg_sortino; the
    # local table has n_trades + sharpe_ratio + sortino_ratio. Sortino is
    # the primary ranking metric (penalizes only downside volatility, which
    # is what we care about for asymmetric crypto payoffs). Sharpe is the
    # tiebreaker when two strategies share the same Sortino (rare but
    # useful when a strategy's downside is constant and Sortino rounds).
    # Try upstream first (DuckLake), fall back to local names (quant.duckdb).
    [
        """
        CREATE OR REPLACE VIEW v_nexus_regime_strategy_map AS
        SELECT * FROM regime_strategy_map
        WHERE sample_count >= 5 AND avg_sortino IS NOT NULL AND avg_sortino > 0
        ORDER BY avg_sortino DESC NULLS LAST, avg_sharpe DESC NULLS LAST
        """,
        """
        CREATE OR REPLACE VIEW v_nexus_regime_strategy_map AS
        SELECT * FROM regime_strategy_map
        WHERE n_trades >= 5
          AND sharpe_ratio IS NOT NULL
          AND sortino_ratio IS NOT NULL
          AND sortino_ratio > 0
        ORDER BY sortino_ratio DESC NULLS LAST, sharpe_ratio DESC NULLS LAST
        """,
    ],
    # 6. Catalyst digest — latest catalyst grades
    [
        """
        CREATE OR REPLACE VIEW v_nexus_catalyst_digest AS
        SELECT DISTINCT ON (ticker) *
        FROM catalyst_grades
        WHERE score IS NOT NULL
        ORDER BY ticker, timestamp DESC
        """,
    ],
    # 7. Failure memory for Nexus preflight checks
    [
        """
        CREATE OR REPLACE VIEW v_nexus_failures AS
        SELECT * FROM failures
        ORDER BY timestamp DESC
        """,
    ],
    # 8. Experience bank entries from quant projects (not Nexus itself)
    [
        """
        CREATE OR REPLACE VIEW v_nexus_experience AS
        SELECT * FROM experience_bank
        WHERE source_repo IN (
            'regime-intelligence', 'alpha-factory',
            'quant-loop-testnet', 'alpha-lab', 'quant-research-mas'
        )
           AND severity IN ('warning', 'critical', 'info')
        ORDER BY created_at DESC
        """,
    ],
]


# B2 anti-leakage (2026-06-25): ``*_asof`` macros. Each macro wraps the
# corresponding view and adds a timestamp cutoff. The agent tools read
# the active sim-time and pass it through here.
#
# Each macro accepts a single ``as_of`` argument (ISO datetime string)
# and filters rows whose ``created_at`` column is NULL or <= the bound.
#
# Asset-class split (2026-06-26): the strategy pool is split into
# ``v_nexus_strategy_pool_crypto`` (BTC/ETH/SOL universe, source_repo
# excluding quant-research-mas) and ``v_nexus_strategy_pool_stocks``
# (TradFi universe — reads from the ``strategies`` base table where
# ``type='stocks'``). Both expose the same column shape so the lakehouse
# reader can iterate them uniformly. Both have their own asof macro.
#
# Note: the legacy ``v_nexus_strategy_pool_asof`` macro and the unsuffixed
# ``v_nexus_strategy_pool`` view are kept for backward compatibility but
# new agent code MUST read from ``*_crypto`` / ``*_stocks`` explicitly.
NEXUS_ASOF_MACRO_DDL: list[str] = [
    # Legacy unsuffixed — kept for callers that haven't migrated yet.
    # Filters by date_end (the legacy view doesn't expose created_at).
    """
    CREATE OR REPLACE MACRO v_nexus_strategy_pool_asof(as_of) AS TABLE
    SELECT * FROM v_nexus_strategy_pool
    WHERE date_end IS NULL OR TRY_CAST(date_end AS DATE) <= TRY_CAST(as_of AS DATE)
    """,
    # Crypto asof (uses created_at synthesized from date_start in the view)
    """
    CREATE OR REPLACE MACRO v_nexus_strategy_pool_crypto_asof(as_of) AS TABLE
    SELECT * FROM v_nexus_strategy_pool_crypto
    WHERE created_at IS NULL OR created_at <= CAST(as_of AS TIMESTAMP)
    """,
    # Stocks asof (uses created_at from the strategies base table)
    """
    CREATE OR REPLACE MACRO v_nexus_strategy_pool_stocks_asof(as_of) AS TABLE
    SELECT * FROM v_nexus_strategy_pool_stocks
    WHERE created_at IS NULL OR created_at <= CAST(as_of AS TIMESTAMP)
    """,
    # Other curated views
    """
    CREATE OR REPLACE MACRO v_nexus_regime_strategy_map_asof(as_of) AS
    SELECT * FROM v_nexus_regime_strategy_map
    WHERE (as_of_timestamp IS NULL OR as_of_timestamp <= to_timestamp(as_of))
    """,
    """
    CREATE OR REPLACE MACRO v_nexus_catalyst_digest_asof(as_of) AS
    SELECT * FROM v_nexus_catalyst_digest
    WHERE (as_of_timestamp IS NULL OR as_of_timestamp <= to_timestamp(as_of))
    """,
    """
    CREATE OR REPLACE MACRO v_nexus_failures_asof(as_of) AS
    SELECT * FROM v_nexus_failures
    WHERE (as_of_timestamp IS NULL OR as_of_timestamp <= to_timestamp(as_of))
    """,
    """
    CREATE OR REPLACE MACRO v_nexus_experience_asof(as_of) AS
    SELECT * FROM v_nexus_experience
    WHERE (as_of_timestamp IS NULL OR as_of_timestamp <= to_timestamp(as_of))
    """,
]
NEXUS_ASOF_MACRO_NAMES: tuple[str, ...] = (
    "v_nexus_strategy_pool_asof",
    "v_nexus_strategy_pool_crypto_asof",
    "v_nexus_strategy_pool_stocks_asof",
    "v_nexus_regime_strategy_map_asof",
    "v_nexus_catalyst_digest_asof",
    "v_nexus_failures_asof",
    "v_nexus_experience_asof",
)

# Asset-class split views (2026-06-26). Crypto filters the existing
# v_nexus_strategy_pool (BTC/ETH/SOL universe); stocks reads from the
# ``strategies`` base table where ``type='stocks'`` and projects a
# compatible column shape. Both views add a ``created_at`` column for
# the asof macros above.
#
# IMPORTANT: there are TWO places these views need to live — the local
# ``quant.duckdb`` file (used for testing / standalone scripts) AND the
# DuckLake Postgres catalog (the production read path; the
# ``ducklake_redirect`` shim in ``src.lakehouse.reader`` routes nexus-trade
# reads through DuckLake). The migration installs to BOTH targets so the
# split is consistent regardless of which DB the caller hits.
NEXUS_ASSET_CLASS_VIEWS: list[str] = [
    # Crypto — same shape as the legacy pool but with created_at synthesized
    # Uses source_repo and ticker to identify crypto rows (the local
    # quant.duckdb populates these from backtest_results_v2; DuckLake's
    # pool is built from strategies where non-stock rows are crypto).
    """
    CREATE OR REPLACE VIEW v_nexus_strategy_pool_crypto AS
    SELECT *, TRY_CAST(date_start AS TIMESTAMP) AS created_at
    FROM v_nexus_strategy_pool
    WHERE COALESCE(source_repo,'') != 'quant-research-mas'
       OR ticker IN ('BTC','ETH','SOL','BTC/USDT','ETH/USDT','SOL/USDT','MULTI','ALL')
    """,
    # Stocks — projection from strategies base table
    """
    CREATE OR REPLACE VIEW v_nexus_strategy_pool_stocks AS
    SELECT
        name            AS strategy_name,
        'STOCKS'        AS ticker,
        NULL::DOUBLE    AS composite_score,
        backtest_sharpe AS sharpe,
        NULL::DOUBLE    AS sortino,
        NULL::DOUBLE    AS calmar,
        NULL::DOUBLE    AS profit_factor,
        backtest_max_dd AS max_drawdown,
        NULL::DOUBLE    AS win_rate,
        NULL::INTEGER   AS num_trades,
        backtest_return AS total_return,
        NULL::DOUBLE    AS avg_win,
        NULL::DOUBLE    AS avg_loss,
        NULL::BOOLEAN   AS wf_pass,
        NULL::DOUBLE    AS wf_test_sharpe,
        NULL::DOUBLE    AS avg_test_sharpe,
        NULL::INTEGER   AS n_windows,
        NULL::DOUBLE    AS wf_consistency,
        COALESCE(strategy_category, type) AS archetype,
        NULL::VARCHAR   AS regime_label,
        NULL::VARCHAR   AS regime_best_tag,
        NULL::DOUBLE    AS regime_best_sharpe,
        params_json     AS params_json,
        NULL::VARCHAR   AS source_code,
        status          AS status,
        NULL::VARCHAR   AS fail_reasons,
        source_repo     AS source_repo,
        COALESCE(is_canonical, false) AS is_best_version,
        NULL::INTEGER   AS indicator_count,
        NULL::BOOLEAN   AS has_regime_filter,
        NULL::BOOLEAN   AS has_volume,
        NULL::BOOLEAN   AS has_atr_stop,
        NULL::VARCHAR   AS entry_logic_type,
        NULL::VARCHAR   AS exit_logic_type,
        NULL::VARCHAR   AS specialist_regime,
        quality_score   AS robustness_score,
        NULL::DOUBLE    AS holding_period_avg,
        NULL::DOUBLE    AS exposure_pct,
        NULL::DATE      AS date_start,
        NULL::DATE      AS date_end,
        created_at      AS created_at
    FROM strategies
    WHERE type = 'stocks'
    """,
]
NEXUS_ASSET_CLASS_VIEW_NAMES: tuple[str, ...] = (
    "v_nexus_strategy_pool_crypto",
    "v_nexus_strategy_pool_stocks",
)


# DuckLake-specific variant of the asset-class views. The DuckLake
# catalog uses a different source table layout (the ``strategies`` base
# table is the canonical strategy registry there; ``backtest_results_v2``
# has per-backtest rows with an ``asset_class`` column but is not used by
# the existing ``v_nexus_strategy_pool`` view). We use ``type != 'stocks'``
# as the crypto filter (the only non-stock strategies in DuckLake are
# crypto), and the explicit ``strategies WHERE type='stocks'`` for stocks.
NEXUS_ASSET_CLASS_VIEWS_DUCKLAKE: list[str] = [
    """
    CREATE OR REPLACE VIEW v_nexus_strategy_pool_crypto AS
    SELECT *, created_at
    FROM v_nexus_strategy_pool
    WHERE COALESCE(type,'') != 'stocks'
    """,
    """
    CREATE OR REPLACE VIEW v_nexus_strategy_pool_stocks AS
    SELECT
        name            AS strategy_name,
        'STOCKS'        AS ticker,
        CAST(NULL AS DOUBLE) AS composite_score,
        backtest_sharpe AS sharpe,
        CAST(NULL AS DOUBLE) AS sortino,
        CAST(NULL AS DOUBLE) AS calmar,
        CAST(NULL AS DOUBLE) AS profit_factor,
        backtest_max_dd AS max_drawdown,
        CAST(NULL AS DOUBLE) AS win_rate,
        CAST(NULL AS INTEGER) AS num_trades,
        backtest_return AS total_return,
        CAST(NULL AS DOUBLE) AS avg_win,
        CAST(NULL AS DOUBLE) AS avg_loss,
        CAST(NULL AS BOOLEAN) AS wf_pass,
        CAST(NULL AS DOUBLE) AS wf_test_sharpe,
        CAST(NULL AS DOUBLE) AS avg_test_sharpe,
        CAST(NULL AS INTEGER) AS n_windows,
        CAST(NULL AS DOUBLE) AS wf_consistency,
        COALESCE(strategy_category, type) AS archetype,
        CAST(NULL AS VARCHAR) AS regime_label,
        CAST(NULL AS VARCHAR) AS regime_best_tag,
        CAST(NULL AS DOUBLE) AS regime_best_sharpe,
        params_json     AS params_json,
        CAST(NULL AS VARCHAR) AS source_code,
        status          AS status,
        CAST(NULL AS VARCHAR) AS fail_reasons,
        source_repo     AS source_repo,
        COALESCE(is_canonical, CAST('f' AS BOOLEAN)) AS is_best_version,
        CAST(NULL AS INTEGER) AS indicator_count,
        CAST(NULL AS BOOLEAN) AS has_regime_filter,
        CAST(NULL AS BOOLEAN) AS has_volume,
        CAST(NULL AS BOOLEAN) AS has_atr_stop,
        CAST(NULL AS VARCHAR) AS entry_logic_type,
        CAST(NULL AS VARCHAR) AS exit_logic_type,
        CAST(NULL AS VARCHAR) AS specialist_regime,
        quality_score   AS robustness_score,
        CAST(NULL AS DOUBLE) AS holding_period_avg,
        CAST(NULL AS DOUBLE) AS exposure_pct,
        CAST(NULL AS DATE) AS date_start,
        CAST(NULL AS DATE) AS date_end,
        created_at      AS created_at
    FROM strategies
    WHERE type = 'stocks'
    """,
]
NEXUS_ASSET_CLASS_VIEWS_DUCKLAKE_MACROS: list[str] = [
    """
    CREATE OR REPLACE MACRO v_nexus_strategy_pool_crypto_asof(as_of) AS TABLE
    SELECT * FROM v_nexus_strategy_pool_crypto
    WHERE created_at IS NULL OR created_at <= CAST(as_of AS TIMESTAMP)
    """,
    """
    CREATE OR REPLACE MACRO v_nexus_strategy_pool_stocks_asof(as_of) AS TABLE
    SELECT * FROM v_nexus_strategy_pool_stocks
    WHERE created_at IS NULL OR created_at <= CAST(as_of AS TIMESTAMP)
    """,
]


def ensure_views(con: Any, view_ddl: list[list[str]] | None = None) -> int:
    """Ensure the 4 nexus curated views exist in the given connection.

    Idempotent (``CREATE OR REPLACE``). Returns the number of views
    successfully created or replaced. Skips silently if the underlying
    tables don't exist (e.g. on a fresh empty quant.duckdb where the
    tables will be created by the next AQS agent sync).

    Each entry in ``view_ddl`` is a list of DDL candidates; the function
    tries them in order and uses the first that succeeds (so we can
    tolerate column-name drift between DuckLake and the local DB).

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        An open writable DuckDB connection. The function will NOT
        open or close the connection itself.
    view_ddl : list[list[str]] | None
        Optional override of the DDL list. Defaults to
        :data:`NEXUS_CURATED_VIEW_DDL`.


    Returns
    -------
    int
        Number of views created or replaced.
    """
    ddl = view_ddl or NEXUS_CURATED_VIEW_DDL
    created = 0
    for candidates in ddl:
        last_exc: Exception | None = None
        for stmt in candidates:
            try:
                con.execute(stmt)
                created += 1
                last_exc = None
                break
            except Exception as exc:
                # Try next candidate, or fall through to log
                last_exc = exc
        if last_exc is not None:
            # All candidates failed — likely the underlying table
            # doesn't exist yet. Not fatal; will retry next call.
            logger.debug(
                "View migration skipped (all %d candidates failed): %s",
                len(candidates), last_exc,
            )
    if created:
        logger.info(
            "Nexus view migration: %d/%d views ensured",
            created, len(ddl),
        )
    return created


def ensure_asof_macros(con: Any, macro_ddl: list[str] | None = None) -> int:
    """Ensure the 5 ``*_asof`` macros exist in the given connection.

    B2 anti-leakage: each macro wraps a curated view with an ``as_of``
    cutoff parameter so agent tools can clamp results to the current
    sim-bar. Idempotent (``CREATE OR REPLACE MACRO``). Returns the
    number of macros successfully created or replaced. Fails soft if
    the underlying view doesn't exist yet (will retry on next call).

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        An open writable DuckDB connection.
    macro_ddl : list[str] | None
        Optional override of the DDL list. Defaults to
        :data:`NEXUS_ASOF_MACRO_DDL`.

    Returns
    -------
    int
        Number of macros created or replaced.
    """
    ddl = macro_ddl or NEXUS_ASOF_MACRO_DDL
    created = 0
    for stmt in ddl:
        try:
            con.execute(stmt)
            created += 1
        except Exception as exc:
            # Likely the underlying view doesn't exist yet. Will retry
            # on the next call after ``ensure_views`` has populated them.
            logger.debug(
                "asof macro creation skipped: %s", exc,
            )
    if created:
        logger.info(
            "Nexus asof macro migration: %d/%d macros ensured",
            created, len(ddl),
        )
    return created


def ensure_views_in_quantdb(db_path: str | None = None) -> int:
    """Open quant.duckdb in write mode and ensure views + asof macros exist.

    Also installs the asset-class views + asof macros in the DuckLake
    catalog (the production read path used by nexus-trade via the
    ``ducklake_redirect`` shim). DuckLake install is best-effort — if
    the catalog isn't reachable, we log a warning and continue.

    Returns the total number of views+macros created/replaced across
    both targets (local file + DuckLake).

    Parameters
    ----------
    db_path : str | None
        Path to the DuckDB file. Defaults to ``NEXUS_LAKEHOUSE_PATH``
        env var or ``~/development/agentic-quant-os/data/quant.duckdb``.

    Returns
    -------
    int
        Number of views+macros created/replaced (0 if file doesn't exist
        or can't be opened in write mode).
    """
    if db_path is None:
        db_path = os.path.expanduser(
            os.environ.get(
                "NEXUS_LAKEHOUSE_PATH",
                "~/development/agentic-quant-os/data/quant.duckdb",
            )
        )
    n_total = 0
    # --- Local file ---
    if not os.path.exists(db_path):
        logger.warning(
            "quant.duckdb not found at %s — skipping local view migration",
            db_path,
        )
    else:
        try:
            import duckdb
            con = duckdb.connect(db_path, read_only=False)
            try:
                n_total += ensure_views(con)
                n_total += ensure_asset_class_views(con)
                n_total += ensure_asof_macros(con)
            finally:
                con.close()
        except Exception as exc:
            logger.error(
                "Local view migration failed for %s: %s", db_path, exc,
            )

    # --- DuckLake (best-effort) ---
    n_total += ensure_views_in_ducklake()
    return n_total


def ensure_views_in_ducklake() -> int:
    """Install asset-class views + asof macros in the DuckLake catalog.

    The DuckLake catalog lives at ``ducklake:postgres:...`` (defaults to
    localhost:5433) and stores the canonical production lakehouse. The
    ``ducklake_redirect`` shim in ``src.lakehouse.reader`` makes
    nexus-trade read from DuckLake transparently, so the views MUST exist
    there too. Best-effort: returns 0 and logs a warning if the catalog
    isn't reachable.

    Returns
    -------
    int
        Number of views+macros created/replaced in DuckLake (0 if unreachable).
    """
    catalog = os.environ.get(
        "DUCKLAKE_CATALOG",
        "ducklake:postgres:dbname=ducklake_catalog host=localhost port=5433 user=postgres",
    )
    data_path = os.environ.get(
        "DUCKLAKE_DATA_PATH",
        "/home/Zev/development/agentic-quant-os/data/ducklake_data",
    )
    try:
        import duckdb
        con = duckdb.connect(catalog)
        con.execute(
            f"ATTACH '{catalog}' AS ducklake (DATA_PATH '{data_path}')"
        )
    except Exception as exc:
        logger.warning(
            "DuckLake catalog unreachable (%s) — skipping DuckLake view migration",
            str(exc)[:120],
        )
        return 0
    try:
        n = 0
        for stmt in NEXUS_ASSET_CLASS_VIEWS_DUCKLAKE:
            try:
                con.execute(stmt)
                n += 1
            except Exception as exc:
                logger.debug(
                    "DuckLake asset-class view skipped (%s): %s",
                    stmt[:60], str(exc)[:120],
                )
        for stmt in NEXUS_ASSET_CLASS_VIEWS_DUCKLAKE_MACROS:
            try:
                con.execute(stmt)
                n += 1
            except Exception as exc:
                logger.debug(
                    "DuckLake asof macro skipped (%s): %s",
                    stmt[:60], str(exc)[:120],
                )
        if n:
            logger.info("DuckLake asset-class migration: %d views+macros ensured", n)
        return n
    finally:
        con.close()


def ensure_asset_class_views(
    con: Any, view_ddl: list[str] | None = None,
) -> int:
    """Install the 2 asset-class split views.

    Asset-class split (2026-06-26): the strategy pool is split into
    ``v_nexus_strategy_pool_crypto`` (BTC/ETH/SOL universe) and
    ``v_nexus_strategy_pool_stocks`` (TradFi universe). This function
    installs both. Idempotent (``CREATE OR REPLACE VIEW``). Returns the
    number of views successfully created.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        An open writable DuckDB connection.
    view_ddl : list[str] | None
        Optional override of the DDL list. Defaults to
        :data:`NEXUS_ASSET_CLASS_VIEWS`.

    Returns
    -------
    int
        Number of views created or replaced.
    """
    ddl = view_ddl or NEXUS_ASSET_CLASS_VIEWS
    created = 0
    for stmt in ddl:
        try:
            con.execute(stmt)
            created += 1
        except Exception as exc:
            logger.debug(
                "asset-class view creation skipped (%s): %s",
                stmt[:60], exc,
            )
    if created:
        logger.info(
            "Nexus asset-class view migration: %d/%d views ensured",
            created, len(ddl),
        )
    return created


def check_missing_views(db_path: str | None = None) -> list[str]:
    """Check which of the 4 nexus views (and asof macros) are missing
    from quant.duckdb.

    Returns a list of names that don't exist or error on read.
    Empty list means all are present.
    """
    if db_path is None:
        db_path = os.path.expanduser(
            os.environ.get(
                "NEXUS_LAKEHOUSE_PATH",
                "~/development/agentic-quant-os/data/quant.duckdb",
            )
        )
    if not os.path.exists(db_path):
        return (
            list((
                "v_nexus_regime_strategy_map",
                "v_nexus_catalyst_digest",
                "v_nexus_failures",
                "v_nexus_experience",
            ))
            + list(NEXUS_ASSET_CLASS_VIEW_NAMES)
            + list(NEXUS_ASOF_MACRO_NAMES)
        )
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        try:
            missing = []
            for vname in (
                "v_nexus_regime_strategy_map",
                "v_nexus_catalyst_digest",
                "v_nexus_failures",
                "v_nexus_experience",
            ) + NEXUS_ASSET_CLASS_VIEW_NAMES + NEXUS_ASOF_MACRO_NAMES:
                try:
                    if vname in NEXUS_ASOF_MACRO_NAMES:
                        con.execute(f"SELECT 1 FROM {vname}(CURRENT_TIMESTAMP) LIMIT 1").fetchone()
                    else:
                        con.execute(f"SELECT 1 FROM {vname} LIMIT 1").fetchone()
                except Exception as exc:
                    # DEBUG: log the underlying exception so we can diagnose
                    # catalog mismatches. Remove once confirmed stable.
                    import logging
                    logging.getLogger(__name__).debug(
                        "check_missing_views: %s reported missing: %s",
                        vname, str(exc)[:120],
                    )
                    missing.append(vname)
            return missing
        finally:
            con.close()
    except Exception:
        return []


if __name__ == "__main__":
    # CLI: python -m src.lakehouse.view_migration
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    missing = check_missing_views(db_path)
    if missing:
        print(f"Missing views/macros: {missing}")
        n = ensure_views_in_quantdb(db_path)
        print(f"Created/replaced: {n}")
    else:
        print("All nexus views and asof macros already present.")
