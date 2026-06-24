"""
Nexus Trader Investment Committee — Multi-Agent Strategy with Vector Memory
==========================================================================

A multi-agent investment committee strategy for LumiBot that combines:

1. **Evidence Researcher** — gathers market data, technical indicators,
   and market regime detection.
2. **Bull Researcher** — builds the strongest long-only case.
3. **Bear Researcher** — attacks the trade and identifies risks.
4. **Portfolio Manager** — makes the final decision and places orders.

Each agent has access to custom tools including:
- ``detect_regime`` — CrabQuant market regime classification
- ``signal_dashboard`` — comprehensive technical snapshot
- ``query_trade_memory`` — semantic search of past decisions and lessons
- ``remember_decision`` — persist this decision for future learning
- ``remember_lesson`` — store lessons learned

Key design principles
---------------------
* **Works in backtesting and live** — uses ``self.get_historical_prices()``
  for data, ``self.agents`` for AI committee.
* **Persistent memory** — decisions and lessons are stored in LanceDB
  vector memory for semantic retrieval across runs.
* **Regime-aware** — uses CrabQuant regime detection to inform risk
  appetite and strategy selection.
* **Graceful degradation** — if vector memory or regime detection
  is unavailable, the committee still operates with core tools.

Environment variables
---------------------
* ``NEXUS_LANCEDB_DIR`` — override LanceDB storage directory
* ``NEXUS_MEMORY_DIR`` — override LumiBot memory JSONL directory
* ``COMMITTEE_RESEARCH_MODEL`` — model for evidence researcher
* ``COMMITTEE_BULL_MODEL`` — model for bull researcher
* ``COMMITTEE_BEAR_MODEL`` — model for bear researcher
* ``COMMITTEE_TRADER_MODEL`` — model for portfolio manager

Example model setup:

    COMMITTEE_RESEARCH_MODEL="openai/gpt-4o-mini"
    COMMITTEE_BULL_MODEL="openai/gpt-4o"
    COMMITTEE_BEAR_MODEL="openai/gpt-4o"
    COMMITTEE_TRADER_MODEL="openai/gpt-4o"

Requirements
------------
* LumiBot with agent support (``strategy.agents``)
* CrabQuant (importable, for regime detection)
* Nexus Trader memory package (importable, for vector memory)
* ``sentence-transformers`` with ``Qwen/Qwen3-Embedding-0.6B`` (local embeddings)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from typing import Any

# Ensure Nexus Trader src is importable
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ----------------------------------------------------------------------
# DETERMINISTIC GUARD — fail loud at strategy init if lancedb or
# sentence_transformers were installed into the LUMIBOT venv.
#
# The vector memory stack lives in the AQOS venv and is invoked via
# subprocess bridge (src/memory/bridge.py). If we can import it from the
# lumibot venv, something has been installed in the wrong place — refuse
# to start the strategy. See AGENTS.md "Vector memory stack" for context.
# ----------------------------------------------------------------------
def _check_vector_memory_venv_isolation(logger_name: str) -> None:
    """Hard-fail if lancedb or sentence_transformers are importable here.

    These packages belong in the AQOS venv, not the lumibot venv. If they
    are importable from the current Python, something has been installed
    in the wrong venv. We log a loud banner and raise so the strategy
    won't initialize.
    """
    forbidden = []
    try:
        import lancedb  # noqa: F401
        forbidden.append("lancedb")
    except ImportError:
        pass
    try:
        import sentence_transformers  # noqa: F401
        forbidden.append("sentence_transformers")
    except ImportError:
        pass
    if forbidden:
        banner_lines = [
            "",
            "+--------------------------------------------------------------+",
            "| FATAL: vector memory stack found in LUMIBOT venv              |",
            "+--------------------------------------------------------------+",
            f"| Detected: {', '.join(forbidden):<54}|",
            "| The vector memory stack (lancedb + sentence_transformers +   |",
            "| Qwen3-Embedding-0.6B) must live in the AQOS venv. Nexus calls |",
            "| it via subprocess bridge (src/memory/bridge.py), NOT in-process.|",
            "|                                                              |",
            "| If you ran `pip install lancedb sentence_transformers` in the|",
            "| lumibot venv, UNINSTALL it:                                  |",
            "|   <lumibot-venv>/bin/pip uninstall -y lancedb sentence_transformers |",
            "|                                                              |",
            "| See nexus-trade/AGENTS.md -> 'Vector memory stack'.          |",
            "+--------------------------------------------------------------+",
            "",
        ]
        banner = "\n".join(banner_lines)
        logging.getLogger(logger_name).critical(banner)
        # Also write to stderr in case logging isn't wired yet
        import sys as _sys
        print(banner, file=_sys.stderr)
        raise RuntimeError(
            "Vector memory stack (lancedb/sentence_transformers) must not be "
            "installed in the lumibot venv. See banner for fix."
        )


_check_vector_memory_venv_isolation("src.strategies.nexus_committee")

from lumibot.strategies.strategy import Strategy

logger = logging.getLogger(__name__)

# Default trading universe
DEFAULT_UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "SPY", "QQQ"]


class NexusCommitteeStrategy(Strategy):
    """Multi-agent investment committee with persistent vector memory.

    Parameters (configurable via ``parameters`` dict):
        universe : list[str]
            Symbols to trade (default: tech-heavy large caps + SPY/QQQ).
        max_position_pct : float
            Maximum portfolio allocation per position (default 0.20 = 20%).
        max_new_positions_per_run : int
            Max new positions opened per committee session (default 2).
        enable_notifications : bool
            Whether to enable Telegram/email notifications (default False).
        use_memory_bridge : bool
            Whether to run the LumiBot JSONL → vector memory bridge
            before each committee session (default True).
    """

    parameters = {
        "universe": DEFAULT_UNIVERSE,
        "max_position_pct": 0.20,
        "max_new_positions_per_run": 2,
        "enable_notifications": False,
        "use_memory_bridge": True,
    }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Set up the committee agents, tools, and state."""
        self.sleeptime = "1D"

        # Register this strategy with the tool context so @agent_tool
        # functions can access it WITHOUT taking ``self`` as a parameter.
        # This fixes the self-binding bug where ADK's FunctionTool was
        # exposing ``self`` as a required parameter to the AI model.
        from src.tools._strategy_context import register_strategy
        register_strategy(self)

        # Persistent state across iterations
        self.vars.last_evidence_pack = None
        self.vars.last_bull_case = None
        self.vars.last_bear_case = None
        self.vars.current_regime = "unknown"
        self.vars.committee_run_count = 0

        # Notifications (Telegram, email)
        if self.parameters.get("enable_notifications"):
            try:
                self.notifications.enabled = True
                self.notifications.configure_telegram()
            except Exception:
                logger.warning("Notifications not configured — continuing without")

        # Model selection from environment — MiniMax-M3 is the default
        # because it's cost-effective with strong reasoning.  Override per-role
        # via COMMITTEE_*_MODEL env vars if needed.
        _default_model = "minimax/MiniMax-M3"
        research_model = os.environ.get("COMMITTEE_RESEARCH_MODEL", _default_model)
        bull_model = os.environ.get("COMMITTEE_BULL_MODEL", _default_model)
        bear_model = os.environ.get("COMMITTEE_BEAR_MODEL", _default_model)
        trader_model = os.environ.get("COMMITTEE_TRADER_MODEL", _default_model)

        # ── Collect custom tools ──
        self._lakehouse_enabled = self.parameters.get("lakehouse_enabled", True)
        core_tools = self._load_core_tools()
        lakehouse_tools = self._load_lakehouse_tools()
        stratforge_tool = self._load_stratforge_tools()

        # All tools go to every agent except stratforge (PM only)
        all_read_tools = core_tools + lakehouse_tools
        pm_tools = core_tools + lakehouse_tools + stratforge_tool

        # ── Create committee agents ──
        self.agents.create(
            name="evidence_researcher",
            model=research_model,
            allow_trading=False,
            tools=all_read_tools,
            system_prompt=self._evidence_researcher_prompt(),
        )
        self.agents.create(
            name="bull_researcher",
            model=bull_model,
            allow_trading=False,
            tools=all_read_tools,
            system_prompt=self._bull_researcher_prompt(),
        )
        self.agents.create(
            name="bear_researcher",
            model=bear_model,
            allow_trading=False,
            tools=all_read_tools,
            system_prompt=self._bear_researcher_prompt(),
        )
        self.agents.create(
            name="portfolio_manager",
            model=trader_model,
            allow_trading=True,
            tools=pm_tools,
            system_prompt=self._portfolio_manager_prompt(),
        )

        # ── Run memory bridge on first run ──
        if self.parameters.get("use_memory_bridge", True):
            self._run_memory_bridge()

        logger.info(
            "NexusCommittee initialized: universe=%s, models=[%s, %s, %s, %s], lakehouse=%s",
            self.parameters.get("universe"),
            research_model, bull_model, bear_model, trader_model,
            self._lakehouse_enabled,
        )

    def _load_core_tools(self) -> list:
        """Load core @agent_tool tools (regime, dashboard, memory, signal eval)."""
        tools = []
        try:
            from src.tools.regime_tool import DETECT_REGIME_TOOL
            from src.tools.signal_dashboard_tool import SIGNAL_DASHBOARD
            from src.tools.trade_memory_tool import (
                QUERY_TRADE_MEMORY,
                REMEMBER_DECISION,
                REMEMBER_LESSON,
                GET_MEMORY_STATS,
            )
            from src.tools.evaluate_signal import EVALUATE_SIGNAL
            tools = [
                DETECT_REGIME_TOOL,
                SIGNAL_DASHBOARD,
                QUERY_TRADE_MEMORY,
                REMEMBER_DECISION,
                REMEMBER_LESSON,
                GET_MEMORY_STATS,
                EVALUATE_SIGNAL,
            ]
            logger.info("Loaded %d core tools", len(tools))
        except Exception as exc:
            logger.warning("Failed to load core tools: %s", exc)
        return tools

    def _load_lakehouse_tools(self) -> list:
        """Load lakehouse @agent_tool tools (regime, signals, factors, etc.)."""
        if not self._lakehouse_enabled:
            return []
        try:
            from src.lakehouse.nexus_tools import (
                LAKEHOUSE_REGIME,
                LAKEHOUSE_SIGNALS,
                LAKEHOUSE_FACTORS,
                LAKEHOUSE_STRATEGY_CANDIDATES,
                LAKEHOUSE_CATALYST,
                LAKEHOUSE_EXPERIENCE,
                LAKEHOUSE_PREFLIGHT,
                LAKEHOUSE_INTELLIGENCE,
                LAKEHOUSE_WRITE_LESSON,
            )
            tools = [
                LAKEHOUSE_REGIME,
                LAKEHOUSE_SIGNALS,
                LAKEHOUSE_FACTORS,
                LAKEHOUSE_STRATEGY_CANDIDATES,
                LAKEHOUSE_CATALYST,
                LAKEHOUSE_EXPERIENCE,
                LAKEHOUSE_PREFLIGHT,
                LAKEHOUSE_INTELLIGENCE,
                LAKEHOUSE_WRITE_LESSON,
            ]
            logger.info("Loaded %d lakehouse tools", len(tools))
            return tools
        except Exception as exc:
            logger.warning("Lakehouse tools not available: %s — continuing without", exc)
            return []

    def _load_stratforge_tools(self) -> list:
        """Load StratForge query tool for the PM agent."""
        try:
            from src.lakehouse.stratforge_query import QUERY_STRATFORGE_STRATEGIES
            logger.info("Loaded StratForge query tool")
            return [QUERY_STRATFORGE_STRATEGIES]
        except Exception as exc:
            logger.warning("StratForge query tool not available: %s", exc)
            return []

    def _run_memory_bridge(self) -> None:
        """Sync LumiBot JSONL memory into vector memory (non-blocking, best-effort)."""
        try:
            from src.memory.bridge import MemoryBridge

            bridge = MemoryBridge(strategy_name="Nexus_Trader")
            stats = bridge.sync_all()
            total = sum(stats.get(k, {}).get("embedded", 0) for k in ("decisions", "lessons", "theses", "memories"))
            if total > 0:
                logger.info("Memory bridge synced %d entries", total)
            else:
                logger.debug("Memory bridge: no new entries to sync")
        except Exception:
            logger.debug("Memory bridge not available — continuing without", exc_info=True)

    # ------------------------------------------------------------------
    # Trading iteration — the committee session
    # ------------------------------------------------------------------

    def on_trading_iteration(self) -> None:
        """Run a full investment committee session."""
        self.vars.committee_run_count += 1
        run = self.vars.committee_run_count

        universe = list(self.parameters.get("universe") or DEFAULT_UNIVERSE)
        now = self.get_datetime()
        logger.info("[NexusCommittee run %d] Session at %s, universe=%s", run, now.isoformat(), universe)

        # ── Phase 0: Pre-fetch regime and memory context (background) ──
        context = self._build_context(universe)

        # ── Phase 1: Evidence Researcher ──
        evidence = self.agents["evidence_researcher"].run(
            task_prompt=(
                f"Build the evidence pack for the investment committee at {now.isoformat()}. "
                f"Scan the full universe {universe}, then focus deeply on the top 3-4 "
                f"long candidates. Use signal_dashboard for each promising candidate, "
                f"detect_regime to understand market conditions, and query_trade_memory to "
                f"learn from similar past situations. "
                f"Your output will be handed to the bull and bear researchers."
            ),
            context=context,
        )
        self.vars.last_evidence_pack = evidence.summary or evidence.text
        logger.info("[NexusCommittee run %d] Evidence pack: %d chars", run, len(self.vars.last_evidence_pack or ""))

        # ── Phase 2: Bull Case ──
        bull = self.agents["bull_researcher"].run(
            task_prompt=(
                "Build the strongest long-only investment case from the evidence pack. "
                "Use query_trade_memory to find historical precedents that support your thesis. "
                "Use signal_dashboard to confirm technical alignment. "
                "Identify the top 1-3 candidates and explain why the reward justifies the risk."
            ),
            context={
                **context,
                "evidence_pack": self.vars.last_evidence_pack,
            },
        )
        self.vars.last_bull_case = bull.summary or bull.text
        logger.info("[NexusCommittee run %d] Bull case: %d chars", run, len(self.vars.last_bull_case or ""))

        # ── Phase 3: Bear Case ──
        bear = self.agents["bear_researcher"].run(
            task_prompt=(
                "Attack the long case. Find reasons to avoid, delay, reduce size, or demand "
                "more evidence. Use query_trade_memory to surface lessons from past losses "
                "or mistakes in similar conditions. Use detect_regime to check if the current "
                "regime is appropriate for the proposed trade. Identify specific invalidation "
                "points for each candidate."
            ),
            context={
                **context,
                "evidence_pack": self.vars.last_evidence_pack,
                "bull_case": self.vars.last_bull_case,
            },
        )
        self.vars.last_bear_case = bear.summary or bear.text
        logger.info("[NexusCommittee run %d] Bear case: %d chars", run, len(self.vars.last_bear_case or ""))

        # ── Phase 4: Portfolio Manager Decision ──
        decision = self.agents["portfolio_manager"].run(
            task_prompt=(
                "Make the final long-only portfolio decision. "
                "1. Check current positions, cash, and open orders. "
                "2. Review the evidence pack, bull case, and bear case. "
                "3. Respect max_position_pct={max_pct} and max_new_positions_per_run={max_new}. "
                "4. Trade only symbols in context.universe. "
                "5. Do not short. Do not use options. "
                "6. Prefer doing nothing when evidence is weak. "
                "7. After placing orders, use remember_decision for every trade you execute. "
                "8. If you identify important lessons, use remember_lesson to store them."
            ).format(
                max_pct=self.parameters.get("max_position_pct", 0.20),
                max_new=self.parameters.get("max_new_positions_per_run", 2),
            ),
            context={
                **context,
                "evidence_pack": self.vars.last_evidence_pack,
                "bull_case": self.vars.last_bull_case,
                "bear_case": self.vars.last_bear_case,
            },
        )
        summary = decision.summary or decision.text
        self.log_message(f"[NexusCommittee run {run}] {summary}", color="yellow")

        # ── Phase 5: Write-back to AQS agent_memory ──
        self._write_decision_to_aqs(
            universe=universe,
            summary=summary,
            evidence=self.vars.last_evidence_pack or "",
            bull_case=self.vars.last_bull_case or "",
            bear_case=self.vars.last_bear_case or "",
            run_id=run,
            regime=context.get("current_regime", "unknown"),
        )

        logger.info("[NexusCommittee run %d] Complete", run)

    def _write_decision_to_aqs(
        self,
        universe: list[str],
        summary: str,
        evidence: str,
        bull_case: str,
        bear_case: str,
        run_id: int,
        regime: str,
    ) -> None:
        """Write the PM decision to AQS agent_memory for cross-agent visibility."""
        try:
            from src.memory.aqs_writer import write_decision_to_aqs

            # Parse action from summary (best-effort)
            summary_lower = summary.lower()
            if any(w in summary_lower for w in ("buy", "long", "enter", "submit")):
                action = "buy"
            elif any(w in summary_lower for w in ("sell", "exit", "close")):
                action = "sell"
            else:
                action = "hold"

            # Write one decision record per primary symbol
            failed_symbols = []
            for symbol in universe[:3]:  # top 3 max
                ok = write_decision_to_aqs(
                    symbol=symbol,
                    action=action,
                    regime=regime,
                    thesis_summary=summary[:1000],
                    committee_split={
                        "evidence_researcher": (evidence or "")[:200],
                        "bull_researcher": (bull_case or "")[:200],
                        "bear_researcher": (bear_case or "")[:200],
                        "portfolio_manager": summary[:200],
                    },
                    evidence_summary=evidence[:500],
                    run_id=f"run-{run_id}",
                )
                if not ok:
                    failed_symbols.append(symbol)

            if failed_symbols:
                logger.warning(
                    "[NexusCommittee run %d] AQS write failed for %d/%d symbols: %s",
                    run_id, len(failed_symbols), len(universe[:3]), failed_symbols,
                )
            else:
                logger.info("[NexusCommittee run %d] Decision written to AQS", run_id)
        except Exception as exc:
            logger.error("AQS write-back failed (non-fatal): %s", exc)

        # ── Phase 5: AQS sync (dual-write path) ──
        self._sync_decision_to_aqs(summary, universe, regime, run_id)

    # ------------------------------------------------------------------
    # AQS write-back (second path via aqs_sync)
    # ------------------------------------------------------------------

    def _sync_decision_to_aqs(
        self,
        summary: str,
        universe: list[str],
        regime: str,
        run: int,
    ) -> None:
        """Write PM decision and committee summary to AQS lakehouse.

        Best-effort: never crashes the committee if AQS is unavailable.
        Writes to:
        - ``agent_memory`` (memory_type='trade_decision')
        - ``signals`` (source_repo='nexus-trade')
        """
        try:
            from src.memory.aqs_sync import sync_committee_decision, sync_signal

            run_id = f"run{run}_{self.get_datetime().strftime('%Y%m%d_%H%M')}"
            ts = self.get_datetime().isoformat()

            # Extract primary symbol from universe (first crypto ticker)
            symbol = universe[0] if universe else "UNKNOWN"

            # Determine action from summary (simple heuristic)
            summary_lower = summary.lower()
            if any(w in summary_lower for w in ("buy", "long", "enter", "submitted")):
                action = "buy"
            elif any(w in summary_lower for w in ("sell", "exit", "close")):
                action = "sell"
            else:
                action = "hold"

            # Write decision to agent_memory
            sync_committee_decision({
                "symbol": symbol,
                "action": action,
                "regime": regime,
                "thesis": summary[:2000],
                "evidence_summary": (
                    f"Bull: {(self.vars.last_bull_case or '')[:300]} | "
                    f"Bear: {(self.vars.last_bear_case or '')[:300]}"
                ),
                "committee_split": {
                    "evidence": (self.vars.last_evidence_pack or "")[:200],
                    "bull": (self.vars.last_bull_case or "")[:200],
                    "bear": (self.vars.last_bear_case or "")[:200],
                    "pm": summary[:200],
                },
                "backtest_id": getattr(self, "backtest_id", ""),
            }, run_id=run_id, timestamp=ts)

            # Write signal to signals table
            sync_signal({
                "source": "nexus-trade",
                "signal_type": "committee_decision",
                "ticker": symbol,
                "value": 1.0 if action == "buy" else (-1.0 if action == "sell" else 0.0),
                "confidence": 0.5,  # Neutral default; PM can refine
                "regime_context": regime,
                "metadata_json": json.dumps({
                    "run": run,
                    "run_id": run_id,
                    "universe": universe,
                    "committee_run_count": run,
                    "summary": summary[:500],
                }),
            })

            logger.info("[NexusCommittee run %d] AQS sync complete", run)
        except Exception:
            logger.debug("AQS sync failed (non-critical)", exc_info=True)

        # ── Phase 6: Auto-invoke LanceDB bridge from aqos venv ──────────
        # Controlled by NEXUS_BRIDGE_AUTO_SYNC=1 (default 0 = off)
        # NEXUS_VENV_AQOS must point to the aqos venv python binary.
        # Failures are logged and ignored — bridge errors must never crash the committee.
        if os.environ.get("NEXUS_BRIDGE_AUTO_SYNC") == "1":
            venv_python = os.environ.get("NEXUS_VENV_AQOS", "")
            if venv_python:
                try:
                    result = subprocess.run(
                        [venv_python, "-m", "src.memory.bridge",
                         "--strategy", "Nexus_Trader"],
                        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    if result.returncode == 0:
                        logger.info("[NexusCommittee run %d] Bridge auto-sync completed", run)
                    else:
                        logger.warning(
                            "[NexusCommittee run %d] Bridge auto-sync returned %d: %s",
                            run, result.returncode, result.stderr[:200],
                        )
                except subprocess.TimeoutExpired:
                    logger.warning("[NexusCommittee run %d] Bridge auto-sync timed out after 300s", run)
                except Exception as exc:
                    logger.warning("[NexusCommittee run %d] Bridge auto-sync failed: %s", run, exc)
            else:
                logger.debug("NEXUS_VENV_AQOS not set — skipping bridge auto-sync")

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(self, universe: list[str]) -> dict[str, Any]:
        """Build the context dict passed to all committee agents.

        Includes market regime, memory stats, and trade parameters.
        """
        context: dict[str, Any] = {
            "universe": universe,
            "max_position_pct": self.parameters.get("max_position_pct", 0.20),
            "max_new_positions_per_run": self.parameters.get("max_new_positions_per_run", 2),
            "datetime": self.get_datetime().isoformat(),
            "committee_run": self.vars.committee_run_count,
            "backtest_id": getattr(self, "backtest_id", ""),
        }

        # Try to get regime and memory stats for context
        try:
            from src.tools.regime_tool import detect_regime_tool
            regime_result = detect_regime_tool(lookback=50)
            context["current_regime"] = regime_result.get("regime", "unknown")
            context["regime_confidence"] = regime_result.get("confidence", 0.0)
            context["regime_details"] = regime_result
            self.vars.current_regime = regime_result.get("regime", "unknown")
            logger.debug("Pre-fetched regime: %s", context["current_regime"])
        except Exception:
            context["current_regime"] = "unknown"
            context["regime_confidence"] = 0.0

        # Try to get memory stats
        try:
            from src.tools.trade_memory_tool import get_memory_stats_tool
            mem_stats = get_memory_stats_tool()
            context["memory_stats"] = mem_stats
        except Exception:
            context["memory_stats"] = {"enabled": False}

        # ── Lakehouse intelligence (mode-gated, graceful degradation) ──
        if self._lakehouse_enabled:
            try:
                from src.lakehouse.reader import get_reader
                lakehouse = get_reader()
                hc = lakehouse.health_check()
                if hc.get("connected"):
                    # Pre-fetch regime for all universe tickers
                    lakehouse_regimes = {}
                    for sym in universe:
                        reg = lakehouse.get_regime(sym)
                        if reg:
                            lakehouse_regimes[sym] = reg
                    context["lakehouse_regimes"] = lakehouse_regimes
                    context["lakehouse_available"] = True
                    # Include strategy candidates from pool
                    strats = lakehouse.get_strategy_pool(min_composite=49.0, limit=10)
                    if strats:
                        context["lakehouse_strategy_candidates"] = [
                            {"name": s.get("strategy_name"), "sharpe": s.get("sharpe"),
                             "composite": s.get("composite_score"), "status": s.get("status"),
                             "ticker": s.get("ticker")}
                            for s in strats[:5]
                        ]
                    # Include recent failures for awareness
                    failures = lakehouse.get_failures(limit=10)
                    if failures:
                        context["lakehouse_failures"] = [
                            {"strategy": f.get("strategy_name"), "reason": f.get("failure_reason", "")[:80]}
                            for f in failures[:5]
                        ]
                    logger.debug("Lakehouse context injected: %d regimes, %d strategies, %d failures",
                                 len(lakehouse_regimes), len(strats), len(failures))
                else:
                    context["lakehouse_available"] = False
            except Exception:
                context["lakehouse_available"] = False

        return context

    # ------------------------------------------------------------------
    # System prompts
    # ------------------------------------------------------------------

    def _evidence_researcher_prompt(self) -> str:
        return """
You are the Evidence Researcher for a LumiBot AI investment committee.

You cannot place, modify, or cancel trades. Your job is to build a compact,
well-sourced evidence pack.

For each candidate symbol, use these tools:
1. **signal_dashboard(symbol)** — get RSI, MACD, SMA crossovers, ATR, momentum,
   Bollinger Bands, trend alignment, and risk recommendation in one call.
2. **detect_regime()** — classify the market regime (trending_up, trending_down,
   mean_reversion, high_volatility, low_volatility).
3. **query_trade_memory(query, symbol, regime)** — search past decisions and
   lessons for context on how similar setups played out.
4. **get_memory_stats()** — check how much historical data we have.

Lakehouse tools (if available — these pull from the quant ecosystem lakehouse):
5. **lakehouse_intelligence(ticker)** — FULL intelligence packet: regime from
   Regime Intelligence, curated signals, alpha-factory factors, catalyst grades,
   experience bank lessons, failure history, and strategy candidates. Use this
   for any ticker in the universe.
6. **lakehouse_regime(ticker)** — latest composite regime from the lakehouse.
7. **lakehouse_signals(ticker)** — curated signal feed from alpha-lab, alpha-factory,
   regime-intelligence (confidence-filtered).
8. **lakehouse_factors(ticker)** — factor snapshot from alpha-factory.
9. **lakehouse_strategy_candidates(regime)** — promoted strategies with Sharpe>1.0.
10. **lakehouse_preflight(strategy_name, ticker)** — check failure history before trading.
11. Any built-in tools for price data, news, fundamentals.

If lakehouse tools are available, ALWAYS use lakehouse_intelligence as your first
source for each ticker — it aggregates everything. Supplement with signal_dashboard
for real-time technical data.

Output structured markdown with:
- Market regime and confidence (from lakehouse if available, detect_regime otherwise)
- Candidates reviewed (with signal_dashboard results)
- Top long candidates (with rationale)
- Lakehouse intelligence summary (if available)
- Technical summary per candidate
- Bull evidence
- Bear evidence
- Relevant historical precedents (from query_trade_memory)
- Missing data or uncertainty
- Tools/sources used
"""

    def _bull_researcher_prompt(self) -> str:
        return """
You are the Bull Researcher. You cannot place, modify, or cancel trades.

Build the strongest long-only case from the evidence pack. You may use:
- **signal_dashboard(symbol)** to confirm technical alignment
- **query_trade_memory(query, symbol, regime)** to find historical winning patterns
- **lakehouse_intelligence(ticker)** for aggregated lakehouse data per ticker
- **lakehouse_factors(ticker)** for alpha-factory factor analysis
- **lakehouse_strategy_candidates(regime)** for strategies that work in current regime
- **lakehouse_experience(ticker)** for lessons from the quant ecosystem
- Any read-only tools to dig deeper

Focus on:
- Catalysts — what makes this trade work *now*?
- Technical setup — is the signal dashboard aligned?
- Regime fit — is the strategy appropriate for current conditions?
- Lakehouse factors — do alpha-factory factors support the thesis?
- Historical precedents — have similar setups worked before?
- Why the reward justifies the risk

Return:
- Strongest buy candidates (1-3, ranked)
- Thesis for each
- Supporting evidence
- Catalysts and triggers
- Risks you accept
- Invalidation points
- Suggested monitoring
"""

    def _bear_researcher_prompt(self) -> str:
        return """
You are the Bear Researcher. You cannot place, modify, or cancel trades.

Attack the long case. Find reasons the portfolio manager should avoid, delay,
reduce size, or demand more evidence. Use:
- **query_trade_memory(query)** to find past losses and lessons in similar conditions
- **detect_regime()** to check if current conditions contradict the thesis
- **signal_dashboard(symbol)** to find technical weaknesses
- **lakehouse_preflight(strategy_name, ticker)** to check failure history
- **lakehouse_experience(ticker)** for ecosystem lessons about what went wrong
- **lakehouse_intelligence(ticker)** for full risk picture from the lakehouse

Look for:
- Regime mismatch — is the strategy wrong for current conditions?
- Technical weaknesses — overbought, bearish divergences, resistance
- Historical precedent — have similar setups failed before?
- Lakehouse failures — have related strategies failed in the ecosystem?
- Missing data or high uncertainty
- Conflicting signals across the evidence pack

Return:
- Strongest objections per candidate
- Downside catalysts
- Evidence contradicting the bull case
- Red flags (technical, fundamental, macro)
- Conditions that would make the trade acceptable
- Veto recommendation if appropriate
"""

    def _portfolio_manager_prompt(self) -> str:
        return """
You are the Portfolio Manager for a long-only LumiBot strategy.

You may place real LumiBot orders, but only within the strategy risk limits.

Before trading:
1. Check current portfolio, positions, open orders, and available cash.
2. Review the evidence pack, bull case, and bear case.
3. Respect max_position_pct and max_new_positions_per_run from context.
4. Trade only symbols in context.universe. Do not buy defensive ETFs or
   alternative symbols unless they're in the universe.
5. Do not short. Do not use options.
6. Prefer doing nothing when evidence is weak or contradictory.
7. **After every trade decision, call remember_decision** to store it for
   future learning.
8. If you identify important lessons (patterns, mistakes, adaptations),
   call remember_lesson to store them.
9. **If lakehouse tools are available**, use lakehouse_write_lesson to
   persist important lessons to the ecosystem experience bank for cross-project learning.
10. **Use query_stratforge_strategies** to discover best-fit strategies from
    the StratForge lakehouse. Query by symbol and min composite score to find
    validated strategies with strong walk-forward Sharpe ratios. Use this to
    select which strategy to load for the current market conditions.

Use these memory tools:
- **query_trade_memory** — check what history tells us about similar setups
- **remember_decision** — persist this decision for future reference
- **remember_lesson** — capture lessons for the experience bank
- **lakehouse_preflight(strategy_name, ticker)** — check ecosystem failures before trading
- **lakehouse_write_lesson** — write lessons to the ecosystem (if available)

Return:
- Final decision (buy/sell/hold per candidate)
- Orders submitted, if any
- Sizing rationale
- Risk controls applied
- Why the bear case was accepted or rejected
- Monitoring points and invalidation levels
"""


# ---------------------------------------------------------------------------
# Backtest entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    from lumibot.backtesting import YahooDataBacktesting
    from lumibot.entities import Asset, TradingFee

    parser = argparse.ArgumentParser(description="Run Nexus Committee backtest")
    parser.add_argument("--start", default="2024-01-01", help="Backtest start date")
    parser.add_argument("--end", default="2024-06-01", help="Backtest end date")
    parser.add_argument("--budget", type=float, default=10000, help="Starting budget")
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_UNIVERSE[:5], help="Symbols to trade")
    parser.add_argument("--no-memory-bridge", action="store_true", help="Skip memory bridge")
    parser.add_argument("--no-lakehouse", action="store_true", help="Disable lakehouse integration (prevents backtest data leakage)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Configure strategy parameters
    NexusCommitteeStrategy.parameters = {
        "universe": args.symbols,
        "max_position_pct": 0.20,
        "max_new_positions_per_run": 2,
        "enable_notifications": False,
        "use_memory_bridge": not args.no_memory_bridge,
        "lakehouse_enabled": not args.no_lakehouse,
    }

    trading_fee = TradingFee(percent_fee=0.001)
    backtesting_start = datetime.fromisoformat(args.start)
    backtesting_end = datetime.fromisoformat(args.end)

    logger.info(
        "Starting Nexus Committee backtest: %s → %s, budget=$%.0f, symbols=%s",
        args.start, args.end, args.budget, args.symbols,
    )

    result = NexusCommitteeStrategy.backtest(
        YahooDataBacktesting,
        backtesting_start=backtesting_start,
        backtesting_end=backtesting_end,
        benchmark_asset=Asset("SPY", Asset.AssetType.STOCK),
        buy_trading_fees=[trading_fee],
        sell_trading_fees=[trading_fee],
        quote_asset=Asset("USD", Asset.AssetType.FOREX),
        budget=args.budget,
        quiet_logs=False,
    )

    logger.info("Backtest complete: %s", result)
    print("\nBacktest Results:")
    print(f"  Start: {args.start}")
    print(f"  End:   {args.end}")
    print(f"  Budget: ${args.budget:,.0f}")
    print(f"  Symbols: {args.symbols}")
    if result:
        for key, value in result.items():
            if isinstance(value, (int, float)):
                print(f"  {key}: {value:.4f}")
            else:
                print(f"  {key}: {value}")
