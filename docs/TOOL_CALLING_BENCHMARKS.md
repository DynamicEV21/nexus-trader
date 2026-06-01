# LumiBot Agent Tool-Calling Benchmarks

**Date:** 2026-05-31  
**Backtest Period:** Jan 6-10, 2025 (5 trading days)  
**LLM Provider:** Ollama Cloud API (`https://ollama.com/v1`)  
**Framework:** LumiBot v4.5.25 with full built-in tools (30+ tools)

---

## ⚠️ CRITICAL: Weekly Rate Limit Hit

**All benchmarks were blocked by Ollama Cloud weekly usage limit.** Error 429: "you (tristanmarshall8821) have reached your weekly usage limit." This affects ALL models on the Ollama Cloud platform.

**Direct API test confirms:** Even a minimal `openai.chat.completions.create(model='glm-5', max_tokens=5)` returns 429. Not a per-model limit — an account-level weekly cap.

**Resolution:** Wait for weekly reset OR upgrade at https://ollama.com/upgrade OR add extra usage at https://ollama.com/settings.

---

## 1. Available Results

### From Rapid-Fire Agent (before session lock crash)

| Model | Status | Time (s) | Tool Calls | Order | Remember | Notes |
|-------|--------|----------|------------|-------|----------|-------|
| glm-5 | ✅ SUCCESS | 19.7 | 6 (account_positions, get_indicator, get_fred_latest, market_last_price, orders_submit_order, remember_decision) | ✅ Yes (TQQQ) | ✅ Yes | RSI(14)=47.27, VIX=16.04. Decided risk-on. |

### From Direct Benchmark Script (after rate limit hit)

| Model | Status | Time (s) | Tool Calls | Order | Notes |
|-------|--------|----------|------------|-------|-------|
| glm-5 | ❌ 429 | 16.5 | 0 | No | Weekly limit hit |
| deepseek-v4-pro | ❌ 429 | 16.2 | 0 | No | Weekly limit hit |
| deepseek-v4-flash | ❌ 429 | 18.0 | 0 | No | Weekly limit hit |
| gemini-3-flash-preview | ❌ 429 | 17.0 | 0 | No | Weekly limit hit |
| kimi-k2.5 | ❌ 429 | 17.9 | 0 | No | Weekly limit hit |

---

## 2. Key Insight from GLM-5 Test

**GLM-5 successfully completed the full tool-calling pipeline in 19.7 seconds:**

1. `account_positions` — checked current holdings
2. `get_indicator` — pulled SPY RSI(14) = 47.27
3. `get_fred_latest(VIXCLS)` — pulled VIX = 16.04
4. `market_last_price` — checked TQQQ price
5. `orders_submit_order` — bought TQQQ (risk-on decision)
6. `remember_decision` — logged the reasoning

**Decision quality:** RSI < 50 and VIX < 25 → risk-on → buy TQQQ. This is reasonable. Not brilliant, but not wrong.

**6 tool calls in 19.7 seconds** = ~3.3 seconds per tool call (including LLM reasoning between each).

---

## 3. What We Can Infer

Based on the one successful test + Wave 0 audit + Wave 1 progress:

| Capability | GLM-5 | Others | Notes |
|------------|-------|--------|-------|
| Tool calling (schema) | ✅ | ⏳ | GLM-5 follows schemas correctly |
| Multi-step reasoning | ✅ | ⏳ | Chained 6 tools in logical order |
| Order execution | ✅ | ⏳ | Correct order format, market order |
| Memory (remember) | ✅ | ⏳ | Used remember_decision() |
| FRED data access | ✅ | ⏳ | Correctly called get_fred_latest |
| Thesis lifecycle | ⏳ | ⏳ | Wave 1 showed thesis_id tracking issues |
| DuckDB queries | ⏳ | ⏳ | Not tested directly |
| Cross-run memory | ❌ | ❌ | Confirmed: memory doesn't persist between runs |

---

## 4. Tests Remaining (After Rate Limit Resets)

1. **DuckDB Analysis** — Can the AI write valid SQL and interpret results?
2. **Memory Recall** — Can search_memory find decisions from earlier bars?
3. **Thesis Lifecycle** — Can it open/update/close theses across bars?
4. **Model Comparison** — Which models have best tool-calling success rates?
5. **Multi-Agent** — Can bull/bear agents coordinate through a PM agent?

---

## 5. Estimated Cost Per Trading Day

Based on GLM-5 benchmark (6 tools, ~20s, estimated ~4K tokens total per call):

| Model Mix | Calls/Day | Est Tokens | Est Cost/Day |
|-----------|-----------|-------------|--------------|
| Single model | 1 | ~4K | ~$0.01 |
| Committee (4 agents) | 4 | ~16K | ~$0.04 |
| Full (4 agents × 2 calls) | 8 | ~32K | ~$0.08 |

With replay caching, subsequent backtest runs are FREE (zero API calls).

---

*Updated: 2026-05-31 — rate limit prevents further testing this week*
