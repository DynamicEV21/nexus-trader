# Nexus Trader — Product Requirements Document

> **Version:** 0.1 (Research Phase)  
> **Last Updated:** 2026-05-31  
> **Status:** Active research — tests running, findings being incorporated

---

## 1. Vision Statement

**Nexus Trader turns LumiBot into the universal AI trading harness.**

Plug any LLM in, give it market data tools, custom quant tools, persistent memory, and it becomes an autonomous trader. The AI's reasoning IS the strategy — no hardcoded rules, no fixed indicators. The system learns from its own decisions and compounds knowledge over time.

### What This Is NOT
- Not a prediction model ("ChatGPT predicts candles")
- Not a single fixed strategy
- Not a manual trading dashboard
- Not a black-box HFT system

### What This IS
- An AI-native research and execution platform
- A multi-model investment committee
- A continuously learning trading agent
- Institutional-grade backtest safety with live deployment path

---

## 2. Core Capabilities (What LumiBot Already Provides)

Based on Wave 0 audit (all 21 tools tested, all passed):

### 2.1 Market Data Tools
| Tool | Status | Data Source | Backtest-Safe |
|------|--------|-------------|---------------|
| `account_positions` | ✅ | Strategy state | ✅ |
| `account_portfolio` | ✅ | Strategy state | ✅ |
| `market_last_price` | ✅ | Yahoo/Polygon | ✅ (no lookahead) |
| `market_load_history_table` | ✅ | Yahoo/Polygon | ✅ (datetime view) |
| `duckdb_query` | ✅ | In-memory DuckDB | ✅ (read-only) |
| `get_indicator` | ✅ | pandas-ta | ✅ (current bar only) |
| `get_indicators` | ✅ | pandas-ta | ✅ (current bar only) |
| `list_indicators` | ✅ | pandas-ta | ✅ |
| `get_fred_latest` | ✅ | FRED/ALFRED | ✅ (vintage data) |
| `get_fred_series` | ✅ | FRED/ALFRED | ✅ (vintage data) |
| `get_fred_snapshot` | ✅ | FRED/ALFRED | ✅ (vintage data) |

### 2.2 Memory & Thesis System
| Tool | Status | Storage | Notes |
|------|--------|---------|-------|
| `remember` | ✅ | JSONL | General memory notes |
| `remember_decision` | ✅ | JSONL | Trading decisions |
| `remember_lesson` | ✅ | JSONL | Lessons (written to both memories + lessons) |
| `search_memory` | ✅ | JSONL | Keyword search (NO embeddings) |
| `open_thesis` | ✅ | JSONL | Investment thesis |
| `update_thesis` | ✅ | JSONL | Thesis updates |
| `close_thesis` | ✅ | JSONL | Thesis close + reflection |

### 2.3 Order Execution
| Tool | Status | Notes |
|------|--------|-------|
| `orders_submit_order` | ✅ | Market, limit, stop, trailing stop, smart_limit |
| `orders_cancel_order` | ✅ | By identifier |
| `orders_open_orders` | ✅ | List tracked orders |
| `orders_modify_order` | ✅ | Modify limit/stop prices |

### 2.4 Other
| Tool | Status | Notes |
|------|--------|-------|
| `lumibot_docs_search` | ✅ | Search LumiBot docs |
| `alpaca_news` | ⚠️ | Needs Alpaca credentials |
| `get_income_statement` | ✅ | SEC EDGAR (free) |
| `get_balance_sheet` | ✅ | SEC EDGAR (free) |
| `get_filings` | ✅ | SEC EDGAR (free) |
| `notify_user` | ✅ | User notifications |

### 2.5 Critical Architecture Features
- **Point-in-time safety**: All data tools automatically truncate to current simulated bar
- **FRED vintage data**: Uses ALFRED endpoints for backtest-safe macro data
- **DuckDB analytics**: AI can write arbitrary SQL against loaded market data
- **Replay caching**: Tool call results are cached for deterministic re-runs
- **Multi-agent support**: Multiple agents can share a strategy

---

## 3. Gaps Identified (What We Need to Build)

### 3.1 Critical Gaps
| Gap | Impact | Solution |
|-----|--------|----------|
| **No cross-run learning** | AI starts fresh every backtest | Memory bridge → Strategy Genome DB |
| **No embeddings in search** | Keyword-only memory search | Add embedding layer to search_memory |
| **Single model perspective** | One model's biases | Multi-model investment committee |
| **No pre-computed signals** | AI reasons from raw data | Custom regime/signal tools |
| **No portfolio risk tools** | No beta, correlation, concentration | Custom portfolio analytics tools |
| **No universe scanner** | AI can't discover new opportunities | Custom screener tool |

### 3.2 Custom Tools to Build

**Implementation method:** Use LumiBot's `@agent_tool` decorator (preferred over MCP for backtesting reliability). Auto-includes source code in tool description so AI can see parameters and implementation.

1. **`regime_detect()`** — Classify current market regime using CrabQuant's regime detector
2. **`signal_dashboard()`** — Pre-computed dashboard: RSI, momentum, VIX regime, correlation in one call
3. **`portfolio_risk()`** — Portfolio beta, concentration risk, max loss, VaR
4. **`universe_scan()`** — Find top momentum/volume movers from a watchlist
5. **`strategy_history()`** — Query Genome DB for what worked in similar regimes
6. **`correlation_matrix()`** — Holdings correlation from DuckDB
7. **`greeks()`** — Options Greeks calculator (for options trading)

```python
from lumibot.components.agents import agent_tool

@agent_tool(
    name="regime_detect",
    description="Classify current market regime: BULL_TRENDING, BULL_VOLATILE, SIDEWAYS, BEAR, CRASH"
)
def regime_detect(self, symbol: str = "SPY", lookback: int = 50) -> dict:
    bars = self.get_historical_prices(symbol, lookback, "day")
 # ... CrabQuant regime logic ...
    return {"regime": "BULL_TRENDING", "confidence": 0.82, "volatility_state": "low"}
```

### 3.3 Memory Bridge Architecture
```
LumiBot Memory (JSONL per backtest)
    ↓ after each run
Extraction Layer (parses decisions, lessons, theses)
    ↓
Strategy Genome DB (SQLite in quant-loop-testnet)
    ↓ on next run
System Prompt Injection (top lessons + regime-strategy map)
    ↓
AI starts with accumulated knowledge
```

---

## 4. Integration Map (Existing Projects)

### 4.1 quant-loop-testnet → Nexus Trader
- **Strategy Genome DB**: Store all AI trading decisions, theses, and outcomes
- **Regime detector**: Feed regime classification into AI's context
- **Strategy factory**: AI-generated strategies get validated in arena harness
- **Daily brief**: Feed AI with genome DB insights before trading

### 4.2 CrabQuant → Nexus Trader
- **Arena harness**: Backtest validation for AI-created strategies
- **Guardrails system**: Overfitting detection, risk checks
- **Vectorized backtest**: Fast parameter sweeps for signal optimization
- **Walk-forward validation**: Before any live deployment

### 4.3 strat-depot → Nexus Trader
- **7,000+ strategies**: Source material for AI to study and adapt
- **RSI-Centered-Pivots**: The one verified strategy — baseline comparison
- **Strategy classifier**: Auto-score strategies for the AI

### 4.4 agentic-quant-os → Nexus Trader
- **Master vision**: Architecture reference and phased roadmap alignment
- **Agent topology**: Research → Validation → Portfolio → Risk → Execution
- **Memory stack**: Qdrant for vector memory integration

---

## 5. Model Strategy

### 5.1 Models Available on Ollama Cloud
(NEED: Update with actual tool-calling benchmarks from tests)

| Model | Context | Best For | Tool Calling | Status |
|-------|---------|----------|-------------|--------|
| GLM-5 | ~128K | Deep reasoning, strategy | TBD | Primary candidate |
| GLM-5.1 | ~128K | Deep reasoning (newer) | TBD | Primary candidate |
| DeepSeek-V4 | ~128K | Coding, analysis | TBD | Analyst role |
| DeepSeek-V4-Flash | ~128K | Fast decisions | TBD | Utility role |
| Gemini-3FP | ~1M | Long context, research | TBD | Research role |
| Qwen3.5 | ~128K | Balanced | TBD | General |
| Kimi-K2.5 | ~128K | Reasoning | TBD | General |
| MiniMax-M2.5 | ~1M | Long context | TBD | Research role |
| Nemotron-3 | ~128K | Coding | TBD | Tool dev |

### 5.2 Multi-Model Committee Architecture
```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  BULL AGENT   │   │  BEAR AGENT  │   │  RESEARCHER   │
│  GLM-5        │   │  DeepSeek-V4  │   │  Gemini-3FP   │
│  (optimistic) │   │  (contrarian) │   │  (data-heavy) │
│  allow_trade=F │   │  allow_trade=F │   │  allow_trade=F │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │                  │                   │
       └──────────────────┼───────────────────┘
                          ▼
                ┌──────────────────┐
                │   PM AGENT       │
                │   GLM-5.1        │
                │   allow_trade=T  │
                │   (final decision)│
                └──────────────────┘
```

---

## 6. Test Results

### Wave 0: Tool Audit ✅
- All 21 built-in tools tested
- All passed in backtesting mode
- FRED vintage data confirmed point-in-time safe
- DuckDB analytics confirmed working
- Memory/thesis system confirmed working
- Full report: `AUDIT_REPORT.md`

### Wave 1: Multi-Call Memory Backtest ⏸ BLOCKED
- GLM-5 ran ~25 bars (40% progress) before being killed for debugging
- Portfolio actively trading ($100K → fluctuating $91K-$96K)
- Hit rate limit before completing all 60 bars
- Model name fixed: `openai/glm-5` (not `glm-5-turbo`)
- Results format fixed: `results` is a dict (not list)
- [NEEDS: Re-run after Ollama Cloud weekly limit resets]

### Wave 2: Thesis Multi-Asset ⏳
- Not yet run

### Wave 3-N: Rapid-Fire Tests ⏸ BLOCKED
- GLM-5 tested successfully: 6 tool calls in 19.7s, correct TQQQ buy order
- All other models blocked by Ollama Cloud weekly rate limit (429)
- Full details in `TOOL_CALLING_BENCHMARKS.md`

---

## 7. Key Research Findings (from GitHub/Docs Research)

### 7.1 @agent_tool Pattern (Preferred for Custom Tools)
LumiBot's recommended way to add custom tools. Wraps any Python function, auto-includes source code in the AI's tool description. Works reliably in both backtesting and live. Full control over HTTP calls, error handling, and data formatting.

### 7.2 Multi-Agent Team Patterns (Canonical Examples)
LumiBot ships with 6 proven multi-agent patterns:
- **Bull/Bear Leveraged ETF** — Aggressive rotation team
- **Bull/Bear Large-Cap Stocks** — Structured debate with evidence
- **Ray Dalio Idea-Meritocracy** — Macro specialists argue growth, inflation, debt, liquidity
- **Warren Buffett Value** — Fundamental analysis focus
- **Bill Ackman Concentrated** — High-conviction + short-seller stress test
- **Citadel Sector-Pods** — Sector rotation through multiple lenses

**Key insight:** Research agents use `role="research"` (no trading permission). Trading agents use `role="trading"`. This separation is critical for safety.

### 7.3 Replay Cache (Cost Control)
SHA-256 hash of prompt+context+model+tools+timestamp → cached result. First cold run takes 20-40 min for a 6-year daily backtest. Warm reruns: seconds, zero API calls. This solves the cost problem for iterative development.

### 7.4 Observability System
Every agent run produces: compact log line, JSON trace file, `agent_run_summaries.jsonl` for programmatic analysis. Built-in warnings for: no-tools-called, tool-error, future-dated data (critical), unsupported-order.

### 7.5 System Prompt Best Practice
Keep system prompts to **2-3 sentences**. LumiBot's base prompt already handles position sizing, order execution, look-ahead bias prevention, and tool usage instructions. Focus on: what data to use, what assets to trade, allocation logic.

### 7.6 BotSpot Cloud (Managed Infrastructure)
MCP server at `mcp.botspot.trade/mcp` — can generate/deploy strategies from Claude Code or Cursor. Pre-wired data feeds, 10+ broker connections, scheduling, monitoring, kill switches. Not required for local development but available for production.

---

## 8. Ollama Cloud Rate Limit (2026-05-31)

**All Ollama Cloud API calls returning 429:** "you (tristanmarshall8821) have reached your weekly usage limit." This blocks ALL model testing until the weekly reset.

**Resolution options:**
1. Wait for weekly reset (automatic)
2. Upgrade at https://ollama.com/upgrade
3. Add extra usage at https://ollama.com/settings
4. Use a different LLM provider (e.g., direct Gemini API, Anthropic, OpenAI)

**Impact:** Wave 1 re-runs, all rapid-fire benchmarks, and multi-model comparisons are blocked until resolved.

---

## 9. Open Questions

1. **Which models actually excel at tool calling in LumiBot?** Need benchmarks.
2. **How many tool calls per bar is optimal?** More data = better decisions, but more cost + latency.
3. **Does cross-run memory actually improve performance?** Need A/B test.
4. **What's the right bar frequency?** Daily is proven, but can we do weekly with better reasoning?
5. **Multi-agent committee vs single agent?** Need comparison.
6. **Custom tools impact?** Does a regime signal tool improve AI decisions measurably?
7. **Memory bridge implementation?** How to extract and inject lessons from Genome DB.

---

## 8. Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| AI makes irrational trades | High | Medium | Risk limits, kill switch |
| Model API costs at scale | Medium | Medium | Caching, batch decisions |
| Overfitting to backtest period | High | High | Walk-forward, out-of-sample |
| Data leakage via custom tools | Low | Critical | All tools go through LumiBot safety |
| Regulatory/compliance | Low | High | Paper trading first, small capital |

---

*This PRD is a living document. Updated as test results come in.*
