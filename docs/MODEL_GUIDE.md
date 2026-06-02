# Nexus Trader Model Selection Guide

> **Version:** 0.1 (Initial Research)  
> **Last Updated:** 2026-05-31  
> **Status:** Awaiting rapid-fire LumiBot benchmark results

---

## Executive Summary

This guide recommends model assignments for Nexus Trader's multi-agent trading system based on:
1. **Existing model knowledge** from strat-depot research and AI trading landscape analysis
2. **LumiBot tool-calling context** from Wave 0 audit
3. **Model characteristics** from Ollama Cloud availability

**Key Insight:** The Wave 0 audit revealed that LumiBot's tool surface is production-grade (all 21 tools passed), but **model-specific tool-calling performance is untested**. Rapid-fire benchmarks are needed to validate these preliminary recommendations.

---

## 1. Existing Model Knowledge

### 1.1 What We Know From Research

From `strat-depot/docs/ai-trading-landscape-research.md` and the broader AI trading ecosystem:

#### TradingAgents Architecture (80K+ stars)
The TradingAgents project (TauricResearch) defined the multi-agent debate paradigm:
- **Fundamental analysts** — process SEC filings, earnings, macro
- **Technical analysts** — chart patterns, indicators
- **Sentiment experts** — news, social signals
- **Bull/bear researchers** — adversarial perspectives
- **Risk control** — position sizing, limits
- **Portfolio manager** — final decision synthesis

Their work suggests **different models for different roles**:
- Reasoning-heavy roles (strategy, research) → larger context models
- Utility roles (indicators, data fetch) → fast, cheap models
- Decision roles (PM) → balanced models with good tool calling

#### OpenAlice / TraderAlice (4.5K+ stars)
"Your one-person Wall Street" — full-lifecycle AI trading agent:
- Research → position entry → ongoing management → exit
- Uses **Claude Agent SDK** (Claude models optimized for tool calling)
- Emphasizes **Trading-as-Git** versioning and approval gates
- Multi-broker unified trading account (UTA)

Key insight: **Claude models are production-proven for agentic trading**, but Nexus Trader uses Ollama Cloud (different provider set).

#### LumiBot Agent Architecture (from Wave 0 Audit)

LumiBot's runtime:
- **Google ADK** for Gemini models (native path)
- **LiteLLM** for all other providers (OpenAI, Anthropic, xAI, Ollama, DeepSeek, etc.)
- **Tool surface:** 30+ tools across account, market data, DuckDB, indicators, FRED, SEC, orders, memory, docs
- **Backtest safety:** All tools enforce point-in-time data, no look-ahead
- **Caching:** SHA-256 key based on (system_prompt, task, context, runtime_context, model, tool_surface, memory_notes)

**Tool call pattern observed:**
- Single `on_trading_iteration()` can call 5-15 tools per bar
- DuckDB queries require multi-step: `market_load_history_table` → `duckdb_query`
- Memory tools are called frequently (remember, search_memory)

### 1.2 Model Characteristics (From General Knowledge)

| Model | Context Window | Strengths | Weaknesses | Known For |
|-------|---------------|-----------|------------|-----------|
| **GLM-5** | ~128K | Deep reasoning, Chinese/English bilingual | Slower inference | Reasoning, analysis |
| **GLM-5.1** | ~128K | Improved reasoning, faster | Less tested | Strategy, synthesis |
| **DeepSeek-V4** | ~128K | Coding, structured output, tool calling | Conservative | Code generation, analysis |
| **DeepSeek-V4-Flash** | ~128K | Fast, efficient | Less reasoning depth | Utility, quick calls |
| **Gemini-3FP** | ~1M | Massive context, research-heavy | Higher cost, latency | Long documents, research |
| **Qwen3.5** | ~128K | Balanced, multilingual | Average at extremes | General purpose |
| **Kimi-K2.5** | ~128K | Strong reasoning | Less tool-calling data | Analysis |
| **MiniMax-M2.5** | ~1M | Long context | Less widely tested | Long documents |
| **Nemotron-3** | ~128K | Coding, structured output | Narrower focus | Tool development |

---

## 2. LumiBot Tool-Calling Context

### 2.1 What Makes Tool Calling Hard

From the Wave 0 audit, successful tool calling requires:

1. **Schema adherence** — Parameters must match the exact schema (type hints, required vs optional)
2. **Multi-step reasoning** — Some operations require 2-3 tool calls in sequence (load history → query → analyze)
3. **Memory tracking** — Thesis IDs are timestamp strings the model must remember across calls
4. **Decision quality** — The model must interpret tool outputs and make trading decisions

### 2.2 Tool Categories by Complexity

| Category | Tools | Complexity | Model Requirement |
|----------|-------|------------|-------------------|
| **Account** | `account_positions`, `account_portfolio` | Low (simple passthrough) | Any model |
| **Market Data** | `market_last_price`, `market_load_history_table` | Low-Medium | Any model |
| **DuckDB** | `duckdb_query` | Medium (SQL generation) | Model with structured output |
| **Indicators** | `get_indicator`, `get_indicators`, `list_indicators` | Low (current bar only) | Any model |
| **FRED** | `get_fred_latest`, `get_fred_series`, etc. | Low-Medium | Any model |
| **Memory** | `remember`, `search_memory`, thesis tools | Medium (ID tracking, synthesis) | Model with memory |
| **Orders** | `orders_submit_order`, etc. | Medium (multi-param) | Model with decision quality |
| **SEC Filings** | `get_income_statement`, etc. | Medium (large text parsing) | Model with context |
| **Docs** | `lumibot_docs_search` | Low | Any model |

### 2.3 Tool Call Frequency Analysis

Based on typical trading iteration:

| Agent Role | Expected Tool Calls/Bar | Tool Types | Latency Sensitivity |
|------------|------------------------|------------|---------------------|
| **Researcher** | 8-15 | Market data, FRED, SEC, DuckDB | Medium (can batch) |
| **Bull Agent** | 3-6 | Indicators, market data, memory | Low (focused) |
| **Bear Agent** | 3-6 | Indicators, market data, memory | Low (focused) |
| **PM Agent** | 2-4 | Orders, account, memory | High (execution) |
| **Utility** | 1-2 | Single tools | High (must be fast) |

---

## 3. Recommended Model Assignments

### 3.1 Multi-Agent Committee Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        NEXUSTRADE AGENT COMMITTEE                         │
│                                                                           │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │  RESEARCHER      │  │  BULL AGENT     │  │  BEAR AGENT     │           │
│  │  Gemini-3FP     │  │  GLM-5          │  │  DeepSeek-V4    │           │
│  │  (1M context)    │  │  (reasoning)    │  │  (contrarian)   │           │
│  │  allow_trade=F   │  │  allow_trade=F  │  │  allow_trade=F  │           │
│  │  Role: Deep      │  │  Role: Optimistic│  │  Role: Skeptical│           │
│  │  research, macro │  │  thesis building│  │  thesis critique│           │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘           │
│           │ reports            │ reports            │ reports             │
│           └────────────────────┼────────────────────┘                     │
│                                ▼                                          │
│                    ┌───────────────────────┐                              │
│                    │      PM AGENT          │                              │
│                    │      GLM-5.1           │                              │
│                    │      (balanced)        │                              │
│                    │      allow_trade=T     │                              │
│                    │      Role: Final       │                              │
│                    │      decision + orders │                              │
│                    └───────────────────────┘                              │
│                                │                                          │
│                    ┌───────────┴───────────┐                              │
│                    │    UTILITY AGENTS     │                              │
│                    │    DeepSeek-V4-Flash  │                              │
│                    │    (fast, cheap)      │                              │
│                    │    Role: Single-tool  │                              │
│                    │    calls, routing     │                              │
│                    └───────────────────────┘                              │
│                                                                           │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Role Assignments with Rationale

#### **Bull Agent: GLM-5**

| Attribute | Value |
|-----------|-------|
| **Model** | `ollama-cloud/glm-5` |
| **Why** | Deep reasoning for building strong bullish thesis. Good at structured argumentation. |
| **Context Need** | Medium — needs to maintain thesis state across bars |
| **Tool Calls** | 3-6 per bar (indicators, market data, memory) |
| **allow_trade** | `False` (only produces reports) |
| **System Prompt Focus** | "Build the strongest possible bullish case for this asset. Consider momentum, fundamentals, sentiment. Challenge yourself to find evidence." |

#### **Bear Agent: DeepSeek-V4**

| Attribute | Value |
|-----------|-------|
| **Model** | `ollama-cloud/deepseek-v4` |
| **Why** | Strong at structured output and contrarian analysis. Good at finding weaknesses in bullish thesis. |
| **Context Need** | Medium — needs to track bull thesis and provide counter-arguments |
| **Tool Calls** | 3-6 per bar (same tools as bull) |
| **allow_trade** | `False` |
| **System Prompt Focus** | "Challenge every bullish thesis. Find risks, weak signals, and bearish evidence. Be the devil's advocate." |

#### **Researcher Agent: Gemini-3FP**

| Attribute | Value |
|-----------|-------|
| **Model** | `ollama-cloud/gemini-3fp` |
| **Why** | Massive 1M+ context for deep research. Can ingest full SEC filings, macro reports, historical data. |
| **Context Need** | High — loads entire earnings reports, FRED series, news |
| **Tool Calls** | 8-15 per bar (SEC filings, FRED macro, DuckDB analytics) |
| **allow_trade** | `False` |
| **System Prompt Focus** | "Provide comprehensive research. Load SEC filings, analyze macro trends via FRED, compute correlation matrices. Produce detailed research notes." |

#### **PM Agent (Executor): GLM-5.1**

| Attribute | Value |
|-----------|-------|
| **Model** | `ollama-cloud/glm-5-turbo` or `glm-5.1` |
| **Why** | Balanced reasoning + fast execution. Final decision synthesis. Good tool calling for orders. |
| **Context Need** | Medium — aggregates reports from Bull/Bear/Researcher |
| **Tool Calls** | 2-4 per bar (account, orders, memory, thesis lifecycle) |
| **allow_trade** | `True` (only agent that can trade) |
| **System Prompt Focus** | "Synthesize bull, bear, and research reports. Make final trading decisions. Execute orders only when conviction is high. Remember all decisions in memory." |

#### **Utility/Router Agents: DeepSeek-V4-Flash**

| Attribute | Value |
|-----------|-------|
| **Model** | `ollama-cloud/deepseek-v4-flash` |
| **Why** | Fast, cheap, good enough for single-tool calls. Minimizes latency and cost. |
| **Context Need** | Low — single purpose, no memory |
| **Tool Calls** | 1-2 per call (single tool) |
| **allow_trade** | `False` |
| **Roles** | - Get current price (`market_last_price`) - Get single indicator (`get_indicator`) - Quick DuckDB query (`duckdb_query`) - Route to appropriate agent |

---

## 4. Model Cost/Speed Tradeoffs

### 4.1 Estimated Token Costs (Per Trading Day)

Assumptions:
- 1 trading iteration per bar
- Daily bar frequency (1 iteration/day)
- Bull + Bear + Researcher + PM agents per iteration
- Average tool calls: 25 total per iteration

| Model | Est. Input Tokens/Call | Est. Output Tokens/Call | Cost per 1K Input | Cost per 1K Output | Daily Cost (25 calls) |
|-------|----------------------|------------------------|-------------------|--------------------|-----------------------|
| GLM-5 | 8,000 | 1,500 | ~$0.01 | ~$0.02 | ~$2.75 |
| GLM-5.1 | 8,000 | 1,500 | ~$0.01 | ~$0.02 | ~$2.75 |
| DeepSeek-V4 | 6,000 | 1,200 | ~$0.005 | ~$0.01 | ~$1.05 |
| DeepSeek-V4-Flash | 2,000 | 500 | ~$0.001 | ~$0.002 | ~$0.15 |
| Gemini-3FP | 15,000 | 2,000 | ~$0.02 | ~$0.04 | ~$11.50 |
| Qwen3.5 | 5,000 | 1,000 | ~$0.008 | ~$0.015 | ~$1.38 |

**Notes:**
- Costs are estimates; actual Ollama Cloud pricing varies
- Gemini-3FP is most expensive due to larger context usage
- DeepSeek-V4-Flash is ~10-20x cheaper than full models

### 4.2 Latency Estimates (Per Agent Call)

| Model | Avg Response Time | P95 Response Time | Suitable For |
|-------|-------------------|-------------------|--------------|
| GLM-5 | 2-4 seconds | 6-8 seconds | Bull/Bear reasoning |
| GLM-5.1 | 1.5-3 seconds | 5-7 seconds | PM decisions |
| DeepSeek-V4 | 2-3 seconds | 5-6 seconds | Bear analysis |
| DeepSeek-V4-Flash | 0.5-1 second | 2-3 seconds | Utility, routing |
| Gemini-3FP | 4-8 seconds | 12-15 seconds | Deep research |
| Qwen3.5 | 2-3 seconds | 5-6 seconds | General |

**Critical for trading:** PM Agent latency matters most. Research Agent latency can be hidden by running in parallel.

### 4.3 When to Use Expensive vs Cheap Models

| Use Case | Model | Why |
|----------|-------|-----|
| **Hourly/daily decision loop** | GLM-5.1 (PM) + DeepSeek-V4-Flash (utility) | Minimize latency, balanced reasoning |
| **Weekly strategic review** | Gemini-3FP (research) + GLM-5 (bull/bear) | Deep research worth the cost |
| **High-frequency signal check** | DeepSeek-V4-Flash only | Single indicator call, must be fast |
| **Thesis lifecycle** | GLM-5 or GLM-5.1 | Reasoning about thesis validity |
| **Position sizing calculation** | DeepSeek-V4 or utility | Structured output, low creativity |
| **Macro regime analysis** | Gemini-3FP | Large context for multiple FRED series |
| **Post-market reflection** | GLM-5 + DeepSeek-V4 | Both perspectives for lessons learned |

---

## 5. Awaiting Benchmark Results

### 5.1 What We Don't Know Yet

The rapid-fire LumiBot benchmarks (being run by another agent) will provide:

1. **Tool-calling success rate** per model — Do all tools work? Any schema issues?
2. **Latency distribution** — Real response times under load
3. **Error patterns** — Which models fail on which tools?
4. **Memory handling** — Can models track thesis IDs across calls?
5. **Decision quality** — Do trading decisions make sense?

### 5.2 Test Matrix (Expected)

| Test | Models | Bars | Tool Surface | Metrics |
|------|--------|------|--------------|---------|
| Tool Audit (done) | StubAgent (no LLM) | 5 | All 21 tools | Tool functionality ✅ |
| Multi-Call Memory | GLM-5 | 60 | All tools | Memory persistence, thesis tracking |
| Rapid-Fire Single | GLM-5, DS-V4, DS-V4-Flash, Gemini, Qwen | 5-10 | Core tools | Tool success rate, latency |
| Committee Sim | Multi-agent | 10 | All tools | Agent coordination |

### 5.3 How Results Will Update This Guide

When benchmark results arrive:

1. **Update Section 3** — Change model assignments if any model fails tool calling
2. **Update Section 4** — Replace latency/cost estimates with real measurements
3. **Add Section 6** — "Benchmark Results" with per-model pass rates
4. **Add Section 7** — "Model-Specific Gotchas" with error patterns
5. **Update Section 5.2** — Mark tests as completed with results

---

## 6. Integration with quant-loop-testnet

### 6.1 Strategy Genome DB

The Genome DB (in `quant-loop-testnet/conveyor/genome_db.py`) stores:
- Every strategy test result
- Performance by regime (bull/bear/sideways/high-vol/low-vol)
- Parameter success patterns

**For Nexus Trader:** Use Genome DB to:
1. **Inform Researcher Agent** — "In similar regimes, these strategies worked..."
2. **Track AI strategy performance** — Store AI-generated theses + outcomes
3. **Cross-validate** — Compare AI decisions to historical winners

### 6.2 Regime Detector Integration

CrabQuant's regime detector classifies market state:
- Bull trend, bear trend, sideways
- High vol, low vol
- Regime transitions

**Custom tool to build:**
```python
@agent_tool(name="regime_detect")
def regime_detect(self, symbol: str = "SPY", lookback: int = 20) -> dict:
    """
    Classify current market regime.
    
    Returns:
        regime: str (bull_trend, bear_trend, sideways_high_vol, sideways_low_vol)
        confidence: float
        signals: dict (momentum, volatility, trend_strength)
    """
```

This tool should feed into the **Researcher Agent's system prompt** so the AI knows what regime it's operating in.

---

## 7. Memory Bridge Architecture

### 7.1 Current LumiBot Memory

From Wave 0 audit:
- **Storage:** JSONL files per strategy
- **Search:** Keyword matching (NO embeddings)
- **Thesis lifecycle:** open → update → close (fragile ID tracking)

### 7.2 Proposed Genome DB Bridge

```
LumiBot Memory (JSONL)
    ↓ post-backtest extraction
Extracted Lessons
    ↓ structured parsing
Strategy Genome DB (SQLite)
    ↓ regime-tagged lessons
System Prompt Injection
    ↓ on next run
AI receives relevant lessons
```

**Key insight:** The AI should start each run with lessons from similar regimes:
- "In the last bull market (VIX < 15), momentum strategies worked best"
- "In high-vol regimes, reduce position size by 50%"

This requires:
1. Parser for JSONL → structured lessons
2. Regime tagger for each lesson
3. Query interface for "lessons from regime X"
4. System prompt templating to inject top N lessons

---

## 8. Custom Tools Roadmap

### Phase 1: Critical (Required for MVP)

| Tool | Priority | Complexity | Why Critical |
|------|----------|------------|--------------|
| `regime_detect()` | P0 | Medium | AI needs to know market context |
| `signal_dashboard()` | P0 | Medium | Pre-computed signals reduce tool calls |
| `portfolio_risk()` | P1 | Medium | Risk management without manual calculation |

### Phase 2: Important (Enhance AI Capabilities)

| Tool | Priority | Complexity | Why Important |
|------|----------|------------|---------------|
| `universe_scan()` | P2 | High | Discovery of new opportunities |
| `correlation_matrix()` | P2 | Medium | Portfolio-level decisions |
| `strategy_history()` | P2 | Medium | Cross-run learning |

### Phase 3: Nice-to-Have

| Tool | Priority | Complexity | Why Nice |
|------|----------|------------|----------|
| `greeks()` | P3 | Medium | Options strategies |
| `sentiment_score()` | P3 | Medium | News aggregation |
| `position_sizing()` | P3 | Low | Kelly/ATR sizing |

---

## 9. Open Questions

1. **Will all Ollama Cloud models pass LumiBot tool calling?** (Awaiting benchmarks)
2. **What's the real latency distribution under load?** (Awaiting benchmarks)
3. **Does Gemini-3FP's 1M context help or hurt?** (Large context = more tokens = more cost/latency)
4. **Is DeepSeek-V4-Flash reliable enough for utility calls?** (Cheap but less capable)
5. **How many bars should the Researcher Agent look back?** (Context window tradeoff)
6. **Should Bull/Bear agents run in parallel?** (Reduces latency but increases concurrent cost)

---

## 10. Next Steps

1. **Wait for rapid-fire benchmarks** — Validate model assignments
2. **Build Phase 1 custom tools** — `regime_detect`, `signal_dashboard`, `portfolio_risk`
3. **Implement memory bridge** — JSONL → Genome DB → System prompt
4. **Run multi-agent committee test** — Validate full architecture
5. **Paper trade for 30 days** — Validate decisions in live market
6. **Iterate on model assignments** — Swap models based on performance

---

## Changelog

| Date | Change |
|------|--------|
| 2026-05-31 | Initial version — awaiting rapid-fire benchmarks |

---

*This guide will be updated as benchmark results arrive and production testing continues.*