#!/usr/bin/env bash
# Weekly walk-forward memory refresh cron job
# ============================================
#
# Rebuilds the ``nexus_walkforward`` LanceDB collection from the latest
# ``walk_forward_results`` table in ``nexus_results.duckdb``. Runs in the
# AQOS venv (which has lancedb + sentence-transformers) because the Lumibot
# venv intentionally does NOT include them.
#
# Schedule (recommended): Mondays at 06:30 Pacific (after the Alpha Factory
# weekly pipeline at 06:00, before market open).
#
# Crontab entry:
#   30 6 * * 1 /home/Zev/development/nexus-trade/scripts/cron/refresh_walkforward.sh >> /tmp/nexus_walkforward_refresh.log 2>&1
#
# Manual run:
#   /home/Zev/development/nexus-trade/scripts/cron/refresh_walkforward.sh

set -euo pipefail

PROJECT_ROOT="/home/Zev/development/nexus-trade"
AQOS_VENV_PY="/home/Zev/development/agentic-quant-os/.venv/bin/python"
SEEDER="${PROJECT_ROOT}/src/memory/walkforward_seeder.py"
LOG_TAG="[walkforward-refresh]"

if [[ ! -x "${AQOS_VENV_PY}" ]]; then
    echo "$(date -Iseconds) ${LOG_TAG} ERROR: AQOS venv python not found at ${AQOS_VENV_PY}"
    exit 1
fi
if [[ ! -f "${SEEDER}" ]]; then
    echo "$(date -Iseconds) ${LOG_TAG} ERROR: seeder script not found at ${SEEDER}"
    exit 1
fi

echo "$(date -Iseconds) ${LOG_TAG} starting weekly refresh (--rebuild)"

cd "${PROJECT_ROOT}"
# Use --rebuild to drop all existing rows and re-seed from scratch.
# Rebuild is safe because walkforward memory is regenerated weekly and is
# wipe-and-rebuild by design (see walkforward_seeder.py docstring).
"${AQOS_VENV_PY}" "${SEEDER}" --rebuild --min-windows 1

RC=$?
if [[ ${RC} -eq 0 ]]; then
    echo "$(date -Iseconds) ${LOG_TAG} weekly refresh complete"
else
    echo "$(date -Iseconds) ${LOG_TAG} weekly refresh FAILED (rc=${RC})"
fi
exit ${RC}