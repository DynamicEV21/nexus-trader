# AGENTS.md — Nexus Trader Agent Operations Guide

> **Purpose:** When a new agent instance starts working on this project, read this file first. It contains critical operational knowledge, gotchas, and correct procedures that took significant investigation to discover. Do NOT re-derive these from scratch.

---

## Project Overview

**Nexus Trader** is an AI-native trading system built on LumiBot v4.5.25+. It turns LLMs into autonomous trading agents with tools, memory, and backtesting support.

- **Project directory:** `/home/Zev/development/nexus-trade/`
- **LumiBot install:** `/home/Zev/development/trading-bots/lumibot/` (venv at `.venv/bin/python`)
- **LumiBot .env:** `/home/Zev/development/trading-bots/lumibot/.env`
- **Cache dir:** `/tmp/lumibot_cache/` (set via `LUMIBOT_CACHE_FOLDER` env var)
- **Replay cache:** `/tmp/lumibot_cache/agent_runtime/replay/`
- **Traces:** `/tmp/lumibot_cache/agent_runtime/traces/<agent_name>/`
- **Summaries:** `/tmp/lumibot_cache/agent_runtime/agent_run_summaries.jsonl`
- **Memory JSONL:** `/home/Zev/development/trading-bots/lumibot/.lumibot/memory/<strategy_name>/`
- **Data:** `~/development/quant-projects/financial-data/stocks/sp500_daily/` (503 S&P tickers, 5yr daily)

---

## Agent Responsibilities

Every agent working on this project MUST:

1. **Update MEMORY.md** when you discover new operational knowledge, gotchas, configuration changes, or environment quirks.
2. **Update AGENTS.md** when you find procedures that future agents will need (model configs, correct CLI invocations, known failure modes).
3. **After any test or investigation**, check whether findings belong in `MEMORY.md` or `docs/EXECUTION_ROADMAP.md` (progress, test results, phase status) — not here.
4. **Keep AGENTS.md focused** on "how to operate this project." Test results, benchmarks, and phase progress go in `docs/EXECUTION_ROADMAP.md`.

---

## Key Architecture Concepts

### Vector Memory Stack — DO NOT RE-INSTALL OR RE-CREATE

**Sentence-transformers + Qwen3 + LanceDB is already wired and working.** Do not:

- ❌ `pip install sentence-transformers` in the lumibot venv — runs from aqos venv via subprocess instead
- ❌ Re-create the LanceDB instance at a different path
- ❌ Try to use a remote embedding API (OpenAI, Cohere) — costs $, adds latency, breaks the dual-write pipeline
- ❌ Replace Qwen3-Embedding-0.6B with a smaller model without asking first — 1.2 GB on disk is intentional

**The stack:**

| Layer | Component | Where |
|---|---|---|
| Engine | `sentence-transformers` 5.5.1 | `/home/Zev/development/agentic-quant-os/.venv/lib/.../site-packages/sentence_transformers` (4.5 MB) |
| Model | `Qwen/Qwen3-Embedding-0.6B` | `~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/` (1.2 GB) |
| Vector store | `lancedb` 0.33.0 | `/home/Zev/development/agentic-quant-os/data/vectors/nexus_decisions.lance` + `nexus_lessons.lance` |
| Bridge | `src/memory/bridge.py` | Reads LumiBot JSONL → writes LanceDB |
| Auto-sync | `src/strategies/nexus_committee.py` Phase 6 | Runs bridge as subprocess after each committee iteration |
| Env flag | `NEXUS_BRIDGE_AUTO_SYNC=1` (default 0) | Set in nexus-trade/.env to enable |

**If a future agent thinks "sentence-transformers isn't installed, let me pip install it" — STOP.** Check the aqos venv first:

```bash
ls /home/Zev/development/agentic-quant-os/.venv/lib/python*/site-packages/ | grep -iE "sentence|transformers|lancedb|torch"
```

If those four are present, the stack is good. The reason the lumibot venv doesn't have them is intentional: nexus_committee.py invokes the bridge as `subprocess.run([aqos_venv_python, ...])` precisely to keep lumibot venv lightweight.

**Verify it's working:**

```bash
cd /home/Zev/development/nexus-trade
export NEXUS_VENV_AQOS=/home/Zev/development/agentic-quant-os/.venv/bin/python
$NEXUS_VENV_AQOS -m src.memory.bridge --strategy Nexus_Trader --dry-run
# Should show: decisions.jsonl: N read, N unique ready to bridge
```

### Regime Detection — Two Views, Champion by Flag

`quant.duckdb` has TWO regime views:

- `v_nexus_regime` — original, includes `composite` + `ensemble` only (excludes `closed_loop`)
- `v_nexus_regime_champion` — preferred detectors with priority `closed_loop` > `composite` > `ensemble`

**Champion detector** = `closed_loop` (995 rows, BTC-USDT only, last 2026-06-18). Without the flag, BTC-USDT gets whatever `composite` last wrote, which can be 13 days stale.

Toggle with `NEXUS_REGIME_CHAMPION_ONLY=1` in the environment running the PM agent. **Default is 0** (off) for A/B testability.

View was bootstrapped via runtime `CREATE OR REPLACE VIEW` — **not in git**. If quant.duckdb is ever rebuilt from scratch, re-run the bootstrap SQL (see `memory/regime-champion-wiring-2026-06-24.md`).

### DuckDB Quant File — Don't Open Directly

If you need data from the Nexus curated views, **go through `src/lakehouse/reader.py`** (the `NexusLakehouseReader` class) or use the `get_reader()` singleton. Do not call `duckdb.connect('quant.duckdb')` directly anywhere — the reader uses ATTACH into a fresh in-memory connection (see `reader.py:_con_get`) on purpose: it bypasses a DuckDB v1.5.x catalog-cache bug where direct-connect reads return stale view definitions after the file is rewritten.

**If you find yourself writing `duckdb.connect(quant.duckdb_path)` somewhere new — STOP.** Use `get_reader()` and add a method to it.

### Two Caching Layers

1. **LumiBot Replay Cache** (local disk, any model)
   - SHA-256 hash of: system_prompt + task_prompt + runtime_context (datetime, positions, cash, orders, trades) + model + tool definitions + memory_notes
   - Stored as gzip JSON at `/tmp/lumibot_cache/agent_runtime/replay/`
   - Enables instant re-runs of identical backtests (~500x speedup)
   - Works with ANY model (GLM-5, Gemini, etc.)

2. **Provider Prompt Cache** (remote, model-specific)
   - Google's Gemini or OpenAI's server-side caching
   - ~20-40% cost discount on repeated prefixes
   - Only works with that provider's own models
   - GLM-5 does NOT get provider prompt caching on any endpoint

### Sleeptime Controls Agent Frequency

- `sleeptime = "1D"` → agent wakes once per trading day (~126 iterations per 6-month backtest)
- `sleeptime = "5min"` → agent wakes every 5 minutes (~75,000 iterations per 6-month backtest)
- The agent can load 5-minute bars at any sleeptime frequency via `market_load_history_table(timestep='5min')` with `WHERE datetime <= cutoff` in DuckDB
- **Use daily sleeptime for initial testing** — saves tokens and prevents context overflow

### DuckDB Time Wall (Future Data Protection)

- DuckDB creates a "visible" view with `WHERE datetime <= cutoff` (the current backtest datetime)
- This prevents the agent from seeing future price data in DuckDB queries
- **WARNING:** This only protects DuckDB data. Internet-connected tools (web search, news, FRED API) may NOT have time filtering. Must test for lookahead bias separately (see Roadmap Phase 0 test).

### Memory Notes Grow Per Iteration

- Each iteration's result is appended to `memory_notes` via `_append_memory()`
- Memory notes are included in the cache key hash
- This means iteration N always has more notes than iteration N-1 (different hash)
- Cache only works for re-running ENTIRE backtests from scratch (notes reset on new instantiation)
- Memory is capped at 20 most recent notes, with configurable char limit (`LUMIBOT_AGENT_MEMORY_NOTE_MAX_CHARS`, default 2000)

---

## Correct Procedures

### Running a Backtest

```python
from datetime import datetime
from lumibot.strategies import Strategy
from lumibot.backtesting import YahooDataBacktesting
from lumibot.entities import Asset, TradingFee

class MyStrategy(Strategy):
    def initialize(self):
        self.sleeptime = "1D"
        self.agents.create(
            name="trader",
            model="openai/glm-5-turbo",
            allow_trading=True,
            system_prompt="Your trading instructions here.",
        )

    def on_trading_iteration(self):
        now = self.get_datetime()
        self.agents["trader"].run(
            task_prompt=f"Current datetime: {now.isoformat()}. Analyze and trade.",
        )

result = MyStrategy.backtest(
    YahooDataBacktesting,
    backtesting_start=datetime(2025, 1, 1),
    backtesting_end=datetime(2025, 3, 31),
    benchmark_asset=Asset("SPY"),
    buy_trading_fees=[TradingFee(percent_fee=0.001)],
    sell_trading_fees=[TradingFee(percent_fee=0.001)],
    quote_asset=Asset("USD", Asset.AssetType.FOREX),
    budget=10000,
    name="my_strategy_v1",  # MUST be consistent for replay cache
)
```

### Replay Cache Rules

- **CRITICAL:** The `name=` parameter MUST be identical between runs for cache to work
- `name` feeds into `runtime_context["strategy_name"]` which is part of the SHA-256 cache key hash
- Different names = different hashes = cache miss every time
- After the first live run, subsequent identical runs should complete in <1 second for a 10-day backtest

### Model Configuration

| Model | Prefix | Base URL | Notes |
|---|---|---|---|
| GLM-5 Turbo | `openai/glm-5-turbo` | `https://api.z.ai/api/coding/paas/v4` | Current default, good tool calling |
| GLM-5 Turbo (general) | `openai/glm-5-turbo` | `https://api.z.ai/api/paas/v4` | May be better for trading agents |
| Gemini 3 Flash | `openai/gemini-3-flash-preview` | `https://ollama.com/v1` | Via Ollama Cloud, requires `openai/` prefix |

**z.ai has TWO endpoints:**
- **Coding:** `https://api.z.ai/api/coding/paas/v4` — optimized for code generation tools
- **General:** `https://api.z.ai/api/paas/v4` — standard LLM API, may be better for trading agents

### Environment Setup

Before running any LumiBot code:
```bash
cd /home/Zev/development/trading-bots/lumibot
source .venv/bin/activate
export LUMIBOT_CACHE_FOLDER=/tmp/lumibot_cache
```

Or in Python:
```python
import sys, os
sys.path.insert(0, "/home/Zev/development/trading-bots/lumibot")
from dotenv import load_dotenv
load_dotenv("/home/Zev/development/trading-bots/lumibot/.env")
os.environ.setdefault("LUMIBOT_CACHE_FOLDER", "/tmp/lumibot_cache")
```

---

## Known Issues & Gotchas

### Context Window Overflow
- GLM-5 context grows ~100K+ tokens per iteration due to accumulated tool results and memory notes
- Backtests >2 months at daily frequency will likely overflow
- **Fix:** Use 1-2 month windows, or reduce tool calls per iteration
- Thinking tokens add 2-5K per iteration for reasoning models — not the main issue

### Limit Order Chasing Problem
- The agent tends to place limit orders well below current price
- When price moves up, the limit doesn't fill
- Agent cancels and places new limit at higher price, repeating daily
- **Fix:** Use market orders for initial entries, or set tighter limit spreads in prompt

### Agent Over-Conservatism
- Default LumiBot system prompt is very conservative ("do not trade for the sake of activity")
- With daily sleeptime, the agent sees only daily bars → less conviction → more HOLD CASH
- **Fix:** Override default behavior in your system prompt, or use intraday data for more signals

### LiteLLM Provider Prefix
- All models go through LiteLLM with `openai/` prefix
- Gemini Flash via Ollama Cloud: model = `openai/gemini-3-flash-preview` (NOT `ollama-cloud/`)
- LiteLLM doesn't recognize `ollama-cloud/` provider prefix

---

## Environment Flags (set in nexus-trade/.env)

| Flag | Default | Purpose |
|---|---|---|
| `NEXUS_LANCEDB_DIR` | `/home/Zev/development/agentic-quant-os/data/vectors` | Canonical LanceDB persist dir. Used by both bridge and nexus_vector_memory. Do not change. |
| `NEXUS_BRIDGE_AUTO_SYNC` | `0` (off) | When `1`, nexus_committee runs the bridge subprocess after each iteration. Requires `NEXUS_VENV_AQOS`. |
| `NEXUS_VENV_AQOS` | `/home/Zev/development/agentic-quant-os/.venv/bin/python` | Path to the aqos venv Python that has sentence-transformers + lancedb. |
| `NEXUS_REGIME_CHAMPION_ONLY` | `0` (off) | When `1`, PM agent reads `v_nexus_regime_champion` (prefers `closed_loop`) instead of `v_nexus_regime`. |

**To enable both new features for a smoke test:**
```bash
export NEXUS_BRIDGE_AUTO_SYNC=1
export NEXUS_REGIME_CHAMPION_ONLY=1
```
(Or edit nexus-trade/.env and set both to `1`.)

## Available Tools (40+ built-in)

LumiBot agents have access to these tool categories:

| Category | Tools | Status in Nexus Trader |
|---|---|---|
| Account | positions, portfolio, cash | ✅ Used |
| Market | last_price, load_history, load_history_table | ✅ Used |
| DuckDB | query (direct SQL on price data) | ✅ Used |
| Indicators | get_indicators, get_indicator, signal_dashboard | ✅ Used |
| Orders | submit_order, cancel_order, open_orders | ✅ Used |
| FRED Macro | fred_snapshot, fred_latest | ⚠️ Available but untested for time filtering |
| SEC/Fundamentals | sec_filings, company_info | ⚠️ Available but untested for time filtering |
| News | alpaca_news (requires Alpaca keys) | ❌ Not configured |
| Memory/Thesis | remember_decision, remember_lesson, remember_thesis, query_theses | ⚠️ Partially used |
| Notifications | notify (email/webhook) | ❌ Not configured |
| Docs | query_docs, search_docs | ⚠️ Available |

---

## Key Source Files in LumiBot

| File | Purpose | Key Lines |
|---|---|---|
| `components/agents/replay_cache.py` | Cache key hashing, load/save | `compute_key()` at L58 |
| `components/agents/manager.py` | Agent orchestration, cache integration | `_cache_payload()` at L875, `run()` at L1158, `_runtime_context()` at L609 |
| `components/agents/runtime.py` | ADK runner, LLM calls | `_run_async()` at L718, thinking planner |
| `components/agents/duckdb_tools.py` | DuckDB time wall | `_create_visible_view()` at L169 |
| `components/agents/schemas.py` | Data schemas (RuntimeRequest, etc.) | Full file |

---

## Documentation Structure

| File | Purpose |
|---|---|
| `docs/EXECUTION_ROADMAP.md` | Master execution plan (Phases 0-12) — START HERE |
| `docs/PRD.md` | Product requirements document |
| `docs/ROADMAP_legacy.md` | Original roadmap (superseded by EXECUTION_ROADMAP) |
| `docs/NEXUS_TRADER_ROADMAP.md` | Older project roadmap reference |
| `docs/BRAINSTORM_CHATLOG.md` | Raw brainstorm conversation with all feature ideas |
| `docs/INTEGRATION_MAP.md` | Integration map for 8 quant repos |
| `docs/AUDIT_REPORT.md` | Tool and feature audit |
| `docs/MODEL_GUIDE.md` | Model selection guide |
| `docs/TOOL_CALLING_BENCHMARKS.md` | Tool calling benchmarks |
| `docs/LUMIBOT_RESEARCH.md` | Deep research on LumiBot internals |
| `docs/research_*.md` | Financial data, backtesting, agent systems research |
| `src/strategies/nexus_committee.py` | 4-agent committee strategy |
| `src/memory/` | Vector memory bridge + LanceDB storage |
| `src/tools/` | Custom tools (trade_memory_tool) |
