"""
Attribution Bridge — Subprocess wrapper for updating LanceDB outcome + lesson
=============================================================================

The lumibot venv does NOT have lancedb / sentence_transformers (verified at
strategy init time by ``_check_vector_memory_venv_isolation`` in
``src/strategies/nexus_committee.py``). The vector memory stack lives in
the AQOS venv at ``/home/Zev/development/agentic-quant-os/.venv``.

This module is a thin subprocess wrapper — same pattern as
``NexusCommitteeStrategy._run_memory_bridge()`` (see nexus_committee.py).
We send a tiny Python snippet to the AQOS venv python that imports the
bridge and calls ``MemoryBridge.update_outcome(...)`` or
``MemoryBridge.write_lesson(...).``

The wrapper never raises. All failures are logged at DEBUG so the calling
attribution path (``PaperTradeCommitteeStrategy.on_filled_order``) stays
fast and non-blocking.

Functions
---------
* ``update_outcome_via_subprocess(decision_id, outcome, pnl_pct, symbol, strategy_name)``
* ``write_lesson_via_subprocess(text, category, severity, symbol, regime, tags)``
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Same paths as the existing memory bridge in nexus_committee.py.
_AQOS_PYTHON = "/home/Zev/development/agentic-quant-os/.venv/bin/python"
_NEXUS_SRC = "/home/Zev/development/nexus-trade/src"


def _run_aqos_subprocess(wrapper: str, timeout: int = 120) -> dict[str, Any] | None:
    """Run a Python snippet in the AQOS venv and parse the JSON result.

    The snippet must print ``__ATTRIB_RESULT__`` followed by a JSON object.
    Any other output (logs) on stderr is fine.

    Returns the parsed dict on success, None on any failure.
    """
    if not Path(_AQOS_PYTHON).exists():
        logger.debug("AQOS python not found at %s — attribution bridge disabled", _AQOS_PYTHON)
        return None

    bridge_env = {
        **os.environ,
        "PYTHONPATH": _NEXUS_SRC,
        # Use the strategy memory dir that JSONL decisions land in.
        "NEXUS_MEMORY_DIR": os.environ.get(
            "NEXUS_MEMORY_DIR",
            str(Path.home() / "development" / "nexus-trade" / ".lumibot" / "memory"),
        ),
    }

    full_script = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n" % _NEXUS_SRC
        + wrapper
        + "\nsys.stdout.write('__ATTRIB_RESULT__' + json.dumps(result, default=str))\n"
          "sys.stdout.flush()\n"
    )

    try:
        result = subprocess.run(
            [_AQOS_PYTHON, "-c", full_script],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=bridge_env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Attribution bridge subprocess timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.debug("Attribution bridge subprocess failed to launch: %s", exc)
        return None

    if result.returncode != 0:
        logger.warning(
            "Attribution bridge returned %d: %s",
            result.returncode,
            (result.stderr or "")[-200:],
        )
        return None

    stdout = result.stdout or ""
    marker = "__ATTRIB_RESULT__"
    if marker not in stdout:
        logger.debug("Attribution bridge returned no result marker")
        return None

    try:
        return json.loads(stdout.split(marker, 1)[1].strip())
    except Exception as exc:
        logger.debug("Attribution bridge: failed to parse JSON: %s", exc)
        return None


def update_outcome_via_subprocess(
    *,
    decision_id: str,
    outcome: str,
    pnl_pct: float,
    symbol: str,
    strategy_name: str,
) -> dict[str, Any] | None:
    """Update a decision's outcome/pnl in LanceDB via the AQOS bridge.

    The AQOS-side implementation lives in ``src.memory.bridge`` as
    ``MemoryBridge.update_outcome()`` (added 2026-06-25). It re-embeds the
    decision text and updates the matching LanceDB row by ``decision_id``.
    """
    if not decision_id:
        logger.debug("update_outcome_via_subprocess: empty decision_id — skipping")
        return None

    snippet = """
from src.memory.bridge import MemoryBridge
b = MemoryBridge(strategy_name={strategy_name!r})
result = b.update_outcome(
    decision_id={decision_id!r},
    outcome={outcome!r},
    pnl_pct={pnl_pct!r},
    symbol={symbol!r},
)
""".format(
        strategy_name=strategy_name,
        decision_id=decision_id,
        outcome=outcome,
        pnl_pct=float(pnl_pct),
        symbol=symbol,
    )

    parsed = _run_aqos_subprocess(snippet)
    if parsed is None:
        return None
    if parsed.get("ok"):
        logger.info(
            "Attribution outcome updated in LanceDB: %s -> %s pnl=%.2f%%",
            decision_id, outcome, pnl_pct,
        )
    else:
        logger.debug("Attribution outcome update reported failure: %s", parsed)
    return parsed


def write_lesson_via_subprocess(
    *,
    text: str,
    category: str = "insight",
    severity: str = "info",
    symbol: str = "",
    regime: str = "",
    tags: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Write a lesson into LanceDB ``lessons`` table via the AQOS bridge.

    Used by ``PaperTradeCommitteeStrategy._attribution_write_loss_lesson``
    to auto-write a "mistake" lesson when a closed position lost more
    than ``attribution_loss_threshold_pct``.
    """
    tags_list = list(tags or [])
    snippet = """
from src.memory.bridge import MemoryBridge
b = MemoryBridge(strategy_name={strategy_name!r})
result = b.write_lesson(
    text={text!r},
    category={category!r},
    severity={severity!r},
    symbol={symbol!r},
    regime={regime!r},
    tags={tags!r},
)
""".format(
        # The bridge in AQOS uses strategy_name='NexusCommitteeStrategy'
        # because that's the JSONL directory Lumibot writes to. This is
        # hard-coded because the calling strategy already runs under
        # that name; the bridge writes to nexus_lumibot_results/AQS via
        # subprocess anyway.
        strategy_name="NexusCommitteeStrategy",
        text=text,
        category=category,
        severity=severity,
        symbol=symbol,
        regime=regime,
        tags=tags_list,
    )

    parsed = _run_aqos_subprocess(snippet)
    if parsed is None:
        return None
    if parsed.get("ok"):
        logger.info(
            "Attribution lesson written to LanceDB: %s [%s/%s]",
            (text or "")[:60].replace("\n", " "),
            category, severity,
        )
    else:
        logger.debug("Attribution lesson write reported failure: %s", parsed)
    return parsed