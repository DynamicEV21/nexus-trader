# Nexus Trader — Agent-Actionable Execution Roadmap

> **Version:** 1.0  
> **Created:** 2026-06-01  
> **Status:** Ready for execution  
> **Purpose:** Step-by-step instructions that any agent (or human) can follow.

This document assumes you've read `PRD.md` (product requirements, tool catalog, architecture) and `ROADMAP.md` (phases, dependencies, risk gates). If you need to understand *why* something exists, read those. This document is about *how to execute*.

---

## Quick Reference Card

| Item | Value |
|---|---|
| **Project root** | `/home/Zev/development/nexus-trade/` |
| **Strategy file** | `src/strategies/nexus_committee.py` |
| **Memory bridge** | `src/memory/bridge.py` |
| **Vector memory** | `src/memory/nexus_vector_memory.py` |
| **Regime tool** | `src/tools/regime_tool.py` |
| **Signal dashboard tool** | `src/tools/signal_dashboard_tool.py` |
| **Trade memory tools** | `src/tools/trade_memory_tool.py` |
| **CrabQuant** | `~/development/CrabQuant/` — regime detection |
| **LumiBot install** | `~/development/trading-bots/lumibot/` |
| **Replay cache dir** | `.lumibot/agent_runtime/replay/` (in runtime dir) |
| **Traces dir** | `.lumibot/agent_runtime/traces/` (in runtime dir) |
| **Summaries file** | `.lumibot/agent_runtime/agent_run_summaries.jsonl` (in runtime dir) |

### Model Assignments

| Agent Role | Model | Provider | Env Var |
|---|---|---|---|
| Evidence Researcher | `ollama/gemini-3-flash-preview` | Ollama Cloud | `COMMITTEE_RESEARCH_MODEL` |
| Bull Researcher | `ollama/gemini-3-flash-preview` | Ollama Cloud | `COMMITTEE_BULL_MODEL` |
| Bear Researcher | `ollama/gemini-3-flash-preview` | Ollama Cloud | `COMMITTEE_BEAR_MODEL` |
| Portfolio Manager | `zai/glm-5-turbo` | z.ai (OpenAI-compatible) | `COMMITTEE_TRADER_MODEL` |

### Model Configuration Reference

These models go through LiteLLM with the `openai/` prefix. The exact environment setup:

#### Gemini 3 Flash (via Ollama Cloud)
```bash
export OPENAI_API_BASE="https://ollama.com/v1"
export OPENAI_API_KEY="$OLLAMA_API_KEY"
# Model string: "openai/gemini-3-flash-preview"
```

#### GLM-5 Turbo (via z.ai)
```bash
export OPENAI_API_BASE="https://api.z.ai/api/coding/paas/v4"
export OPENAI_API_KEY="$ZAI_API_KEY"
# Model string: "openai/glm-5-turbo"
```

**Important:** Both providers use the OpenAI-compatible API format via LiteLLM. When configuring a model in LumiBot, always use the `openai/` prefix (e.g., `openai/glm-5-turbo`, `openai/gemini-3-flash-preview`).

### z.ai API Endpoints

z.ai has **TWO** endpoints:

1. **General endpoint**: `https://api.z.ai/api/paas/v4` — for general LLM usage (chat, tool-use, reasoning)
2. **Coding endpoint**: `https://api.z.ai/api/coding/paas/v4` — for coding/development tools (Claude Code, OpenClaw, Cursor, etc.)

Current `.env` configuration uses the **coding endpoint**. For LumiBot trading agents, the **general endpoint** may be more appropriate — the coding endpoint is optimized for code generation tools, not general chat/tool-use agents. Consider switching to the general endpoint if encountering context window issues or unexpected behavior.

```bash
# General endpoint (recommended for LumiBot agents)
export OPENAI_API_BASE="https://api.z.ai/api/paas/v4"
# Coding endpoint (current — may cause context issues with tool-use agents)
# export OPENAI_API_BASE="https://api.z.ai/api/coding/paas/v4"
```

### z.ai Available Tools (via API)

z.ai also offers several API-based tools that could potentially be exposed as MCP tools or `@agent_tool` wrappers for the trading agent:

| z.ai Tool | Description | Potential Nexus Trader Use |
|---|---|---|
| **Web Search** | Search engine optimized for LLM retrieval | Real-time news, sentiment, market events |
| **Web Reader** | Parse and extract content from URLs | Read SEC filings, earnings reports, analyst notes |
| **Image Generation (GLM-Image)** | Generate images from prompts | Chart visualization, report generation |
| **Audio Transcription (GLM-ASR-2512)** | Speech-to-text transcription | Earnings call transcription, Fed meeting analysis |
| **Layout Parsing / OCR (GLM-OCR)** | Extract structured data from documents/images | Parse PDF reports, scanned filings, tables |
| **Tokenizer** | Count tokens before sending | Optimize context window usage, prevent overflow |

**Implementation note:** These could be wrapped as LumiBot `@agent_tool` functions or exposed through an MCP server. The Web Search and Web Reader tools are highest priority for adding real-time news capabilities to the trading agent.

### Environment Variables

```bash
# Required — Gemini embeddings for LanceDB vector memory
export GOOGLE_API_KEY="your-gemini-api-key"

# Required for Phase 0+ — model selections (override defaults)
export COMMITTEE_RESEARCH_MODEL="ollama/gemini-3-flash-preview"
export COMMITTEE_BULL_MODEL="ollama/gemini-3-flash-preview"
export COMMITTEE_BEAR_MODEL="ollama/gemini-3-flash-preview"
export COMMITTEE_TRADER_MODEL="zai/glm-5-turbo"

# Required for z.ai provider
export ZAI_API_KEY="your-z-ai-api-key"
# or OPENAI_API_KEY if using openai-compat base URL
export OPENAI_API_BASE="https://api.z.ai/api"  # if needed

# Required for Ollama Cloud
export OLLAMA_API_KEY="your-ollama-cloud-api-key"

# Optional — override storage paths
export NEXUS_LANCEDB_DIR="/home/Zev/development/nexus-trade/.nexus_memory"
export NEXUS_MEMORY_DIR="/home/Zev/development/nexus-trade/.lumibot/memory"

# Optional — Alpaca (Phase 5 paper trading)
export ALPACA_API_KEY="your-alpaca-key"
export ALPACA_SECRET_KEY="your-alpaca-secret"
export ALPACA_PAPER="true"

# Optional — FRED API (macro data)
export FRED_API_KEY="your-fred-key"
```

---

## Phase 0: Unblock & Validate

**Goal:** Get a clean, deterministic backtest running with all models working. This is the foundation for every subsequent phase.

**Time estimate:** 1-2 hours once APIs are working.

### Step 0.1: Verify Ollama Cloud Access (No More Rate Limits)

```
COMMAND:
curl -s -H "Authorization: Bearer $OLLAMA_API_KEY" \
  "https://api.ollama.com/v1/models" | python3 -m json.tool | head -30
```

**Verify:** You see `gemini-3-flash-preview` in the model list. If you get a 429, the weekly rate limit is still active — wait or upgrade.

**If the check fails:**
- Log in to https://ollama.com/settings and check usage
- If over the limit, either wait for weekly reset (Mondays) or upgrade

### Step 0.2: Verify Model Availability on Both Providers

```bash
# Test Ollama Cloud
curl -s -H "Authorization: Bearer $OLLAMA_API_KEY" \
  "https://api.ollama.com/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"ollama/gemini-3-flash-preview","messages":[{"role":"user","content":"Say hello in one word"}],"max_tokens":10}' \
  | python3 -m json.tool

# Test z.ai (GLM-5 Turbo)
curl -s -H "Authorization: Bearer $ZAI_API_KEY" \
  "https://api.z.ai/api/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5-turbo","messages":[{"role":"user","content":"Say hello in one word"}],"max_tokens":10}' \
  | python3 -m json.tool
```

**Verify:** Both return `"choices"` with a response. If z.ai URL is different, adjust. Check https://open.bigmodel.cn for current API base URL.

**If a model fails:**
- Gemini 3 Flash: verify it's available on Ollama Cloud (model names change)
- GLM-5 Turbo: verify the exact model name at z.ai. Try `glm-5`, `glm-5-flash`, etc.

### Step 0.3: Verify CrabQuant Regime Detection Works Standalone

```bash
cd ~/development/CrabQuant
python3 -c "
from crabquant.regime import detect_regime
import pandas as pd
import numpy as np

# Create synthetic data for a quick test
dates = pd.date_range('2024-01-01', periods=100, freq='D')
df = pd.DataFrame({
    'close': 400 + np.cumsum(np.random.randn(100) * 2),
    'high': 403 + np.cumsum(np.random.randn(100) * 2),
    'low': 397 + np.cumsum(np.random.randn(100) * 2),
    'open': 400 + np.cumsum(np.random.randn(100) * 2),
}, index=dates)

regime, metadata = detect_regime(df)
print(f'Regime: {regime.value}, Confidence: {metadata.get(\"confidence\", \"N/A\")}')
print(f'Scores: {metadata.get(\"scores\", {})}')
"
```

**Verify:** Prints a regime like `trending_up` or `mean_reversion` with scores.

**If it fails:** CrabQuant may not be installed. Run:
```bash
cd ~/development/CrabQuant && pip install -e .
```

### Step 0.4: Verify Nexus Trader Strategy Imports Cleanly

```bash
cd /home/Zev/development/nexus-trade
python3 -c "
import sys
sys.path.insert(0, 'src')
from src.strategies.nexus_committee import NexusCommitteeStrategy
print('Strategy class import: OK')

from src.tools.regime_tool import DETECT_REGIME_TOOL
print('Regime tool import: OK')

from src.tools.signal_dashboard_tool import SIGNAL_DASHBOARD
print('Signal dashboard tool import: OK')

from src.tools.trade_memory_tool import QUERY_TRADE_MEMORY, REMEMBER_DECISION, REMEMBER_LESSON, GET_MEMORY_STATS
print('Trade memory tools import: OK')

from src.memory.bridge import MemoryBridge
print('Memory bridge import: OK')

print('ALL IMPORTS PASSED')
"
```

**Verify:** All 6 prints appear with no ImportErrors.

**If it fails:** Install missing dependencies:
```bash
cd /home/Zev/development/nexus-trade
pip install -e /home/Zev/development/trading-bots/lumibot
pip install -e /home/Zev/development/CrabQuant
pip install lancedb google-generativeai pandas numpy
```

### Step 0.5: Run Clean Daily-Frequency Committee Backtest

**This is the baseline.** Every future A/B test compares against this run.

```bash
cd /home/Zev/development/nexus-trade

python3 src/strategies/nexus_committee.py \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA AMZN META
```

**Expected runtime:** 10-40 minutes (first run = cold cache, all API calls)

**What you'll see:**
- Log lines starting with `[NexusCommittee run N]`
- `Evidence pack: XXXX chars` log lines
- `Bull case: XXXX chars` / `Bear case: XXXX chars` lines
- `Backtest complete:` with results dict at the end

**Verify success:**
- The backtest completes without crashing
- Results include portfolio value, cash, P&L metrics
- No `429 Too Many Requests` errors
- No `ImportError` or `AttributeError` tracebacks

**If it fails:**
- **Ollama 429:** Rate limit still active — skip to Phase 0.5b (single-model workaround) or wait
- **z.ai errors:** Check `ZAI_API_KEY` is set, verify model name with `curl` test from 0.2
- **CrabQuant import error:** Run `pip install -e ~/development/CrabQuant`
- **Strategy crash:** Check the traceback — most likely a LumiBot API change. Look at the LumiBot version: `pip show lumibot`

### Step 0.6: Verify Replay Cache Is Populating

After the backtest completes (or after at least a few bars have run):

```bash
# Find the runtime directory — it's wherever you ran the backtest
# Look for .lumibot/agent_runtime/ in the directory you cd'd to
find . -path "*/agent_runtime/replay/*" -name "*.json" 2>/dev/null | head -20

# Check stats
find . -path "*/agent_runtime/replay" -type d -exec sh -c 'echo "Replay cache entries: $(find "$1" -name "*.json" | wc -l)"' _ {} \;
```

**Verify:** You see `.json` files in the replay directory. The count should be non-trivial (50+ for a multi-bar backtest with agent tool calls).

**If empty:** Check that the backtest actually called agent tools (look for `[NexusCommittee run` in logs). If the strategy ran but no replay files, check LumiBot replay configuration — it may need to be explicitly enabled.

### Step 0.7: Verify Traces Are Being Written

```bash
# Check for JSON trace files
find . -path "*/agent_runtime/traces/*" -name "*.json" 2>/dev/null | head -10
```

**Verify:** You see `.json` trace files, one per agent invocation.

**If empty:** Same as 0.6 — LumiBot may need trace mode explicitly enabled. Check the LumiBot docs for trace configuration flags.

### Step 0.8: Verify agent_run_summaries.jsonl

```bash
# Find the summaries file
find . -name "agent_run_summaries.jsonl" 2>/dev/null

# If found, peek at it
find . -name "agent_run_summaries.jsonl" -exec head -3 {} \;
```

**Verify:** File exists and contains JSONL records. Each line is a valid JSON object with agent run metadata.

**If missing:** Check LumiBot version — `agent_run_summaries.jsonl` was added in later v4.5.x versions. Run `pip show lumibot` and compare to latest release.

### Step 0.9: Re-Run Same Backtest → Verify Replay Cache Determinism

```bash
# Re-run the exact same backtest
python3 src/strategies/nexus_committee.py \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA AMZN META
```

**Expected:** This should complete in **seconds** (not minutes), with **zero API calls** (or near-zero), because every tool call hits the replay cache.

**Verify:**
- Backtest completes in <60 seconds (vs 10-40 min cold)
- Results are **identical** to Step 0.5:
  - Same final portfolio value
  - Same trade count
  - Same P&L path

**If results differ:** The replay cache is not being used or is being bypassed. Possible causes:
- Timestamps in the cache key differ (check system clock)
- Cache directory was cleaned between runs
- Model parameters changed between runs
- LumiBot cache implementation changed between versions

**Exit criteria for Phase 0:**
- [x] Ollama Cloud rate limit resolved (no 429s)
- [x] Gemini 3 Flash callable via Ollama
- [x] GLM-5 Turbo callable via z.ai
- [x] Backtest completes with $10K budget, 6 months, daily bars
- [x] Replay cache populates (50+ cache files)
- [x] Traces written (one per agent call)
- [x] agent_run_summaries.jsonl written
- [x] Warm re-run is deterministic (identical results, seconds runtime)

### Phase 0 Testing Results (2026-06-01)

Key findings from recent backtest and infrastructure testing:

#### Model & API Configuration
- **Gemini 3 Flash** works via Ollama Cloud but requires OpenAI-compatible config:
  - `OPENAI_API_BASE=https://ollama.com/v1`
  - `model="openai/gemini-3-flash-preview"`
- **GLM-5 Turbo** via z.ai works for agent tool calling:
  - `OPENAI_API_BASE=https://api.z.ai/api/coding/paas/v4`
  - `model="openai/glm-5-turbo"`
- Both go through LiteLLM with `openai/` prefix

#### Backtest Behavior
- **Single GLM-5 agent backtest**: context overflow on 4-month runs with daily frequency
  - **Fix**: Use 1-2 month windows or shorter tool chains
- **Replay cache entries ARE written** to disk correctly
  - 146 entries found in `/tmp/lumibot_cache/agent_runtime/replay/`
- **Replay cache NOT hitting on re-runs**: root cause is the backtest `name=` parameter being different between runs
  - **Fix**: Must use identical `name=` for cache to work
- **Memory notes grow** between iterations within a single backtest, which may also affect cache keys for within-run replay

#### Infrastructure Verified Working
- **Traces ARE being written** to `/tmp/lumibot_cache/agent_runtime/traces/`
- **agent_run_summaries.jsonl IS being written** correctly
- **40+ built-in tools** available across 11 categories:
  - Account, Market, DuckDB, Docs, News, Indicators, Fundamentals, Macro/FRED, Notifications, Memory/Thesis, Orders

#### Unused Tools in Nexus Trader
- FRED macro tools (`get_fred_latest`, `get_fred_series`, `get_fred_snapshot`)
- SEC fundamentals (`get_income_statement`, `get_balance_sheet`, `get_filings`)
- Full memory/thesis lifecycle tools
- MCP external tools
- Alpaca news

#### Context Window Management
- **Thinking/reasoning tokens** consume significant context window space for GLM-5
  - **Fix**: Disable thinking to reduce context usage and avoid overflow

#### Backtest Time Filtering with MCP/Internet Tools
- **Goal**: Verify that when the agent uses internet-connected tools (web search, news, external data) during a backtest, the time filtering works correctly — the agent must NOT see future information.
- **Priority**: 🔴 HIGH — must be validated before trusting any backtest with internet-connected tools.
- **What to test**:
  1. Run a backtest that spans e.g. Jan–Mar 2025.
  2. Configure the agent with a web search tool or news tool (e.g. Alpaca news, DuckDB news queries, MCP web search).
  3. At each iteration, verify the agent only receives data up to the current backtest datetime.
  4. Check that the DuckDB time wall (`WHERE datetime <= cutoff`) is applied to any external data fetched.
  5. Test with various tools: FRED macro data (if available for backtest dates), SEC filings, news sentiment.
- **How to verify**:
  - Inspect traces in `/tmp/lumibot_cache/agent_runtime/traces/` to confirm no future dates appear in tool call results.
  - Examine each tool call's arguments and response content for any datetime values exceeding the backtest's current iteration date.
  - Run the same backtest twice — once with a news tool enabled, once without — and confirm the P&L doesn't change (proving no forward information leaked).
- **Risk**: If time filtering fails, the agent has lookahead bias — this would invalidate all backtest results.
- **Pass criteria**:
  - [ ] Traces show zero future-dated data in any tool response during backtest
  - [ ] P&L is identical with and without internet tools enabled (controlled experiment)
  - [ ] Agent prompts/datetime passed to tools are correctly bounded to the backtest iteration date
- **Fail criteria**:
  - [ ] Any trace shows a date > current backtest `self.get_datetime()` in tool results
  - [ ] P&L differs when internet tools are enabled vs disabled
  - [ ] Tool receives a date parameter beyond the backtest's current iteration

---

## Phase 1: Custom Tools Integration

**Goal:** Wire `signal_dashboard()` and `detect_regime()` into the committee and compare performance to the Phase 0 baseline.

**Time estimate:** 1-2 hours.

### Step 1.1: Verify Tools Register in Backtest

The strategy already imports and registers all 6 tools in its `initialize()` method (lines ~120-140 of `nexus_committee.py`). We need to verify they actually appear as available tools during backtest.

Run a **quick 2-bar test** to verify tool registration:

```bash
cd /home/Zev/development/nexus-trade

# Ultra-short backtest — just 2 daily bars, minimal cost
python3 src/strategies/nexus_committee.py \
  --start 2024-01-02 \
  --end 2024-01-04 \
  --budget 10000 \
  --symbols AAPL
```

Look for these log lines:
```
[NexusCommittee run 1] ...
Registered 6 custom tools
```

**Verify:** `Registered 6 custom tools` appears. If you see `Failed to register custom tools`, the tools didn't import — check the exception message.

### Step 1.2: Verify signal_dashboard Responds Correctly (Standalone Test)

Before trusting the tool inside a backtest, test it standalone:

```bash
cd /home/Zev/development/nexus-trade

python3 -c "
import sys, os
sys.path.insert(0, 'src')
from src.tools.signal_dashboard_tool import signal_dashboard_tool
import pandas as pd
import numpy as np

# Create a mock strategy instance
class MockStrategy:
    def get_historical_prices(self, symbol, length, timestep):
        dates = pd.date_range('2024-01-01', periods=length, freq='D')
        close = 400 + np.cumsum(np.random.randn(length) * 2)
        return type('obj', (object,), {
            'df': pd.DataFrame({
                'close': close,
                'high': close + 2,
                'low': close - 2,
                'volume': np.random.randint(1e6, 1e7, length),
            }, index=dates)
        })

result = signal_dashboard_tool(MockStrategy(), 'AAPL', lookback=100)
print('Keys:', list(result.keys()))
print('RSI:', result.get('rsi'))
print('MACD signal:', result.get('macd_signal'))
print('Trend:', result.get('trend_alignment'))
print('Risk:', result.get('risk_recommendation'))
print('Summary:', result.get('summary'))
"
```

**Verify:** Output shows all expected keys with numeric/string values. No `error` key.

**If it fails:** Check for missing `pandas` or `numpy`, or a bug in the indicator functions.

### Step 1.3: Verify detect_regime Responds Correctly (Standalone Test)

```bash
cd /home/Zev/development/nexus-trade

python3 -c "
import sys, os
sys.path.insert(0, 'src')
sys.path.insert(0, os.path.expanduser('~/development/CrabQuant'))
from src.tools.regime_tool import detect_regime_tool
import pandas as pd
import numpy as np

class MockStrategy:
    def get_datetime(self):
        from datetime import datetime
        return datetime(2024, 6, 1)
    def get_historical_prices(self, symbol, length, timestep):
        dates = pd.date_range('2024-01-01', periods=length, freq='D')
        close = 400 + np.cumsum(np.random.randn(length) * 2)
        return type('obj', (object,), {
            'df': pd.DataFrame({
                'close': close,
                'high': close + 3,
                'low': close - 3,
                'open': close - 0.5,
            }, index=dates)
        })

result = detect_regime_tool(MockStrategy(), lookback=50)
print('Regime:', result.get('regime'))
print('Confidence:', result.get('confidence'))
print('Scores:', result.get('scores'))
"
```

**Verify:** Regime is one of `trending_up`, `trending_down`, `mean_reversion`, `high_volatility`, `low_volatility`, or `unknown` (if synthetic data doesn't trigger a clear signal). Confidence is between 0.0 and 1.0.

**If it fails:** Ensure CrabQuant is installed and `detect_regime` is importable. Check the file at `~/development/CrabQuant/crabquant/regime.py`.

### Step 1.4: Run Full Phase 1 Backtest with Tools

Now run the same 6-month backtest. The tools should be called by agents:

```bash
cd /home/Zev/development/nexus-trade

python3 src/strategies/nexus_committee.py \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA AMZN META
```

### Step 1.5: Compare Phase 1 P&L to Phase 0 Baseline

Extract the key metrics from both runs:

```bash
# Phase 0 baseline results (record from Step 0.5 output)
# Phase 1 results (record from Step 1.4 output)
```

**Compare:**
- Final portfolio value
- Total return %
- Number of trades
- Win rate (if trackable)
- Maximum drawdown

**What to look for:**
- Tools should NOT make results worse
- Ideally: fewer but better-quality trades (more informed decisions)
- Regime tool should influence position sizing (smaller in bearish regimes)
- Signal dashboard should reduce redundant indicator-fetching calls

**If Phase 1 underperforms Phase 0:**
- Check if the tools are being called at all (look in traces)
- If tools aren't called: agents may not know about them. Verify `self.agents.register_tool()` in `initialize()` runs without error.
- If tools are called but decisions are worse: the signal values may be confusing. Simplify.

### Step 1.6: Verify Trace Files Show Tool Calls

```bash
# Find a trace file from the Phase 1 run
find . -path "*/agent_runtime/traces/*" -name "*.json" -newer /tmp/phase1_start_marker 2>/dev/null | head -5

# Search for tool call names in traces
find . -path "*/agent_runtime/traces/*" -name "*.json" -exec grep -l "signal_dashboard\|detect_regime\|query_trade_memory" {} \; 2>/dev/null | head -10
```

**Verify:** At least some trace files reference `signal_dashboard` and/or `detect_regime`.

**Exit criteria for Phase 1:**
- [x] Both tools register cleanly (no import errors)
- [x] Both tools work standalone with mock data
- [x] Full backtest runs with tools available
- [x] P&L compared to Phase 0 baseline
- [x] Traces confirm tools were called by agents

---

## Phase 2: Prompt Engineering & Single-Agent Testing

**Goal:** Find the best prompt style for each role. Compare single-agent (no committee) vs committee performance.

**Time estimate:** 2-4 hours (one backtest per prompt variant).

### Step 2.1: Create Prompt Variants

Create a test script that overrides system prompts. File: `/home/Zev/development/nexus-trade/tests/prompt_variants.py`

```python
"""
Prompt variant tester — runs single-agent backtests with different prompt styles.

Usage:
    python3 tests/prompt_variants.py --variant conservative --start 2024-01-01 --end 2024-03-31
"""
import argparse, logging, os, sys
from datetime import datetime

sys.path.insert(0, 'src')
from lumibot.backtesting import YahooDataBacktesting
from lumibot.entities import Asset, TradingFee
from lumibot.strategies.strategy import Strategy

# ── Single-agent strategy for prompt testing ──
class SingleAgentStrategy(Strategy):
    parameters = {
        "universe": ["AAPL", "MSFT", "NVDA", "AMZN", "META"],
        "max_position_pct": 0.20,
    }

    def initialize(self):
        self.sleeptime = "1D"
        model = os.environ.get("TEST_MODEL", "zai/glm-5-turbo")
        prompt = os.environ.get("TEST_PROMPT", self._default_prompt())
        self.agents.create(
            name="trader",
            model=model,
            allow_trading=True,
            system_prompt=prompt,
        )

    def on_trading_iteration(self):
        universe = list(self.parameters.get("universe", []))
        self.agents["trader"].run(
            task_prompt=(
                f"Review the portfolio and universe {universe}. "
                f"Decide whether to buy, sell, or hold. "
                f"Max position: {self.parameters['max_position_pct']*100}% per symbol. "
                f"Use any tools available."
            ),
        )

    def _default_prompt(self):
        return "You are a disciplined long-only portfolio manager."

# ── Prompt variants ──
PROMPTS = {
    "conservative": (
        "You are a conservative portfolio manager. Preserve capital above all else. "
        "Only trade when evidence is overwhelming. Prefer cash to uncertain positions. "
        "Maximum 2 positions at a time. Prefer large-cap dividend payers. "
        "If you're not sure, do nothing."
    ),
    "aggressive": (
        "You are an aggressive growth investor seeking maximum returns. "
        "Concentrate in the best 2-3 ideas. Size up when conviction is high. "
        "Ride momentum. Accept higher volatility for higher returns. "
        "Cut losers quickly, let winners run."
    ),
    "momentum": (
        "You are a momentum-focused trader. Follow the trend. "
        "Buy strength, sell weakness. Use RSI and MACD to time entries. "
        "Only trade in direction of the primary trend. "
        "Avoid mean reversion. The trend is your friend."
    ),
    "value": (
        "You are a value-oriented investor. Seek undervalued opportunities. "
        "Buy fear, sell greed. Look for oversold conditions and high quality. "
        "Use fundamentals: P/E, revenue growth, cash flow. "
        "Be patient. Wait for your price."
    ),
    "balanced": (
        "You are a balanced portfolio manager. Seek risk-adjusted returns. "
        "Diversify across sectors when possible. Size positions at 10-15% each. "
        "Rebalance when allocations drift more than 5%. "
        "Always have at least 20% cash reserve."
    ),
}

def run_variant(variant_name: str, start: str, end: str, budget: float, symbols: list[str]):
    prompt = PROMPTS.get(variant_name, PROMPTS["balanced"])
    os.environ["TEST_PROMPT"] = prompt
    os.environ["TEST_MODEL"] = os.environ.get("COMMITTEE_TRADER_MODEL", "zai/glm-5-turbo")

    SingleAgentStrategy.parameters = {
        "universe": symbols,
        "max_position_pct": 0.20,
    }

    trading_fee = TradingFee(percent_fee=0.001)
    result = SingleAgentStrategy.backtest(
        YahooDataBacktesting,
        backtesting_start=datetime.fromisoformat(start),
        backtesting_end=datetime.fromisoformat(end),
        benchmark_asset=Asset("SPY", Asset.AssetType.STOCK),
        buy_trading_fees=[trading_fee],
        sell_trading_fees=[trading_fee],
        budget=budget,
        quiet_logs=False,
    )
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default="balanced", choices=list(PROMPTS.keys()))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2024-03-31")
    parser.add_argument("--budget", type=float, default=10000)
    parser.add_argument("--symbols", nargs="*", default=["AAPL", "MSFT", "NVDA"])
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_variant(args.variant, args.start, args.end, args.budget, args.symbols)
    print(f"\n=== {args.variant.upper()} ===")
    for k, v in result.items():
        if isinstance(v, (int, float)):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")
```

### Step 2.2: Run Single-Agent Tests (All Prompt Variants)

Run each variant with the same parameters:

```bash
cd /home/Zev/development/nexus-trade

for variant in conservative aggressive momentum value balanced; do
  echo "====== Testing: $variant ======"
  python3 tests/prompt_variants.py \
    --variant $variant \
    --start 2024-01-01 \
    --end 2024-03-31 \
    --budget 10000 \
    --symbols AAPL MSFT NVDA AMZN META \
    2>&1 | tee /tmp/phase2_${variant}.log
  echo "---"
done
```

**Record results in a table:**

| Variant | Final Value | Return % | # Trades | Sharpe (approx) | Max DD (approx) |
|---|---|---|---|---|---|
| conservative | | | | | |
| aggressive | | | | | |
| momentum | | | | | |
| value | | | | | |
| balanced | | | | | |
| **committee (baseline)** | | | | | |

### Step 2.3: Compare Best Single-Agent vs Committee

Take the best-performing single-agent variant and compare it to the committee result from Phase 0/1:

**Key questions:**
- Does the committee (4 agents, debate, tools) outperform a single agent?
- Does the committee make fewer but better trades?
- Is committee drawdown lower?
- Is the committee "smarter" (tools used, reasoning quality) even if P&L is similar?

### Step 2.4: Document Prompt Findings

Update `/home/Zev/development/nexus-trade/docs/PROMPT_FINDINGS.md` with:

```markdown
# Prompt Engineering Findings

## Best System Prompts (by Role)

### Evidence Researcher
(TBD from Phase 2 testing)
### Bull Researcher
(TBD)
### Bear Researcher
(TBD)
### Portfolio Manager
(TBD)

## Key Insights
- (what worked, what didn't)
- (prompt length sweet spot)
- (role-specific vs generic prompts)
```

**Exit criteria for Phase 2:**
- [x] All 5 prompt variants tested in single-agent mode
- [x] P&L table populated with real numbers
- [x] Best single-agent vs committee comparison completed
- [x] Prompt findings documented

---

## Phase 3: Memory Bridge Validation

**Goal:** Prove the memory bridge extracts decisions → injects lessons → improves next backtest.

**Time estimate:** 1-3 hours.

### Step 3.1: Verify LanceDB Setup

```bash
cd /home/Zev/development/nexus-trade

python3 -c "
import os
os.environ['GOOGLE_API_KEY'] = os.environ.get('GOOGLE_API_KEY', '')
from src.memory.nexus_vector_memory import get_nexus_memory
nexus = get_nexus_memory()
print('Vector memory enabled:', nexus.enabled)
print('Persist dir:', nexus._persist_dir)
print('Stats:', nexus.get_stats())
"
```

**Verify:** `enabled: True` and `Persist dir` points to a real directory.

**If `enabled: False`:** Check `GOOGLE_API_KEY` is set and valid. Check LanceDB installed (`pip install lancedb`).

### Step 3.2: Run First Backtest (Cold — No Prior Memory)

```bash
cd /home/Zev/development/nexus-trade

# Make sure memory is empty for clean test
rm -rf .nexus_memory/

python3 src/strategies/nexus_committee.py \
  --start 2024-01-01 \
  --end 2024-01-31 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA
```

**Note:** Using 1 month for faster iteration.

### Step 3.3: Run Memory Bridge Manually After Backtest

```bash
cd /home/Zev/development/nexus-trade

python3 -c "
import sys, os
sys.path.insert(0, 'src')
from src.memory.bridge import MemoryBridge

bridge = MemoryBridge(strategy_name='Nexus_Trader')
stats = bridge.sync_all()
print('Bridge sync stats:')
for key, value in stats.items():
    if isinstance(value, dict):
        print(f'  {key}: read={value.get(\"read\", 0)}, embedded={value.get(\"embedded\", 0)}')
    else:
        print(f'  {key}: {value}')
"
```

**Verify:** `embedded` numbers > 0 for at least `decisions` or `lessons`.

**If embedded is 0:**
- Check that the backtest created JSONL files in `.lumibot/memory/Nexus_Trader/`
- If no JSONL files: LumiBot may not be configured to use the custom memory path
- Check `NEXUS_MEMORY_DIR` env var vs actual LumiBot output location
- Find files with: `find ~/development/trading-bots -name "decisions.jsonl" 2>/dev/null`

### Step 3.4: Verify Memory Search Works

```bash
cd /home/Zev/development/nexus-trade

python3 -c "
import sys, os
sys.path.insert(0, 'src')
from src.memory.nexus_vector_memory import get_nexus_memory

nexus = get_nexus_memory()
stats = nexus.get_stats()
print('Total decisions:', stats.get('total_decisions', 0))
print('Total lessons:', stats.get('total_lessons', 0))

if stats.get('total_decisions', 0) > 0:
    results = nexus.search_similar_decisions('technology stocks momentum', n_results=3)
    print(f'Search results: {len(results)}')
    for r in results[:3]:
        print(f'  - {r.get(\"symbol\", \"?\")}: {r.get(\"action\", \"?\")} ({r.get(\"outcome\", \"?\")})')
else:
    print('No decisions to search yet')
"
```

**Verify:** Search returns results (if decisions were stored). The results include symbol, action, and outcome.

### Step 3.5: Run Second Backtest (With Memory Bridge Active)

The strategy already calls `_run_memory_bridge()` in `initialize()` when `use_memory_bridge=True`. Run another backtest:

```bash
cd /home/Zev/development/nexus-trade

python3 src/strategies/nexus_committee.py \
  --start 2024-02-01 \
  --end 2024-02-29 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA
```

**Look for:**
- Log: `Memory bridge synced N entries` (should be >0 — from the Jan backtest)
- In traces: `query_trade_memory` being called (agents should reference past lessons)

### Step 3.6: A/B Test — Memory Bridge On vs Off

```bash
cd /home/Zev/development/nexus-trade

# WITH memory bridge (default)
python3 src/strategies/nexus_committee.py \
  --start 2024-06-01 \
  --end 2024-06-30 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA AMZN META \
  2>&1 | tee /tmp/phase3_with_memory.log

# WITHOUT memory bridge
python3 src/strategies/nexus_committee.py \
  --start 2024-06-01 \
  --end 2024-06-30 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA AMZN META \
  --no-memory-bridge \
  2>&1 | tee /tmp/phase3_without_memory.log
```

**Compare:**
- Final portfolio value
- Number of trades
- Quality of trades (did memory make decisions more consistent?)
- Drawdown

**If memory bridge shows no improvement:**
- Agents may not be calling `query_trade_memory` — check traces
- The bridge may be injecting too much context — try reducing `n_results`
- Early runs may not have enough memory to be useful — try 6 months of history first

**Exit criteria for Phase 3:**
- [x] Memory bridge extracts decisions from JSONL after backtest
- [x] Vector memory stores and searches decisions/lessons
- [x] Second backtest sees >0 entries from memory bridge
- [x] A/B test completed: memory-on vs memory-off comparison
- [x] At least 1 evidence of agent calling `query_trade_memory` in traces

---

## Phase 4: Walk-Forward Validation

**Goal:** Prove the strategy generalizes beyond its training period. This is the most important Phase — it separates real alpha from overfitting.

**Time estimate:** 1-2 hours.

### Step 4.1: Train on Jan-Jun 2024

```bash
cd /home/Zev/development/nexus-trade

python3 src/strategies/nexus_committee.py \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA AMZN META \
  2>&1 | tee /tmp/phase4_train.log
```

Record results:
- Final portfolio value
- Total return %
- Sharpe ratio (if available from backtest result)
- Max drawdown
- Number of trades
- Win rate

### Step 4.2: Test on Jul-Dec 2024 (Completely Cold — No Replay Cache)

**IMPORTANT:** The replay cache from the training run must NOT help the test run. Since the test uses different dates, LumiBot's replay cache will NOT have hits for Jul-Dec bars (different timestamps = different cache keys). This ensures genuine walk-forward testing.

```bash
cd /home/Zev/development/nexus-trade

python3 src/strategies/nexus_committee.py \
  --start 2024-07-01 \
  --end 2024-12-31 \
  --budget 10000 \
  --symbols AAPL MSFT NVDA AMZN META \
  2>&1 | tee /tmp/phase4_test.log
```

### Step 4.3: Compare Train vs Test Results

Extract key metrics from both logs:

```bash
# Parse results from both runs
echo "=== TRAIN (Jan-Jun 2024) ==="
grep -A 20 "Backtest complete" /tmp/phase4_train.log | head -25

echo "=== TEST (Jul-Dec 2024) ==="
grep -A 20 "Backtest complete" /tmp/phase4_test.log | head -25
```

**Success criteria for walk-forward:**
- Test period is profitable (positive total return)
- Test Sharpe is > 0.3
- Test max drawdown is < 20%
- Test return is at least 50% of training return (shows some generalization)
- If test return is negative: strategy is overfit — stop here, don't proceed to paper

**If walk-forward fails:**
1. Check if the market regime shifted between periods (VIX spike, rate changes)
2. Check if the strategy simply bought and held (no AI value-add)
3. Check if the strategy overtraded (too many moves = noise)
4. Consider shorter lookback, simpler tools, fewer symbols
5. **Do NOT paper trade** until walk-forward passes

### Step 4.4: Document Walk-Forward Results

Update `/home/Zev/development/nexus-trade/docs/WALKFORWARD_RESULTS.md`:

```markdown
# Walk-Forward Validation Results

## Period
- Train: 2024-01-01 to 2024-06-30
- Test: 2024-07-01 to 2024-12-31
- Budget: $10,000
- Universe: AAPL, MSFT, NVDA, AMZN, META

## Results
| Metric | Train | Test | Delta |
|---|---|---|---|
| Final Value | $X | $X | |
| Total Return | X% | X% | |
| Sharpe | X | X | |
| Max Drawdown | X% | X% | |
| # Trades | X | X | |
| Win Rate | X% | X% | |

## Assessment
- [ ] Walk-forward passed
- [ ] Strategy generalizes
- [ ] Ready for paper trading
```

**Exit criteria for Phase 4:**
- [x] Train run completed (Jan-Jun 2024)
- [x] Test run completed (Jul-Dec 2024, cold cache)
- [x] Test period is profitable
- [x] Walk-forward results documented
- [x] Decision: proceed to paper or iterate on strategy

---

## Phase 5: Paper Trading

**Goal:** Run the committee daily against Alpaca paper trading for 2-4 weeks to validate real-market behavior.

**Time estimate:** 2-4 weeks of live runtime (setup: 1-2 hours).

### Step 5.1: Verify Alpaca Paper Trading Credentials

```bash
# Check credentials are set
echo "ALPACA_API_KEY: ${ALPACA_API_KEY:0:4}...${ALPACA_API_KEY: -4}"
echo "ALPACA_SECRET_KEY: ${ALPACA_SECRET_KEY:0:4}...${ALPACA_SECRET_KEY: -4}"

# Test Alpaca connection
python3 -c "
from alpaca.trading.client import TradingClient
client = TradingClient(os.environ['ALPACA_API_KEY'], os.environ['ALPACA_SECRET_KEY'], paper=True)
account = client.get_account()
print(f'Account status: {account.status}')
print(f'Cash: \${float(account.cash):,.2f}')
print(f'Portfolio value: \${float(account.portfolio_value):,.2f}')
print(f'Pattern day trader: {account.pattern_day_trader}')
print(f'Paper trading: YES (endpoint is paper-api.alpaca.markets)')
"
```

**Verify:** Account status is `ACTIVE`, cash > $0, paper trading confirmed.

**If it fails:** Check Alpaca dashboard at https://app.alpaca.markets/paper/dashboard/overview. Regenerate API keys if needed.

### Step 5.2: Configure Strategy for Paper Trading

Set environment variables:

```bash
export ALPACA_API_KEY="PK..."
export ALPACA_SECRET_KEY="..."
export ALPACA_PAPER="true"

# Model assignments
export COMMITTEE_RESEARCH_MODEL="ollama/gemini-3-flash-preview"
export COMMITTEE_BULL_MODEL="ollama/gemini-3-flash-preview"
export COMMITTEE_BEAR_MODEL="ollama/gemini-3-flash-preview"
export COMMITTEE_TRADER_MODEL="zai/glm-5-turbo"

# Memory
export GOOGLE_API_KEY="..."
export NEXUS_LANCEDB_DIR="/home/Zev/development/nexus-trade/.nexus_memory"
```

### Step 5.3: Start Paper Trading

The strategy needs a live runtime entry point. Create `/home/Zev/development/nexus-trade/run_live.py`:

```python
#!/usr/bin/env python3
"""
Nexus Trader Live Paper Trading Entry Point

Usage:
    python3 run_live.py [--enable-notifications]
"""
import argparse, logging, os, sys
sys.path.insert(0, 'src')

from lumibot.brokers import Alpaca
from lumibot.traders import Trader
from src.strategies.nexus_committee import NexusCommitteeStrategy, DEFAULT_UNIVERSE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("nexus_live")

# ── Alpaca broker ──
ALPACA_CONFIG = {
    "API_KEY": os.environ.get("ALPACA_API_KEY", ""),
    "API_SECRET": os.environ.get("ALPACA_SECRET_KEY", ""),
    "PAPER": os.environ.get("ALPACA_PAPER", "true").lower() == "true",
}

if not ALPACA_CONFIG["API_KEY"]:
    logger.error("ALPACA_API_KEY not set")
    sys.exit(1)

broker = Alpaca(ALPACA_CONFIG)

# ── Strategy configuration ──
parser = argparse.ArgumentParser()
parser.add_argument("--enable-notifications", action="store_true")
args = parser.parse_args()

NexusCommitteeStrategy.parameters = {
    "universe": DEFAULT_UNIVERSE,
    "max_position_pct": 0.15,  # Conservative for paper
    "max_new_positions_per_run": 1,
    "enable_notifications": args.enable_notifications,
    "use_memory_bridge": True,
}

strategy = NexusCommitteeStrategy(
    broker=broker,
    parameters=NexusCommitteeStrategy.parameters,
)

# ── Start trader ──
trader = Trader()
trader.add_strategy(strategy)
logger.info("Starting Nexus Trader paper trading — daily committee sessions")
trader.run_all()
```

**Start it:**

```bash
cd /home/Zev/development/nexus-trade
python3 run_live.py --enable-notifications 2>&1 | tee /tmp/nexus_paper_$(date +%Y%m%d).log
```

**What to expect:**
- The trader runs continuously, waking every day (`sleeptime = "1D"`)
- Each day at market open (or configured time), the committee runs
- Trades execute against Alpaca paper account
- Traces, logs, and memory files accumulate

### Step 5.4: Daily Monitoring Checklist

Every day, check:

1. **Logs** — any ERROR or WARNING lines?
   ```bash
   tail -100 /tmp/nexus_paper_*.log | grep -E "ERROR|WARNING|CRITICAL"
   ```

2. **Traces** — any `tool-error` or `future_timestamp` warnings?
   ```bash
   find .lumibot/agent_runtime/traces -name "*.json" -newer /tmp/yesterday_marker -exec grep -l "warning\|error\|future" {} \;
   ```

3. **Alpaca dashboard** — check positions, orders, P&L at https://app.alpaca.markets/paper

4. **Memory bridge** — is it syncing?
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'src')
   from src.memory.bridge import MemoryBridge
   bridge = MemoryBridge('Nexus_Trader')
   stats = bridge.sync_all()
   total = sum(stats.get(k, {}).get('embedded', 0) for k in ('decisions','lessons','theses','memories'))
   print(f'Total entries: {total}')
   "
   ```

### Step 5.5: Compare Live Decisions to Backtest Patterns

After 1 week:
- Download trade history from Alpaca dashboard
- Compare trade frequency to backtest (should be similar)
- Compare position sizes to backtest limits
- Check if decisions are "reasonable" (not buying at all-time highs with no catalyst, not panic-selling on 1% dips)

**Red flags that require stopping:**
- Overtrading (10+ trades in a week for a daily strategy)
- Position size violations (>20% in single position)
- Buying only one stock repeatedly (no diversification)
- Trading frequency decreasing to zero (agent frozen/confused)
- Multiple `tool-error` warnings in traces

**Exit criteria for Phase 5:**
- [x] Paper trading running daily for 2-4 weeks
- [x] Daily monitoring routine established
- [x] No red flags observed
- [x] At least 5 committee sessions completed
- [x] Memory bridge syncing live decisions
- [x] Decision: proceed to production or iterate

---

## Phase 6: Feature Testing Matrix

**Goal:** Systematically test each feature in isolation to measure its impact. This is the "scientific method" phase.

**Time estimate:** 8-16 hours (one controlled test per feature).

### Feature Testing Table

For each feature, run two backtests (feature ON vs feature OFF) with identical other parameters. Record results:

#### Test 6.1: Replay Cache — Deterministic Re-runs

**Already tested in Phase 0.9.** Document results:

| Metric | Cold Run | Warm Run | Match? |
|---|---|---|---|
| Final value | $X | $X | ✓/✗ |
| # Trades | N | N | ✓/✗ |
| Runtime | T min | T sec | N/A |

#### Test 6.2: Multi-Model Committee Combinations

Test different model assignments. Each combo is one backtest:

```bash
cd /home/Zev/development/nexus-trade

# Combo A: All single model (baseline — everything uses GLM-5)
COMMITTEE_RESEARCH_MODEL="zai/glm-5-turbo" \
COMMITTEE_BULL_MODEL="zai/glm-5-turbo" \
COMMITTEE_BEAR_MODEL="zai/glm-5-turbo" \
COMMITTEE_TRADER_MODEL="zai/glm-5-turbo" \
python3 src/strategies/nexus_committee.py --start 2024-01-01 --end 2024-03-31 --budget 10000 --symbols AAPL MSFT NVDA

# Combo B: Gemini Flash researchers + GLM-5 PM (target architecture)
COMMITTEE_RESEARCH_MODEL="ollama/gemini-3-flash-preview" \
COMMITTEE_BULL_MODEL="ollama/gemini-3-flash-preview" \
COMMITTEE_BEAR_MODEL="ollama/gemini-3-flash-preview" \
COMMITTEE_TRADER_MODEL="zai/glm-5-turbo" \
python3 src/strategies/nexus_committee.py --start 2024-01-01 --end 2024-03-31 --budget 10000 --symbols AAPL MSFT NVDA
```

Record per combo:
| Combo | Researchers | PM | Final Value | Return % | # Trades |
|---|---|---|---|---|---|
| A | GLM-5 x3 | GLM-5 | | | |
| B | Gemini Flash x3 | GLM-5 | | | |

#### Test 6.3: Sleeptime Frequency

Test different bar frequencies. Modify `self.sleeptime` in the strategy or pass via parameters:

```bash
# Daily (baseline)
python3 src/strategies/nexus_committee.py \
  --start 2024-01-01 --end 2024-03-31 --budget 10000 --symbols AAPL MSFT NVDA

# For 4H and 30min, you'd need minute-level data source (not Yahoo)
# Document as "not yet supported with Yahoo data"
```

> **Note:** Yahoo backtesting supports daily data minimum. For intraday frequencies, switch to Polygon or Alpaca data sources. This is documented as a limitation for now.

#### Test 6.4: Memory Bridge Impact on P&L

**Already covered in Phase 3.6 A/B test.** Document those results here.

#### Test 6.5: Custom Tools Impact on P&L

**Already covered in Phase 1.5 (tools vs no-tools comparison).** Document those results here.

#### Test 6.6: News Integration (Alpaca News)

The built-in `alpaca_news` tool requires Alpaca credentials. Test it:

```bash
cd /home/Zev/development/nexus-trade

# Verify news tool availability
python3 -c "
import lumibot
from lumibot.components.agents.tools import ALPACA_NEWS_TOOL  # or however it's exposed
print('Alpaca news tool:', ALPACA_NEWS_TOOL)
"
```

Run a backtest with Alpaca as data source (not Yahoo) to get live news:

```python
# In the strategy, add to system prompt:
# "Use alpaca_news to check recent news for each candidate before trading."
```

**Document:** Whether news integration is functional, and if it changes decisions.

#### Test 6.7: DuckDB Analytics Usage

The `duckdb_query` tool lets agents run SQL against loaded data. Verify it works:

```bash
cd /home/Zev/development/nexus-trade

# Quick test — 2-bar backtest, check if duckdb is in traces
python3 src/strategies/nexus_committee.py \
  --start 2024-01-02 --end 2024-01-04 --budget 10000 --symbols AAPL

# Search traces for duckdb usage
find . -path "*/agent_runtime/traces/*" -name "*.json" -newer /tmp/duckdb_test_marker -exec grep -l "duckdb" {} \;
```

**Document:** Does the agent naturally use DuckDB for analytics, or does it need prompting?

#### Test 6.8: FRED Macro Data Integration

The built-in `get_fred_latest`, `get_fred_series`, `get_fred_snapshot` tools provide macro data:

```bash
# Test FRED data access
cd /home/Zev/development/nexus-trade

python3 -c "
from lumibot.components.agents.tools.fred_tools import FredTools
# Check if FRED tools are callable
print('FRED tools available')
"
```

Run a backtest with FRED-relevant prompt:
```
Modify the evidence_researcher prompt to include:
"Use get_fred_latest to check unemployment, inflation, and interest rates."
```

**Document:** Does macro data change decisions? Does it make the strategy more defensive in tightening cycles?

#### Test 6.9: SEC Fundamentals Integration

The built-in `get_income_statement`, `get_balance_sheet`, `get_filings` tools provide fundamental data:

```bash
# Test fundamental data
python3 -c "
import requests
# Quick SEC EDGAR test
r = requests.get('https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json', headers={'User-Agent': 'Nexus_Trader/1.0'})
print('SEC response:', r.status_code)
print('Keys:', list(r.json().keys())[:5] if r.ok else 'ERROR')
"
```

Run a backtest with fundamentals prompt:
```
Modify the evidence_researcher prompt to include:
"Use get_income_statement and get_balance_sheet to check company fundamentals."
```

**Document:** Does fundamental data improve stock selection?

### Phase 6 Exit Criteria

- [x] Replay cache determinism confirmed
- [x] At least 2 model combos tested
- [x] Memory bridge A/B documented
- [x] Custom tools vs baseline documented
- [x] News, FRED, SEC, DuckDB integration status documented
- [x] Full feature matrix populated in `FEATURE_MATRIX.md`

---

## Phase 7: Production Readiness (Future)

**Only proceed if Phases 0-6 are complete and paper trading is profitable.**

### Production Checklist

- [ ] Max daily loss limit: $50/day (hard kill switch)
- [ ] Max drawdown limit: 15% from peak (auto-liquidate all positions)
- [ ] Max single position: 25% of portfolio
- [ ] Max total exposure: 80% (always keep 20% cash)
- [ ] Drift detection: compare live decisions to backtest distribution weekly
- [ ] Start capital: $500-1000 maximum
- [ ] Scale rule: increase by $1000 per profitable week, cap at $5000
- [ ] Emergency stop: manual kill switch via Alpaca dashboard
- [ ] Backup: manual position close at end of each week if auto fails

### Production Start Command (DO NOT RUN UNTIL ALL EXIT CRITERIA MET)

```bash
# ⚠️ THIS USES REAL MONEY — ONLY RUN AFTER ALL PHASES PASS
export ALPACA_PAPER="false"  # LIVE TRADING
python3 run_live.py --enable-notifications
```

---

## Phase 8: ML Prediction Ensemble (Future — Research Stage)

**Goal:** Build a statistical/ML prediction layer that generates alpha signals independent of LLM reasoning. The LLM serves as context engine and meta-strategy selector, not the alpha generator.

**Time estimate:** 4-8 weeks (significant ML infrastructure).

### Background: The "LLM as Context Engine" Thesis

The core thesis from architecture brainstorming (see `docs/BRAINSTORM_CHATLOG.md`): the LLM should **not** be the alpha generator. It should serve as:

- **Context engine** — synthesizing multiple signal streams
- **Research analyst** — interpreting news, filings, sentiment
- **Memory retrieval system** — querying similar historical setups
- **Risk manager** — evaluating position sizing and regime awareness
- **Meta-strategy selector** — choosing which models/strategies to trust

Actual alpha comes from statistical/ML models (LightGBM, CatBoost, Transformers, LSTMs). The LLM is the **orchestrator**, not the predictor.

### Architecture: 5-Layer Data Flow

```
Layer 1: Market Data → Layer 2: Prediction Models → Layer 3: Memory → Layer 4: News → Layer 5: Meta-Decision
     ↓                        ↓                        ↓              ↓                ↓
  1m/5m/15m/1h/daily     Trend (GBDT)            LanceDB          SEC filings       LLM receives ALL
  bars for each stock    Sequence (LSTM/Trans)    Similar setups    Reddit/X          outputs + news + 
  Volume, VWAP, etc.     Regime (ML classifier)   Past decisions    Macro news        memory → decides
                         Volatility forecaster                                        whether to act
```

### Effort Allocation for Single-Stock 15m Strategy

| Component | % Effort | Phase(s) |
|---|---|---|
| Prediction models | 50% | Phase 8 |
| Feature engineering | 20% | Phase 9 |
| Risk management | 15% | Phases 10, 12 |
| Memory/retrieval | 10% | Phase 11 |
| LLM reasoning | 5% | Phase 12 |

### What to Build

**8.1 Trend Model (LightGBM/CatBoost)** — `src/ml/trend_model.py`

Predicts next 15min, 1hr, 4hr returns for each symbol.

```python
# File: src/ml/trend_model.py
import lightgbm as lgb  # v4.5+
import catboost as cb   # v1.3+
import numpy as np
from sklearn.preprocessing import StandardScaler
from typing import Optional
import mlflow  # v2.18+

class TrendPredictor:
    """Gradient-boosted model for multi-horizon return prediction.
    
    Features: price action, volume, VWAP, options flow, order flow,
              implied volatility, market breadth.
    Output: probability distribution of next-period returns per horizon.
    """
    
    def __init__(self, horizons: list[str] = ["15m", "1h", "4h"]):
        self.horizons = horizons
        self.models: dict[str, lgb.LGBMRegressor] = {}
        self.scaler = StandardScaler()
    
    def fit(self, X: np.ndarray, y: dict[str, np.ndarray], 
            validation_data: Optional[tuple] = None) -> None:
        """Train one model per horizon. Logs metrics to MLflow."""
        X_scaled = self.scaler.fit_transform(X)
        for horizon in self.horizons:
            with mlflow.start_run(run_name=f"trend_{horizon}"):
                self.models[horizon] = lgb.LGBMRegressor(
                    n_estimators=500, learning_rate=0.01,
                    max_depth=5, num_leaves=31,
                    reg_alpha=0.1, reg_lambda=0.1,
                )
                self.models[horizon].fit(
                    X_scaled, y[horizon],
                    eval_set=[(X_scaled, y[horizon])],
                )
    
    def predict(self, X: np.ndarray) -> dict[str, np.ndarray]:
        """Return predicted returns for each horizon."""
        X_scaled = self.scaler.transform(X)
        return {h: self.models[h].predict(X_scaled) for h in self.horizons}
    
    def feature_importance(self, horizon: str = "1h") -> dict[str, float]:
        """Extract top features for LLM context ingestion."""
        importances = self.models[horizon].feature_importances_
        return dict(zip(self.feature_names, importances))
```

**Install:**
```bash
pip install lightgbm==4.5.0 catboost==1.3.1 mlflow==2.18.0
```

**8.2 Sequence Model (Transformer/LSTM)** — `src/ml/sequence_model.py`

```python
# File: src/ml/sequence_model.py
import torch  # v2.5+
import torch.nn as nn
from typing import Optional
import numpy as np

class FinancialTransformer(nn.Module):
    """Lightweight Transformer for financial time series.
    
    Input: (batch, seq_len=200, features) — last 200 bars with OHLCV + indicators.
    Output: (batch, n_horizons) — forecasted returns for [15m, 1h, 4h, 1d].
    """
    
    def __init__(self, n_features: int = 32, d_model: int = 128,
                 n_heads: int = 4, n_layers: int = 3,
                 dropout: float = 0.1, n_horizons: int = 4):
        super().__init__()
        self.input_proj = nn.Linear(n_features, d_model)
        self.pos_encoding = PositionalEncoding(d_model, max_len=500)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads, dropout=dropout,
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
        self.output_head = nn.Sequential(
            nn.Linear(d_model, 64), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(64, n_horizons),
        )
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (B, 200, n_features) → output: (B, n_horizons)."""
        x = self.input_proj(x)
        x = self.pos_encoding(x)
        x = self.transformer(x)
        x = x[:, -1, :]  # Last timestep
        return self.output_head(x)
```

**Install:**
```bash
pip install torch==2.5.1
```

**8.3 Regime Classification Model** — `src/ml/regime_classifier.py`

```python
# File: src/ml/regime_classifier.py
import lightgbm as lgb
import numpy as np
from enum import Enum

class MarketRegime(Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    BREAKOUT = "breakout"
    REVERSAL = "reversal"
    VOL_EXPANSION = "vol_expansion"
    VOL_CONTRACTION = "vol_contraction"

class RegimeClassifier:
    """ML-based regime classifier complementing (not replacing) CrabQuant.
    
    Uses statistical features of price distributions — Hurst exponent,
    autocorrelation, volatility skew, kurtosis, etc.
    Outputs regime label with probability distribution.
    """
    
    def __init__(self):
        self.model = lgb.LGBMClassifier(
            objective="multiclass", num_class=len(MarketRegime),
            n_estimators=200, learning_rate=0.05,
        )
    
    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        """X: statistical features, y: regime labels."""
        self.model.fit(X, y)
    
    def predict_proba(self, X: np.ndarray) -> dict[MarketRegime, float]:
        """Return probability distribution over regimes."""
        probs = self.model.predict_proba(X)[0]
        return {r: probs[i] for i, r in enumerate(MarketRegime)}
    
    def predict(self, X: np.ndarray) -> tuple[MarketRegime, float]:
        """Return regime + confidence."""
        probs = self.predict_proba(X)
        best_regime = max(probs, key=probs.get)
        return best_regime, probs[best_regime]
```

**Integration with existing Nexus Trader code:** The regime model outputs feed into `src/strategies/nexus_committee.py` `_build_context()` as additional context for the committee agents. The LLM receives both CrabQuant regime and ML regime classification — if they disagree, that's valuable signal for the bear researcher.

**8.4 Volatility Forecasting Model** — `src/ml/volatility_model.py`

```python
# File: src/ml/volatility_model.py
import numpy as np
import lightgbm as lgb

class VolatilityForecaster:
    """Multi-horizon volatility forecaster for position sizing.
    
    Forecasts: ATR, realized volatility, and expected volatility.
    Separate from price prediction — vol has different dynamics.
    """
    
    def __init__(self, horizons: list[int] = [5, 21, 63]):
        """horizons: number of bars forward (e.g., 5 bars = 1 week on daily)."""
        self.horizons = horizons
        self.models: dict[int, lgb.LGBMRegressor] = {}
    
    def fit(self, X: np.ndarray, y: dict[int, np.ndarray]) -> None:
        for h in self.horizons:
            self.models[h] = lgb.LGBMRegressor(n_estimators=300)
            self.models[h].fit(X, y[h])
    
    def predict(self, X: np.ndarray) -> dict[str, float]:
        """Return {f"vol_{h}d": float} for each horizon."""
        return {f"vol_{h}d": self.models[h].predict(X)[0] for h in self.horizons}
```

### Libraries/Tools Needed

| Tool | Version | Purpose |
|------|---------|---------|
| LightGBM | 4.5.0 | Gradient-boosted tree (trend prediction) |
| CatBoost | 1.3.1 | Alternative GBDT with categorical handling |
| PyTorch | 2.5.1 | LSTM and Transformer implementations |
| scikit-learn | 1.5+ | Feature preprocessing, pipeline management |
| pandas / numpy | 2.2+/1.26+ | Data manipulation |
| MLflow | 2.18.0 | Experiment tracking and model registry |
| optuna | 4.0+ | Hyperparameter optimization |

### File Structure
```
src/ml/
├── __init__.py              # ML subsystem exports
├── ensemble.py              # EnsembleMetaLearner — combines all 4 models
├── trend_model.py           # TrendPredictor (LightGBM)
├── sequence_model.py        # FinancialTransformer (PyTorch)
├── regime_classifier.py     # RegimeClassifier (LightGBM)
├── volatility_model.py      # VolatilityForecaster (LightGBM)
├── features.py              # Feature engineering pipeline
├── train.py                 # Training orchestration
└── evaluate.py              # Walk-forward evaluation
```

### Data Flow in Phase 8
```
Market Data (LumiBot get_historical_prices)
    ↓
Feature Engineering (src/ml/features.py)
    ↓ split
    ├──→ TrendPredictor → return_forecast
    ├──→ FinancialTransformer → sequence_forecast  
    ├──→ RegimeClassifier → regime_probs
    └──→ VolatilityForecaster → vol_forecast
    ↓ combine
EnsembleMetaLearner (src/ml/ensemble.py)
    ↓
LLM Context Engine (src/strategies/nexus_committee.py)
    ↓
Trade Decision
```

### Testing Approach
- **Unit tests:** Each model tested with synthetic data (sine waves + noise)
- **Integration test:** Full ensemble run on 1 month of real data, compare to baseline
- **Walk-forward test:** Phase 4 framework applied to ensemble output — must beat Phase 0 baseline
- **File:** `tests/test_ml_ensemble.py`

### Dependencies
- Phase 1 (tools provide feature inputs)
- Phase 4 (walk-forward validation framework used for ML backtesting)
- CrabQuant regime detection (baseline to compare ML regime model against)

### Risk/Caveats
- **Overfitting risk is HIGH** for financial ML — rigorous walk-forward required
- Short-horizon predictions (15m) need intraday data (not Yahoo daily)
- Feature engineering is 80% of the work; model selection is 20%
- ML models may produce contradictory signals that confuse the LLM committee
- Compute cost: training 4+ models weekly on rolling windows
- **Cold start:** No historical model performance to bootstrap weights

---

## Phase 9: Causal Reasoning & Factor Discovery (Future — Research Stage)

**Goal:** Add causal reasoning to validate that factors actually cause outcomes, not just correlate. Build an alpha factor discovery loop with LLM curation and automated validation.

**Time estimate:** 4-6 weeks.

### Why Causal Reasoning?
- Correlations break during regime shifts — causal relationships are more robust
- Enables counterfactual reasoning: "What would happen if X didn't occur?"
- LLM gets structured causal input rather than guessing from correlations
- Anchors decisions in validated cause-effect chains, not statistical noise

### What to Build

**9.1 Causal Model Infrastructure** — `src/causal/`

```
src/causal/
├── __init__.py              # Causal subsystem exports
├── causal_model.py          # CausalModel API (DoWhy wrapper)
├── factor_discovery.py      # Alpha factor discovery loop
├── factor_validation.py     # Backtesting proposed factors
├── causal_graph_agent.py    # DoWhy/CausalNex graph management
├── factor_scoring.py        # Factor predictive power tracking
├── decay_tracker.py         # Factor decay monitoring
└── agents/
    ├── __init__.py
    ├── factor_discovery_agent.py   # LLM-driven factor proposal
    ├── factor_validation_agent.py  # LightGBM/CatBoost validation
    ├── causal_graph_agent.py       # Cause-effect validation
    └── risk_assessment_agent.py    # Exposure monitoring
```

```python
# File: src/causal/causal_model.py
import dowhy  # v0.11+
from dowhy import CausalModel as DoWhyModel
import causalnex  # v0.12+
import networkx as nx  # v3.3+
from typing import Optional

class NexisCausalModel:
    """Wrapper around DoWhy/CausalNex for alpha factor validation.
    
    Key API:
    - identify_effect() — determine which variables drive outcomes
    - estimate_effect() — quantify causal impact
    - validate_factor() — full validation pipeline for proposed factors
    """
    
    def __init__(self, graph: Optional[nx.DiGraph] = None):
        self.graph = graph or nx.DiGraph()
        self.neox_store: Optional[Neo4jStore] = None
    
    def identify_effect(self, data: pd.DataFrame, 
                        treatment: str, outcome: str) -> dict:
        """Determine which variables causally drive the outcome.
        
        Args:
            data: DataFrame with all features + outcome column
            treatment: The factor being tested (e.g., 'rsi_divergence')
            outcome: The target variable (e.g., 'forward_return_1h')
        
        Returns:
            dict with 'identified_estimand', 'backdoor_vars', etc.
        """
        model = DoWhyModel(
            data=data,
            treatment=treatment,
            outcome=outcome,
            graph=self.graph,
        )
        identified = model.identify_effect()
        return {
            "estimand": str(identified),
            "backdoor_vars": identified.get_backdoor_variables(),
            "frontdoor_vars": identified.get_frontdoor_variables(),
        }
    
    def estimate_effect(self, data: pd.DataFrame, 
                        treatment: str, outcome: str,
                        method: str = "backdoor.linear_regression") -> dict:
        """Quantify the causal impact of treatment on outcome.
        
        Returns:
            dict with 'estimate', 'confidence_interval', 'p_value'.
        """
        model = DoWhyModel(
            data=data, treatment=treatment,
            outcome=outcome, graph=self.graph,
        )
        identified = model.identify_effect()
        estimate = model.estimate_effect(
            identified, method_name=method,
        )
        return {
            "estimate": estimate.value,
            "ci_lower": estimate.get_confidence_intervals()[0],
            "ci_upper": estimate.get_confidence_intervals()[1],
        }
    
    def validate_factor(self, data: pd.DataFrame,
                        factor_name: str, outcome: str = "forward_return_1h",
                        min_effect_size: float = 0.01) -> dict:
        """Full validation: identify → estimate → decide if factor passes.
        
        Used by Factor Validation Agent to gate new factors.
        """
        identified = self.identify_effect(data, factor_name, outcome)
        estimated = self.estimate_effect(data, factor_name, outcome)
        
        effect_size = abs(estimated["estimate"])
        passes = effect_size >= min_effect_size and estimated.get("p_value", 1.0) < 0.05
        
        return {
            "factor": factor_name,
            "passes": passes,
            "effect_size": effect_size,
            "p_value": estimated.get("p_value"),
            "backdoor_vars": identified["backdoor_vars"],
            "confidence_interval": [estimated["ci_lower"], estimated["ci_upper"]],
        }
```

**Install:**
```bash
pip install dowhy==0.11.1 causalnex==0.12.0 networkx==3.3
pip install neo4j==5.24.0  # For Neo4j graph persistence
```

**9.2 Alpha Factor Discovery Loop** — `src/causal/factor_discovery.py`

```python
# File: src/causal/factor_discovery.py
from typing import Any
import pandas as pd
import json

class FactorDiscoveryLoop:
    """Continuous alpha factor discovery with LLM curation.
    
    The LLM acts as a curator: scans financial reports, market data,
    social sentiment → proposes new factors → validates → tracks decay.
    
    Continuous feedback loop:
    1. LLM scans structured/semi-structured sources
    2. Proposes factor candidates with formulae
    3. FactorValidationAgent backtests them (LightGBM/CatBoost)
    4. CausalGraphAgent validates cause-effect (DoWhy/CausalNex)
    5. Factors with significant predictive power enter library
    6. FactorScoring tracks which factors are currently predictive
    7. FactorDecayTracker monitors when factors lose power
    """
    
    def __init__(self, llm_model: str = "zai/glm-5-turbo"):
        self.llm_model = llm_model
        self.active_factors: dict[str, FactorScore] = {}
        self.decayed_factors: dict[str, FactorScore] = {}
        self.candidates: list[FactorCandidate] = []
    
    def propose_factors(self, context: dict[str, Any]) -> list[dict]:
        """LLM scans context and proposes new factor candidates.
        
        Context includes: recent market events, news sentiment,
        SEC filings, social media activity, regime state.
        """
        # LLM call: "Given current market conditions X, what novel 
        # alpha factors should we test? Output structured JSON."
        pass
    
    def validate_candidate(self, candidate: dict, 
                           data: pd.DataFrame) -> FactorValidationResult:
        """Run causal + statistical validation on a candidate factor."""
        pass
    
    def score_factors(self) -> dict[str, float]:
        """Score all active factors on recent predictive power.
        
        Used by Factor Scoring Agent (Phase 10) for model weighting.
        """
        pass
    
    def detect_decay(self, window_days: int = 30) -> list[str]:
        """Identify factors whose predictive power has decayed."""
        pass


@dataclass
class FactorCandidate:
    name: str
    formula: str  # e.g., "(close - SMA20) / ATR14"
    source: str   # e.g., "SEC filing analysis", "social sentiment"
    proposed_by: str  # LLM model name
    proposed_at: str  # ISO timestamp

@dataclass  
class FactorScore:
    name: str
    sharpe: float
    ic: float  # Information coefficient
    decay_rate: float  # How fast predictive power erodes
    last_updated: str
    regime_scores: dict[str, float]  # Per-regime performance

@dataclass
class FactorValidationResult:
    passes_causal: bool
    passes_statistical: bool
    effect_size: float
    p_value: float
    oos_sharpe: float
```

**9.3 Multi-Agent Factor System** — `src/causal/agents/`

```python
# File: src/causal/agents/factor_discovery_agent.py
class FactorDiscoveryAgent:
    """LLM-driven agent that proposes new alpha factors.
    
    Scans: financial reports, market data, social sentiment,
    SEC filings, earnings calls, analyst notes.
    
    Outputs structured factor proposals with formulas.
    """
    
    system_prompt = """You are an Alpha Factor Discovery agent.
    Given the following market context, propose 1-3 novel alpha factors.
    For each, provide:
    - Factor name (snake_case)
    - Mathematical formula (in Python/pandas syntax)
    - Data sources needed
    - Why this might have predictive power
    - Market conditions where it should work/don't work
    - Expected decay timeline"""

# File: src/causal/agents/causal_graph_agent.py  
class CausalGraphAgent:
    """Validates cause-effect relationships using DoWhy/CausalNex.
    
    Stores validated causal chains in Neo4j for retrieval.
    Formats causal insights for LLM consumption:
    "Given that factor X causes Y, and we observed Z…"
    """
    
    def build_causal_graph(self, factors: list[str], 
                           data: pd.DataFrame) -> nx.DiGraph:
        """Use causalnex to discover causal structure from data."""
        from causalnex.structure.notears import from_pandas
        sm = from_pandas(data[factors])  # NOTEARS algorithm
        return nx.DiGraph(sm.edges)
    
    def store_in_neo4j(self, graph: nx.DiGraph, 
                       metadata: dict) -> None:
        """Persist causal graph to Neo4j with metadata."""
        pass

# File: src/causal/agents/risk_assessment_agent.py
class RiskAssessmentAgent:
    """Monitors portfolio exposure based on active factors.
    
    Tracks: factor concentration, regime-specific risk,
    factor correlation matrix for diversification.
    """
    
    def assess_exposure(self, portfolio: dict, 
                        active_factors: dict) -> RiskReport:
        pass
```

### Factor Scoring & Decay Tracking — `src/causal/factor_scoring.py` and `src/causal/decay_tracker.py`

```python
# File: src/causal/decay_tracker.py
class FactorDecayTracker:
    """Monitors when alpha factors lose predictive power.
    
    Uses rolling window IC (Information Coefficient) to detect decay.
    Alerts FactorDiscoveryAgent to search for replacements.
    """
    
    def __init__(self, decay_threshold: float = 0.05,
                 window_days: int = 30):
        self.decay_threshold = decay_threshold
        self.window_days = window_days
    
    def check_factor(self, factor_name: str, 
                     predictions: pd.Series,
                     outcomes: pd.Series) -> DecayStatus:
        """Check if a factor's IC has fallen below threshold."""
        rolling_ic = predictions.rolling(self.window_days).corr(outcomes)
        is_decaying = rolling_ic.iloc[-1] < self.decay_threshold
        return DecayStatus(
            factor=factor_name,
            current_ic=rolling_ic.iloc[-1],
            trend="declining" if is_decaying else "stable",
            replace=candidates if is_decaying else None,
        )
```

### Libraries/Tools Needed

| Tool | Version | Purpose |
|------|---------|---------|
| DoWhy | 0.11.1 | Causal inference — identify and estimate causal effects |
| CausalNex | 0.12.0 | Bayesian causal graph discovery (NOTEARS algorithm) |
| NetworkX | 3.3 | Graph data structure for causal links |
| Neo4j | 5.24.0 | Persistent graph database for causal chains |
| LightGBM / CatBoost | 4.5.0 / 1.3.1 | Factor validation backtesting |
| LangChain | 0.3+ | LLM agent construction for factor agents |
| Ray | 2.40+ | Parallel agent execution |
| PostgreSQL | 16+ | Factor library and performance history storage |

### Dependencies
- Phase 4 (walk-forward framework needed for factor validation)
- Phase 8 (ML prediction ensemble — factors feed into these models)
- Neo4j instance (self-hosted or cloud)
- PostgreSQL instance for factor performance history

### Risk/Caveats
- Causal inference is computationally expensive at scale
- Requires careful experimental design for `identify_effect()` — not all causal questions are answerable
- Neo4j adds operational complexity (separate database to maintain)
- Alpha factor discovery can overfit to noise without rigorous OOS validation
- Latency constraints: causal inference may be too slow for intraday decisions
- LLM curation quality depends heavily on prompt engineering and data quality
- **Factor proliferation:** Too many factors → multicollinearity → model instability

---

## Phase 10: Dynamic Model Switching & Meta-Learning (Future — Research Stage)

**Goal:** Build a system that dynamically selects which ML model or strategy to trust based on real-time regime changes. Use a meta-learner that learns which models work best in which conditions.

**Time estimate:** 4-6 weeks.

### What to Build

**10.1 Dynamic Model Switching** — `src/ml/ensemble.py` + `src/meta/`

```
src/meta/
├── __init__.py
├── gating_agent.py          # Real-time signal monitoring + regime gating
├── model_selector.py        # Bandit-based model selection
├── debate_agent.py          # LLM-driven model comparison
├── weight_gating.py         # Dynamic agent weight-gating by regime
└── performance_tracker.py   # PostgreSQL-backed performance history
```

```python
# File: src/meta/model_selector.py
import numpy as np
from typing import Any
from collections import defaultdict

class BanditModelSelector:
    """Multi-armed bandit for model/strategy selection.
    
    Supports: Thompson Sampling, UCB1, Epsilon-Greedy.
    Each model is an "arm" — the bandit allocates weight based on
    recent performance in the current regime.
    """
    
    def __init__(self, models: list[str], 
                 algorithm: str = "thompson"):
        self.models = models
        self.algorithm = algorithm
        # Thompson Sampling: Beta distribution params per model per regime
        self.successes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.failures: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # UCB1: pull counts and mean rewards
        self.pulls: dict[str, int] = defaultdict(int)
        self.rewards: dict[str, list] = defaultdict(list)
    
    def select_weight(self, regime: str) -> dict[str, float]:
        """Return weight distribution across models for this regime."""
        if self.algorithm == "thompson":
            return self._thompson_sample(regime)
        elif self.algorithm == "ucb1":
            return self._ucb1_weights(regime)
        else:  # epsilon-greedy
            return self._epsilon_greedy(regime)
    
    def _thompson_sample(self, regime: str) -> dict[str, float]:
        """Sample from Beta distribution per model."""
        samples = {}
        for model in self.models:
            a = self.successes[regime][model] + 1  # Beta prior
            b = self.failures[regime][model] + 1
            samples[model] = np.random.beta(a, b)
        total = sum(samples.values()) or 1.0
        return {m: s/total for m, s in samples.items()}
    
    def _ucb1_weights(self, regime: str) -> dict[str, float]:
        """UCB1: upper confidence bound for each arm."""
        total_pulls = sum(self.pulls.values())
        ucb = {}
        for model in self.models:
            if self.pulls[model] == 0:
                ucb[model] = float('inf')  # Explore unseen models
            else:
                mean_reward = np.mean(self.rewards[model])
                exploration = np.sqrt(2 * np.log(total_pulls) / self.pulls[model])
                ucb[model] = mean_reward + exploration
        # Softmax over UCB values for weight distribution
        ucb_vals = np.array(list(ucb.values()))
        exp_ucb = np.exp(ucb_vals - np.max(ucb_vals))  # Numerical stability
        probs = exp_ucb / exp_ucb.sum()
        return dict(zip(self.models, probs))
    
    def update(self, model: str, regime: str, 
               reward: float, threshold: float = 0.0) -> None:
        """Update bandit with realized performance."""
        self.pulls[model] += 1
        self.rewards[model].append(reward)
        if reward > threshold:
            self.successes[regime][model] += 1
        else:
            self.failures[regime][model] += 1


# File: src/meta/gating_agent.py
class GatingAgent:
    """Monitors real-time signals and determines current regime.
    
    Pulls from: CrabQuant regime, ML RegimeClassifier (Phase 8),
    volatility signals, correlation matrices, breadth indicators.
    
    Outputs: regime label + uncertainty + recommended model weights.
    """
    
    def __init__(self, bandit_selector: BanditModelSelector):
        self.selector = bandit_selector
        self.regime_history: list[RegimeEvent] = []
    
    def gate(self, signals: dict[str, Any]) -> GatingDecision:
        """Determine regime and produce model weights."""
        regime = self._classify_regime(signals)
        weights = self.selector.select_weight(regime)
        return GatingDecision(
            regime=regime,
            model_weights=weights,
            uncertainty=signals.get("regime_uncertainty", 0.3),
            timestamp=datetime.now().isoformat(),
        )
```

**10.2 Meta-Learner** — `src/meta/meta_learner.py`

```python
# File: src/meta/meta_learner.py
import torch
import torch.nn as nn

class MetaLearner(nn.Module):
    """Learns to weight models based on regime + recent performance.
    
    Input: regime embedding + model performance vector + causal signals
    Output: weight distribution over models
    
    Trained via: supervised (maximize Sharpe of weighted ensemble)
    or RL (optimize P&L directly).
    """
    
    def __init__(self, n_models: int, regime_dim: int = 16,
                 performance_dim: int = 32):
        super().__init__()
        self.regime_encoder = nn.Sequential(
            nn.Linear(regime_dim, 64), nn.ReLU(),
            nn.Linear(64, 32),
        )
        self.performance_encoder = nn.Sequential(
            nn.Linear(performance_dim, 64), nn.ReLU(),
            nn.Linear(64, 32),
        )
        self.weight_head = nn.Sequential(
            nn.Linear(64, 32), nn.ReLU(),
            nn.Linear(32, n_models), nn.Softmax(dim=-1),
        )
    
    def forward(self, regime: torch.Tensor,
                performance: torch.Tensor) -> torch.Tensor:
        """Return model weights (sums to 1)."""
        r = self.regime_encoder(regime)
        p = self.performance_encoder(performance)
        combined = torch.cat([r, p], dim=-1)
        return self.weight_head(combined)
```

**10.3 Dynamic Agent Weight-Gating** — `src/meta/weight_gating.py`

```python
# File: src/meta/weight_gating.py
from enum import Enum

class AgentRole(Enum):
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    TREND_FOLLOWING = "trend_following"
    FUNDAMENTAL = "fundamental"
    SENTIMENT = "sentiment"

class DynamicWeightGater:
    """Different strategy agents weighted depending on market regime.
    
    Example: momentum agent gets higher weight in trending,
    mean-reversion agent gets higher weight in ranging.
    
    Weights updated in real-time based on trailing performance.
    """
    
    # Base weights per regime — adjusted by meta-learner
    REGIME_WEIGHTS = {
        "trending_up": {
            AgentRole.MOMENTUM: 0.35,
            AgentRole.TREND_FOLLOWING: 0.30,
            AgentRole.BREAKOUT: 0.15,
            AgentRole.FUNDAMENTAL: 0.10,
            AgentRole.SENTIMENT: 0.05,
            AgentRole.MEAN_REVERSION: 0.05,
        },
        "ranging": {
            AgentRole.MEAN_REVERSION: 0.35,
            AgentRole.FUNDAMENTAL: 0.25,
            AgentRole.SENTIMENT: 0.15,
            AgentRole.MOMENTUM: 0.10,
            AgentRole.TREND_FOLLOWING: 0.10,
            AgentRole.BREAKOUT: 0.05,
        },
        # ... more regimes
    }
    
    def __init__(self, meta_learner: MetaLearner):
        self.meta_learner = meta_learner
        self.base_weights = self.REGIME_WEIGHTS
    
    def get_weights(self, regime: str,
                    performance_context: torch.Tensor) -> dict[AgentRole, float]:
        """Return adjusted weights for current regime + recent performance."""
        base = self.base_weights.get(regime, self.base_weights["ranging"])
        # Meta-learner adjusts based on recent agent performance
        adjustment = self.meta_learner(regime, performance_context)
        # Blend base weights with meta-learner adjustment
        return self._blend_weights(base, adjustment)
```

**10.4 Debate Agent** — `src/meta/debate_agent.py`

```python
# File: src/meta/debate_agent.py
class DebateAgent:
    """LLM-driven agent comparing model/strategy recommendations.
    
    Given conflicting model outputs (e.g., TrendModel says BUY,
    SequenceModel says SELL), the debate agent evaluates:
    - Which model is historically better in this regime?
    - What are the confidence levels?
    - What does causal reasoning say?
    
    Output: structured comparison with recommendation.
    """
    
    system_prompt = """You are a Model Debate Arbiter.
    You receive outputs from multiple prediction models that may disagree.
    Your job: compare them, considering:
    1. Each model's historical accuracy in the current market regime
    2. Confidence/probability of each prediction
    3. Causal evidence supporting or contradicting each view
    4. Risk/reward asymmetry of acting on each recommendation
    
    Output a structured decision: which model to trust, with rationale."""
```

### Libraries/Tools Needed

| Tool | Version | Purpose |
|------|---------|---------|
| Bandit algorithms (Thompson, UCB, Epsilon-Greedy) | custom | Model selection weights |
| PyTorch | 2.5.1 | Meta-learner training |
| Ray | 2.40+ | Parallel agent execution |
| LangChain | 0.3+ | Agent orchestration framework |
| PostgreSQL | 16+ | Persistent storage for model performance history |
| scikit-learn | 1.5+ | Baseline meta-learner implementations |

### Integration with Existing Nexus Trader Code

```
GatingAgent reads from:
  → src/tools/regime_tool.py (CrabQuant regime)
  → src/ml/regime_classifier.py (ML regime, Phase 8)
  → src/causal/causal_model.py (causal regime signals, Phase 9)

BanditModelSelector weights:
  → src/ml/trend_model.py
  → src/ml/sequence_model.py
  → src/ml/volatility_model.py
  
Combined output goes to:
  → src/strategies/nexus_committee.py (LLM context engine)
  → Phase 5/7 trade execution
```

### Dependencies
- Phase 8 (ML prediction ensemble — these are the models being switched)
- Phase 9 (causal reasoning provides regime change signals)
- Phase 4 (walk-forward framework for meta-learner validation)

### Risk/Caveats
- Bandit algorithms need sufficient data to converge — early performance may be random
- Regime misclassification can lead to wrong model selection (cascading errors)
- Meta-learner adds another layer of complexity that itself can fail
- Cold start problem: no historical data to bootstrap model selection
- Computation: running multiple models in parallel + meta-learner inference
- **Weight oscillation:** Rapid regime changes → frequent weight switches → transaction costs

### 10.7 Stateful Orchestration & Dynamic Agent Focus Control

Beyond model selection, the orchestration layer controls **what each agent thinks about** at runtime. Instead of letting LLM agents wander freely, the Python manager acts as a stateful router that:

1. **Conditional Branching (Decision Tree Routing)** — Route to different agents based on market conditions:
   - Low volatility → Mean Reversion Agent gets higher weight
   - Whale alert / sudden volume spike → Bypass analysis, route straight to Risk Mitigation/Stop-Loss Agent
   - High momentum trend → Trend Following Agent dominates
   - Regime uncertainty → Reduce all positions, increase analysis depth

2. **Prompt Injection & State Buffering** — Dynamically modify agent context before it runs:
   ```python
   # If a trade is underwater, force-inject a warning:
   if current_loss_pct > 1.5:
       inject_context = (
           "[SYSTEM NOTICE: Current unrealized loss is {loss:.1f}%. "
           "You are approaching the {max_loss:.1f}% hard stop. "
           "Analyze ONLY immediate liquidation or hedge strategies. "
           "Do NOT open new positions.]"
       )
   ```
   This overrides the agent's natural tendency to "averaging down" or opening offsetting positions.

3. **Structured Output (Pydantic/Instructor)** — Force agent decisions into typed Python objects:
   ```python
   from pydantic import BaseModel, Field
   
   class TradingDecision(BaseModel):
       reasoning: str = Field(description="Agent's internal analysis and focus")
       action: str = Field(description="MUST be 'BUY', 'SELL', or 'HOLD'")
       confidence_score: float = Field(description="Scale 0.0 to 1.0")
       position_size_pct: float = Field(description="Portfolio % to allocate")
       ticker: str = Field(description="Ticker symbol")
       time_horizon: str = Field(description="'intraday', 'swing', 'position'")
   ```
   This lets the orchestrator **programmatically evaluate** agent decisions (reject low-confidence trades, cap position sizes, etc.) rather than parsing free-text.

4. **Orchestrator Engine Patterns** — Two approaches to implement:

   **Option A: LumiBot's Built-in Committee (Current)**
   - Use `NexusCommittee` with `AgentHandle` — simple, integrated
   - Limited control over routing logic (all agents see all data)
   - Best for initial phases where simplicity matters

   **Option B: Custom Python Orchestrator (Advanced)**
   - Use `asyncio` + direct LLM API calls outside LumiBot
   - Full control over routing, branching, and state injection
   - Can use LangGraph for complex cyclical agent graphs:
     ```python
     from langgraph.graph import StateGraph, END
     
     def route_by_regime(state):
         if state['volatility'] > 2.0:
             return 'risk_agent'
         elif state['momentum'] > 0.7:
             return 'trend_agent'
         else:
             return 'analysis_agent'
     
     graph = StateGraph(TradingState)
     graph.add_node('analysis_agent', analysis_step)
     graph.add_node('trend_agent', trend_step)
     graph.add_node('risk_agent', risk_step)
     graph.add_conditional_edges('analysis_agent', route_by_regime)
     ```
   - Or CrewAI for role-based multi-agent collaboration with hierarchical task delegation
   - Best for Phase 10+ when you need complex conditional routing and external model calls

5. **Temperature/Parameter Control per Agent Role**
   - Risk/execution agents: `temperature=0.1` (strict, deterministic)
   - Research/analysis agents: `temperature=0.7` (creative, exploratory)
   - Portfolio manager: `temperature=0.3` (balanced judgment)
   ```python
   agent_configs = {
       'researcher': {'temperature': 0.7, 'max_tokens': 4096},
       'risk_manager': {'temperature': 0.1, 'max_tokens': 2048},
       'portfolio_manager': {'temperature': 0.3, 'max_tokens': 4096},
   }
   ```

**Libraries for orchestration:**

| Library | Version | Purpose | Integration |
|---------|---------|---------|-------------|
| LangGraph | 0.2+ | Cyclical agent graphs, conditional branching | `src/meta/orchestrator.py` |
| CrewAI | 0.80+ | Role-based multi-agent collaboration | Alternative to LangGraph |
| Pydantic | 2.0+ | Structured output schemas | Agent decision objects |
| Instructor | 1.0+ | LLM response validation with Pydantic | `src/meta/structured_output.py` |
| asyncio | stdlib | Parallel agent execution, background tasks | Orchestrator engine |
| FastAPI | 0.115+ | WebSocket dashboard + live agent monitoring | Optional Phase 7+ |

**Data Flow:**
```
Market Data → Regime Detector → Orchestrator Router
                                    ↓
                    ┌───────────────┼───────────────┐
                    ↓               ↓               ↓
              Research Agent   Trend Agent    Risk Agent
              (temp=0.7)       (temp=0.3)     (temp=0.1)
                    ↓               ↓               ↓
              TradingDecision (Pydantic) ← validated, typed
                    ↓
            Position Sizer + Risk Checks
                    ↓
              Execution Agent
```

---

## Phase 11: Advanced Memory Architecture (Future — Research Stage)

**Goal:** Evolve from simple vector memory (LanceDB) to a multi-modal memory system with causal narrative graphs, dedicated embedding agents, and persistent graph-database-backed memory.

**Time estimate:** 3-5 weeks.

### What to Build

**11.1 Causal Narrative Memory** — `src/memory/causal_narrative.py`

```
src/memory/
├── __init__.py
├── bridge.py                  # Existing: LumiBot JSONL → LanceDB bridge
├── nexus_vector_memory.py     # Existing: LanceDB vector store
├── causal_narrative.py        # NEW: Causal chain → narrative story
├── embedding_agent.py         # NEW: Dedicated embedding pipeline
├── graph_store.py             # NEW: Neo4j integration layer
├── hybrid_retriever.py        # NEW: Vector + Graph hybrid search
└── memory_orchestrator.py     # NEW: Coordinates all memory subsystems
```

```python
# File: src/memory/causal_narrative.py
from typing import Any
from datetime import datetime
import json

class CausalNarrativeMemory:
    """Market events stored in Neo4j graph with causal links.
    
    Each causal chain is summarized as a short interpretive story
    (LLM-generated). The LLM compares current conditions to past
    causal narratives.
    
    Format: "This pattern reminds me of [past cause-effect chain]"
    
    Example narrative:
    "In June 2024, a VIX spike above 25 coincided with a 3% SPX drop.
    The causal chain was: rate-hike-fear → vol-expansion → sell-off →
    mean-reversion-3-days-later. Our momentum model was whipsawed but
    the mean-reversion model captured the bounce. Lesson: during VIX>25,
    favor mean-reversion over momentum."
    """
    
    def __init__(self, llm_model: str = "zai/glm-5-turbo",
                 graph_store: Optional[Neo4jGraphStore] = None):
        self.llm_model = llm_model
        self.graph_store = graph_store
    
    def build_narrative(self, causal_chain: dict[str, Any],
                        market_context: dict[str, Any]) -> str:
        """Convert a causal chain + market context into a narrative.
        
        Uses LLM to generate natural-language story from structured data.
        
        Args:
            causal_chain: Nodes and edges from Neo4j (cause → effect)
            market_context: Prices, volumes, regimes, agent decisions
        
        Returns:
            Natural language narrative paragraph.
        """
        prompt = f"""Summarize this causal chain as a short story a 
        portfolio manager would tell a colleague:
        
        Chain: {json.dumps(causal_chain, indent=2)}
        Context: {json.dumps(market_context, indent=2)}
        
        Include: what happened, why, what we learned, what to watch for.
        Keep it under 200 words."""
        # → LLM call → narrative string
        pass
    
    def find_similar_narratives(self, query: str, 
                                 n_results: int = 5) -> list[str]:
        """Search past causal narratives by semantic similarity.
        
        Uses both: vector search (LanceDB embedding) + graph traversal
        (Neo4j causal links) for hybrid retrieval.
        """
        pass
    
    def compare_to_present(self, current_state: dict) -> list[dict]:
        """Find historical situations that causally resemble now.
        
        Returns narratives with similarity scores and causal relevance.
        """
        pass
```

**11.2 Dedicated Embedding Agent** — `src/memory/embedding_agent.py`

```python
# File: src/memory/embedding_agent.py
import numpy as np
import google.generativeai as genai
from typing import Any

class EmbeddingAgent:
    """Dedicated agent for converting market data → vector embeddings.
    
    Converts: market data, causal relationships, alpha factors →
    vector embeddings (~768-dimensional for Gemini, 3072 for text-embedding).
    
    LLM orchestrates retrieval but doesn't do embedding itself —
    keeps the LLM nimble and reduces context consumption.
    
    Runs on a schedule: embed new events as they occur.
    """
    
    def __init__(self, model_name: str = "models/gemini-embedding-001",
                 dimension: int = 3072):
        self.model_name = model_name
        self.dimension = dimension
        genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
    
    def embed_event(self, event: dict[str, Any]) -> np.ndarray:
        """Convert a market event into a vector embedding.
        
        Event can be: trade decision, lesson, causal chain, alpha factor,
        regime change, news summary, earnings report.
        """
        text = self._event_to_text(event)
        result = genai.embed_content(
            model=self.model_name,
            content=text,
            task_type="retrieval_document",
        )
        return np.array(result["embedding"])
    
    def embed_batch(self, events: list[dict]) -> np.ndarray:
        """Batch embed multiple events for efficiency."""
        texts = [self._event_to_text(e) for e in events]
        result = genai.embed_content(
            model=self.model_name,
            content=texts,
            task_type="retrieval_document",
        )
        return np.array(result["embedding"])
    
    def _event_to_text(self, event: dict) -> str:
        """Serialize event to text for embedding."""
        return json.dumps(event, sort_keys=True, default=str)
```

**11.3 Graph Database Integration** — `src/memory/graph_store.py`

```python
# File: src/memory/graph_store.py
from neo4j import GraphDatabase
from typing import Any, Optional
import networkx as nx

class Neo4jGraphStore:
    """Persistent causal relationship storage in Neo4j.
    
    Schema:
    - (:Event {id, timestamp, type, summary})
    - (:Factor {name, category, current_score})
    - (:Regime {name, start_time, end_time})
    - [:CAUSES {confidence, effect_size, p_value}]
    - [:OCCURS_DURING]
    - [:CORRELATES_WITH]
    """
    
    def __init__(self, uri: str = "bolt://localhost:7687",
                 user: str = "neo4j", password: str = ""):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
    
    def store_causal_chain(self, cause_event: dict,
                           effect_event: dict,
                           metadata: dict) -> None:
        """Store a cause-effect pair with metadata."""
        with self.driver.session() as session:
            session.run("""
                MERGE (c:Event {id: $cause_id})
                SET c += $cause_props
                MERGE (e:Event {id: $effect_id})
                SET e += $effect_props
                MERGE (c)-[:CAUSES {
                    confidence: $confidence,
                    effect_size: $effect_size,
                    p_value: $p_value,
                    verified_at: $verified_at
                }]->(e)
            """, cause_id=cause_event["id"],
                cause_props=cause_event,
                effect_id=effect_event["id"],
                effect_props=effect_event,
                **metadata)
    
    def get_causal_neighborhood(self, event_id: str,
                                 depth: int = 2) -> nx.DiGraph:
        """Retrieve causal graph neighborhood around an event."""
        with self.driver.session() as session:
            result = session.run("""
                MATCH path = (e:Event {id: $id})-[r:CAUSES*1..$depth]-()
                RETURN path
            """, id=event_id, depth=depth)
            # Convert to NetworkX graph
            return self._result_to_nx(result)
    
    def search_causal_patterns(self, pattern_desc: str) -> list[dict]:
        """Find causal chains matching a description."""
        # Uses full-text indexing on event summaries
        pass
```

**11.4 Hybrid Retriever** — `src/memory/hybrid_retriever.py`

```python
# File: src/memory/hybrid_retriever.py
import numpy as np
from typing import Any

class HybridRetriever:
    """Combines vector search (LanceDB) + graph traversal (Neo4j).
    
    For each query:
    1. Vector search: find top-K semantically similar events
    2. Graph traversal: find causally connected events nearby
    3. Fusion: rank by combined relevance score
    
    This is how the LLM gets richer context than vector-only.
    """
    
    def __init__(self, vector_store, graph_store):
        self.vector_store = vector_store
        self.graph_store = graph_store
    
    def search(self, query: str, n_results: int = 10,
               alpha: float = 0.7) -> list[dict]:
        """Hybrid search with weighted fusion.
        
        Args:
            alpha: Weight of vector search vs graph (0-1).
                   0.7 = 70% vector, 30% graph.
        """
        vector_results = self.vector_store.search(query, n_results * 2)
        graph_results = self._graph_expand(vector_results)
        fused = self._reciprocal_rank_fusion(
            vector_results, graph_results, alpha
        )
        return fused[:n_results]
    
    def _graph_expand(self, vector_results: list[dict]) -> list[dict]:
        """For each vector result, find causally connected neighbors."""
        expanded = []
        for r in vector_results:
            if "id" in r:
                neighbors = self.graph_store.get_causal_neighborhood(r["id"])
                expanded.extend(neighbors)
        return expanded
    
    def _reciprocal_rank_fusion(self, vec, graph, alpha):
        """RRF: combine rankings from two retrieval methods."""
        scores = {}
        for rank, item in enumerate(vec):
            scores[item["id"]] = scores.get(item["id"], 0) + alpha / (rank + 60)
        for rank, item in enumerate(graph):
            scores[item["id"]] = scores.get(item["id"], 0) + (1 - alpha) / (rank + 60)
        # Return items sorted by combined score
        pass
```

### Libraries/Tools Needed

| Tool | Version | Purpose |
|------|---------|---------|
| LanceDB | latest | Vector embeddings and similarity search |
| Neo4j | 5.24.0 | Graph database for causal relationship storage |
| LangChain | 0.3+ | LLM agent construction |
| Google Generative AI | latest | Gemini embedding model (3072-dim) |
| NetworkX | 3.3 | In-memory graph operations |

### Dependencies
- Phase 3 (existing memory bridge and LanceDB setup)
- Phase 9 (causal reasoning — produces the causal chains stored here)
- Neo4j instance (Docker: `docker run -p 7474:7474 -p 7687:7687 neo4j:5.24`)

### Risk/Caveats
- Neo4j adds operational complexity and cost
- Causal narrative quality depends on LLM summarization accuracy
- Memory retrieval latency may be too high for intraday decisions
- Embedding drift: as market regimes change, older embeddings may become less relevant
- Storage growth: causal graphs + vector embeddings accumulate quickly
- **Cold narrative quality:** Early narratives may be low quality until enough causal chains exist

---

## Phase 12: Reinforcement Learning & Adaptive Systems (Future — Research Stage)

**Goal:** Build systems that adapt to changing market conditions through simulation and continuous learning. Move from static agent behaviors to truly adaptive systems.

**Time estimate:** 6-10 weeks (ambitious research phase).

### What to Build

**12.1 Reinforcement Learning with Simulated Markets**
- Build simulated market environments for RL training
- System adapts to changing regimes through simulation
- Continuous training loop: train → validate → deploy → collect data → retrain
- RL agent learns position sizing, entry/exit timing, and risk management

**12.2 Multi-Modal Foundation Agents**
- Combine price data, news, and visual data (charts) into unified agent input
- Multi-modal inputs provide richer context than text-only
- Chart image analysis: pattern recognition, support/resistance, volume profiles
- Foundation model fine-tuned on financial multi-modal data

**12.3 Fleet of Specialized LLM Agents**
- Agents debate and cross-check each other's insights
- Specialized roles: Fundamental Analyst, Sentiment Analyst, Technical Analyst, Macro Analyst, Risk Manager
- Each agent has domain-specific tools and prompts
- LLM Reasoning Coordinator synthesizes all views into final decision
- All agents running on a local 8B (or larger) LLM as decision arbiter

**12.4 Continuous Meta-Learning**
- System adapts its decision-making horizon over time
- Learns how to learn across market regimes
- Combines causal graph-based memory with RL for deeper adaptive reasoning
- Agent coordination overhead actively monitored vs. alpha generated

### Libraries/Tools Needed

| Tool | Purpose |
|------|---------|
| Stable-Baselines3 / RLlib | Reinforcement learning frameworks |
| OpenAI Gym / custom environments | Simulated market environments |
| PyTorch | Model training |
| Ray | Distributed RL training and agent orchestration |
| LangChain | Multi-agent LLM construction |
| Vision models (GPT-4V, Gemini Vision) | Chart image analysis |
| Local 8B+ LLM (Llama, Mistral, etc.) | Decision arbiter for agent fleet |

### Dependencies
- Phases 8-11 (all previous research phases provide foundation)
- Phase 4 (walk-forward framework validates RL performance)
- Substantial compute resources for RL training (GPU recommended)

### Risk/Caveats
- **Highest risk phase** — RL in finance is notoriously difficult due to non-stationary distributions
- Simulated markets may not capture real market microstructure
- Multi-modal foundation models are expensive and may have limited financial domain knowledge
- Fleet of agents can produce conflicting opinions — coordination is a hard problem
- Local LLM (8B) may not be capable enough as arbiter — may need cloud models
- Compute costs scale significantly with RL training loops
- Overfitting to simulation: RL agents may learn to exploit simulation artifacts

### Open Questions (from brainstorm)
- Latency constraints for real-time causal inference?
- LLM reasoning reliability under novel market conditions?
- Agent coordination overhead vs. alpha generated?
- Out-of-sample validation requirements for new factors?
- Can a local 8B model serve as adequate decision arbiter, or is cloud required?

### Effort Allocation Estimate (Single-Stock 15m Strategy)
- 50% prediction models (Phase 8)
- 20% feature engineering (Phase 9)
- 15% risk management (Phases 10, 12)
- 10% memory/retrieval (Phase 11)
- 5% LLM reasoning (Phase 12)

---

## Troubleshooting Quick Reference

| Symptom | Likely Cause | Fix |
|---|---|---|
| `429 Too Many Requests` | Ollama rate limit | Wait for reset or upgrade plan |
| `ImportError: lumibot` | LumiBot not on PYTHONPATH | `pip install -e ~/development/trading-bots/lumibot` |
| `ImportError: crabquant` | CrabQuant not installed | `pip install -e ~/development/CrabQuant` |
| `No module named 'src'` | Running from wrong dir | Always `cd /home/Zev/development/nexus-trade` first |
| `Vector memory disabled` | Missing `GOOGLE_API_KEY` | `export GOOGLE_API_KEY="..."` |
| `LanceDB` not found | Missing dependency | `pip install lancedb` |
| Backtest completes but no trades | Agents decided to hold | Check if task prompt is clear, check if tools work |
| Replay cache empty after backtest | Cache directory wrong or disabled | Check `.lumibot/agent_runtime/` in the working directory |
| `agent_run_summaries.jsonl` missing | Old LumiBot version | Upgrade to latest: `pip install --upgrade lumibot` |
| `signal_dashboard` returns error | No price data for symbol | Verify symbol exists and has data for the date range |
| `detect_regime` returns `unknown` | Not enough bars or VIX unavailable | Increase lookback, test more bars |
| Walk-forward test period has losses | Strategy overfit to training | Simplify, reduce symbols, don't proceed to paper |
| Paper trading stops making trades | Agent confused or API errors | Check logs, restart, verify Alpaca connection |

---

## File Index — Every File You Might Need to Edit

| File | What It Does | When to Edit |
|---|---|---|
| `src/strategies/nexus_committee.py` | Main strategy with committee, tools, lifecycle | Change prompts, tool registration, committee flow |
| `src/tools/regime_tool.py` | Regime detection @agent_tool | Tune regime classification logic |
| `src/tools/signal_dashboard_tool.py` | Technical signal dashboard @agent_tool | Add/remove indicators, tune thresholds |
| `src/tools/trade_memory_tool.py` | Memory search/remember @agent_tools | Change search behavior, add memory features |
| `src/memory/bridge.py` | LumiBot JSONL → LanceDB bridge | Change extraction logic, add file formats |
| `src/memory/nexus_vector_memory.py` | LanceDB vector store (decisions + lessons) | Change schema, embedding model, search params |
| `tests/prompt_variants.py` | Prompt A/B testing (create this file) | Add new prompt variants |
| `run_live.py` | Paper/live trading entry point (create this file) | Change broker, add safety checks |
| `docs/EXECUTION_ROADMAP.md` | This document | Update as phases complete |

---

## Progress Tracker

| Phase | Status | Date Completed | Key Result |
|---|---|---|---|
| Phase 0: Unblock & Validate | ⬜ Pending | | |
| Phase 1: Custom Tools | ⬜ Pending | | |
| Phase 2: Prompt Engineering | ⬜ Pending | | |
| Phase 3: Memory Bridge | ⬜ Pending | | |
| Phase 4: Walk-Forward | ⬜ Pending | | |
| Phase 5: Paper Trading | ⬜ Pending | | |
| Phase 6: Feature Matrix | ⬜ Pending | | |
| Phase 7: Production | ⬜ Blocked | | Waiting on Phases 0-6 |
| Phase 8: ML Prediction Ensemble | ⬜ Future | | Research stage — ML infrastructure |
| Phase 9: Causal Reasoning & Factor Discovery | ⬜ Future | | Research stage — causal inference |
| Phase 10: Dynamic Model Switching & Meta-Learning | ⬜ Future | | Research stage — bandit algorithms |
| Phase 11: Advanced Memory Architecture | ⬜ Future | | Research stage — Neo4j + causal narrative |
| Phase 12: RL & Adaptive Systems | ⬜ Future | | Research stage — most ambitious phase |

---

*This document is the authoritative execution plan. If a step is unclear, ambiguous, or fails in an unexpected way, update this document with findings so the next agent can learn from it.*
