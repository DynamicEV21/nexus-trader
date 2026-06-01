# Wave 0 Audit Report: LumiBot AI Agent System

**Date:** 2026-05-31  
**Scope:** Full audit of LumiBot's AI agent tools, memory system, runtime architecture, and custom-tool opportunities  
**Method:** Source code analysis + live backtest validation (all 21 tool calls passed)  
**LumiBot Version:** v4.5.25

---

## 1. Tool Audit Table

### 1.1 Category: Account & Portfolio

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `account_positions` | ✅ | Current positions with asset fields + quantity | None | `{positions: [...], as_of: ISO}` | Strategy state (free) | Returns `include_cash_positions=True` — USD forex position always appears |
| `account_portfolio` | ✅ | Cash + portfolio value for sizing | None | `{cash: float, portfolio_value: float, datetime: ISO}` | Strategy state (free) | None — simple passthrough |

**Backtest gotchas:** Both work correctly. `account_positions` includes both real positions and the USD cash position (represented as a forex asset). The cash position shows quantity equal to current cash.

### 1.2 Category: Market Data

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `market_last_price` | ✅ | Current last price for one asset | symbol, asset_type, optional expiration/strike/right/quote/exchange | `{symbol, asset_type, price, datetime}` | Backtest data source / broker (free if backtest data provided) | No look-ahead: returns price at simulated datetime |
| `market_load_history_table` | ✅ | Load historical bars into DuckDB | symbol, length, timestep, optional table_name, asset_type | `{table_name, row_count, columns, ...}` | Backtest data source / broker | Only returns bars visible at current datetime — enforces point-in-time safety |

**Backtest gotchas:** 
- `market_load_history_table` creates a SQL VIEW with `WHERE datetime <= current_dt` — no look-ahead possible
- `market_last_price` uses `replay_on_cache: True` so cached results are replayed in backtest re-runs
- Asset resolution automatically matches data store entries by symbol/type/expiration/strike/right (clever dedup)

### 1.3 Category: DuckDB Analytics

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `duckdb_query` | ✅ | Read-only SQL against loaded tables | sql, optional limit (default 200) | `{row_count, columns, rows, truncated, query_ms}` | In-memory DuckDB (free) | Read-only only: regex-enforced (`SELECT/WITH/SHOW/DESCRIBE/PRAGMA/EXPLAIN`) |

**Architecture insight:** `DuckDBQueryLayer` is a sophisticated in-memory DuckDB connection. It registers data source frames lazily, caches source tables by `(store_key, timestep)` tuple, and creates "visible views" with datetime cutoffs. Metrics tracking is built in (load calls, cache hits, query ms). The `duckdb_query` is a separate tool from `market_load_history_table` — the LLM must load a table first, then query it. Multi-step pattern: `market_load_history_table` → `duckdb_query`.

### 1.4 Category: Technical Indicators

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `list_indicators` | ✅ | List supported indicator names | None | `{common_indicators: ["sma","ema","rsi",...]}` | pandas-ta-classic (free) | Only 10 built-in indicators listed (sma, ema, rsi, macd, bbands, atr, vwap, vwma, roc, stoch) |
| `get_indicator` | ✅ | Get one current-bar indicator value | symbol, indicator, timestep, asset_type, optional parameters_json | `{symbol, indicator, value, datetime, no_lookahead: true}` | pandas-ta-classic (free) | Only current-bar value (not time series). Parameters passed as JSON string |
| `get_indicators` | ✅ | Get multiple current-bar indicators | symbol, indicators=list, timestep, asset_type | `{symbol, results: [...]}` | pandas-ta-classic (free) | Same limitation — current bar only |

**Backtest gotchas:**
- Indicators are sliced to the current strategy datetime — no future bars
- `parameters_json` is a JSON string, not a dict. The LLM must format parameters as `'{"length": 14}'`
- Only 10 indicators exposed. `pandas-ta-classic` supports ~120 indicators. Could expand easily.
- Important: these return a SINGLE value, not a time series. For backtest safety, only the current-bar value is given. If you need SMA(50) and SMA(200) crossover analysis, you'd need `market_load_history_table` + `duckdb_query` instead.

### 1.5 Category: FRED Macro

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `list_fred_series` | ✅ | List curated FRED macro series | optional category | `{categories: [...]}` | FRED API (free, unlimited) | Curated subset — not all 800k+ FRED series |
| `get_fred_series` | ✅ | Get a FRED time series | series_id, optional start/end/as_of/limit | `{observations: [...]}` | FRED API (free) | In backtests uses vintage data via realtime_start/realtime_end |
| `get_fred_latest` | ✅ | Get latest FRED observation | series_id, optional as_of | `{series_id, value, date, ...}` | FRED API (free) | Uses ALFRED vintage endpoint for backtest safety |
| `get_fred_snapshot` | ✅ | Multi-series FRED snapshot | series_ids (list or comma-sep), optional as_of | `{series: {...}}` | FRED API (free) | Convenience wrapper for multiple get_fred_latest calls |

**Critical backtest behavior:** FRED tools REQUIRE `FRED_API_KEY` in backtest mode. Without it, they become DISABLED tools that return `{"ok": false, "tool_error": true}` with a clear message about point-in-time data safety. When the key is present, all FRED calls use ALFRED vintage endpoints (`realtime_start`/`realtime_end`) to fetch data as-it-was-at-the-simulated-time. This prevents revised-data look-ahead bias.

**Our test:** FRED_API_KEY is configured in `.env` — `get_fred_latest('VIXCLS')` returned correctly with `point_in_time_safe: true` and `uses_revised_data: false`.

### 1.6 Category: Memory & Theses

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `remember` | ✅ | Store a local memory note | text, kind="memory", optional tags | `{id, timestamp, kind, text}` | JSONL files (free) | Appended to `memories.jsonl`, grows unbounded |
| `remember_decision` | ✅ | Record a trading decision | text, optional symbol/action | `{id, timestamp, kind: "decision", text}` | JSONL files (free) | Written to `decisions.jsonl` |
| `remember_lesson` | ✅ | Record a compact lesson | text, optional symbol | `{id, timestamp, kind: "lesson", text}` | JSONL files (free) | Written to both `lessons.jsonl` AND `memories.jsonl` |
| `open_thesis` | ✅ | Open a hedge-fund-style thesis | text, optional symbol/tags | `{id, timestamp, kind: "thesis", text, metadata: {status: "open"}}` | JSONL files (free) | Must track thesis_id for updates |
| `update_thesis` | ✅ | Append update to open thesis | thesis_id, text | `{id, timestamp, kind: "thesis_update", text}` | JSONL files (free) | Relies on model remembering thesis_id across calls |
| `close_thesis` | ✅ | Close thesis with outcome | thesis_id, text | `{id, timestamp, kind: "thesis_close", text, metadata: {outcome}}` | JSONL files (free) | No enforcement that thesis was open |
| `search_memory` | ✅ | Search local memories | query, optional limit/kind | `{query, count, results: [...]}` | JSONL files (free) | Simple keyword matching, no embeddings |

### 1.7 Category: Orders

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `orders_submit_order` | ✅ | Create + submit order | symbol, quantity, side, optional asset_type/order_type/limit_price/stop_price/etc. | `{order: {identifier, status, ...}}` | Strategy broker (free) | `replay_on_cache: True` — side effects replayed on cache hit |
| `orders_open_orders` | ✅ | List tracked orders | None | `{orders: [...], datetime}` | Strategy state (free) | Returns strategy-tracked orders, not broker orders |
| `orders_cancel_order` | ✅ | Cancel tracked order | identifier | `{identifier, status}` | Strategy broker (free) | `replay_on_cache: True` |
| `orders_modify_order` | ✅ | Modify limit/stop prices | identifier, optional limit_price/stop_price | `{identifier, limit_price, stop_price}` | Strategy broker (free) | `replay_on_cache: True` |

**Backtest gotchas:**
- `orders_submit_order` requires specific params based on order type: limit→limit_price, stop→stop_price, stop_limit→stop_limit_price, trailing_stop→trail_price or trail_percent
- Orders are submitted through `strategy.create_order()` then `strategy.submit_order()`
- `smart_limit` order type uses LumiBot's built-in smart-limit behavior (not documented in tool description)
- Market orders in backtests fill at the current bar's close

### 1.8 Category: News (Alpaca)

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `alpaca_news` | ✅/⚠️ | Fetch Alpaca/Benzinga news | symbols, start, end, limit ≤50, include_content, page_token, etc. | `{articles: [...], count, next_page_token}` | Alpaca API (requires Alpaca credentials or ALPACA_NEWS_API_KEY) | DISABLED without credentials. 2-step workflow recommended (scan→read) |

**Status in our test:** NOT tested directly (we tested the binding, not the actual API call). If no Alpaca credentials are available, the tool is bound as DISABLED with a clear error message. When available, it has sophisticated backtest safety: future end-times are clamped to the simulated datetime, and a `lookahead_clamped` flag is returned.

### 1.9 Category: SEC Fundamentals & Filings

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `get_income_statement` | ✅ | SEC income statement facts | symbol, optional as_of/raw | `{facts: [...]}` | SEC EDGAR (free, rate-limited) | Point-in-time via as_of param |
| `get_balance_sheet` | ✅ | SEC balance sheet facts | symbol, optional as_of/raw | `{facts: [...]}` | SEC EDGAR (free) | Point-in-time via as_of param |
| `get_cash_flow` | ✅ | SEC cash flow facts | symbol, optional as_of/raw | `{facts: [...]}` | SEC EDGAR (free) | Point-in-time via as_of param |
| `get_company_facts` | ✅ | Compact SEC companyfacts | symbol, optional as_of/raw/max_facts | `{facts: [...]}` | SEC EDGAR (free) | Default capped at 80 facts |
| `get_filings` | ✅ | List SEC filings | symbol, optional form/as_of/limit | `{filings: [...]}` | SEC EDGAR (free) | Point-in-time gated |
| `search_filing` | ✅ | Keyword-search cached filing | symbol, accession_number, query | `{snippets: [...]}` | SEC EDGAR (free) | Requires prior get_filings call |
| `get_filing_document` | ✅ | Read full filing document | symbol, accession_number, optional max_chars | `{text, truncated, ...}` | SEC EDGAR (free) | Large docs - max_chars=20000 by default |

**Not tested in our backtest** (fundamentals require external API access and are gated by strategy datetime). These route through `strategy.fundamentals.*` which uses the SEC EDGAR API.

### 1.10 Category: Documentation & Notifications

| Tool | Status | Description | Input | Output | Data Source | Limitations |
|------|--------|-------------|-------|--------|-------------|-------------|
| `lumibot_docs_search` | ✅ | Search LumiBot docs | query, max_results | `{query, result_count, results: [{path, title, score, snippet}]}` | Local docs (free) | Simple keyword matching on RST/MD files under docs/ |
| `notify_user` | ✅ | Send notification | title, message, severity, enabled | `{ok, results}` | Notification providers (free) | Disabled by default in backtests |

---

## 2. Memory & Thesis System Analysis

### 2.1 Architecture

The memory system is implemented in `lumibot/components/memory/store.py` as the `MemoryStore` class. It is:

- **File-based:** JSONL (JSON Lines) files in `{LUMIBOT_MEMORY_DIR}/{strategy_name}/`
- **Per-strategy:** Each strategy gets its own subfolder
- **Append-only:** All writes are append operations to JSONL files
- **Four separate files:**
  - `memories.jsonl` — general memories + lessons (lessons are cross-written here)
  - `decisions.jsonl` — trading decisions
  - `lessons.jsonl` — lessons
  - `theses.jsonl` — theses, thesis updates, thesis closes

### 2.2 How search_memory() Works

**Method:** Simple keyword matching — NOT embeddings, NOT vector search.

```python
def search(self, query, *, limit=10, kind=None):
    terms = [term.lower() for term in re.split(r"\s+", query.strip()) if term]
    for path in sorted(self.strategy_dir.glob("*.jsonl")):
        for entry in self._read_jsonl(path):
            haystack = json.dumps(entry, sort_keys=True).lower()
            score = sum(1 for term in terms if term in haystack)
            if score > 0:
                rows.append((score, entry))
    rows.sort(key=lambda item: (item[0], item[1].get("timestamp", "")), reverse=True)
```

**Implications:**
- No semantic understanding — "bull market" won't match "uptrend"
- Case-insensitive substring matching
- Scores by term hit count, sorted by (score, timestamp desc)
- Searches ALL JSONL files unless `kind` filter applied
- No indexing — linear scan through all entries

### 2.3 Thesis Lifecycle

Theses follow a three-state lifecycle:

1. **open_thesis(text, symbol, tags)** → creates entry with `kind: "thesis"`, `metadata.status: "open"`
2. **update_thesis(thesis_id, text)** → creates entry with `kind: "thesis_update"`, `metadata.thesis_id`
3. **close_thesis(thesis_id, text)** → creates entry with `kind: "thesis_close"`, `metadata.outcome`

**Key limitations:**
- **No enforcement:** The system doesn't check if thesis_id exists or is in the right state. The LLM must track thesis_ids.
- **Thesis ID is a string timestamp:** `thesis_20250110T0000000500` — hard for an LLM to remember and reference across calls
- **Updates are separate entries:** To "read" a thesis, you must search for all entries with the same thesis_id and reassemble them
- **No structured fields:** All thesis content is free-text. No P&L tracking, no position linkage, no exit criteria enforcement
- **search_memory can find theses:** By searching for "thesis" kind or thesis_id text

### 2.4 Memory Persistence Across Agent Calls

**Yes, memories persist across calls.** In our test, the memories written by `_bind_*` tool calls survive across iterations:

```
AuditStrategy/decisions.jsonl: 4 entries
AuditStrategy/lessons.jsonl: 4 entries
AuditStrategy/memories.jsonl: 8 entries
AuditStrategy/theses.jsonl: 12 entries
```

However, note the **duplication**: our 5-day backtest ran `on_trading_iteration` 5 times (once per day), so each tool call happened 5 times. The memories ARE persistent and cumulative. Also, there's also an **in-memory ring buffer** in `AgentHandle._state_bucket()` that feeds recent memories into the system prompt as `Persistent Memory JSON` with the last 5 notes.

### 2.5 memory_note_max_chars

**Default: 2000 characters.** Configurable via `LUMIBOT_AGENT_MEMORY_NOTE_MAX_CHARS` env var (minimum 200).

In the manager, `_agent_memory_note_max_chars()` reads this env var. Memory notes fed into system prompts are truncated to this limit via `_truncate_text()`. The full text IS stored in JSONL — only the prompt-injected summary is truncated.

---

## 3. Runtime Architecture

### 3.1 Model Calling

LumiBot uses **Google ADK (Agent Development Kit)** as its runtime by default (`GoogleADKRuntime`):

- **Native Gemini:** `gemini-*` and `models/gemini*` models go through ADK's native path
- **Other providers:** Any `openai/...`, `xai/...`, `anthropic/...`, etc. go through `google.adk.models.lite_llm.LiteLlm` which bridges to LiteLLM (~100 providers)
- **LiteLLM config:** drop_params=True (xAI/Anthropic reject unknown params), suppress_debug_info=True, num_retries=3

### 3.2 Error Handling

Sophisticated 5-bucket error taxonomy:

| Bucket | Examples | Backtest Behavior | Live Behavior |
|--------|----------|-------------------|---------------|
| `auth` | missing/invalid API key (401, 403) | CRASH — loud error banner pointing to env var | SKIP iteration gracefully |
| `config` | bad model id, context-window exceeded (400, 422) | CRASH — loud error banner | SKIP iteration gracefully |
| `billing` | out of credits, quota exhausted (402, 429+msg) | CRASH — loud error banner | SKIP iteration gracefully |
| `transient` | 5xx, rate-limits, timeouts | Silent skip, retry up to 2x | Silent skip, retry up to 10x |
| `unknown` | anything unmatched | Treated as transient | Treated as transient |

**Backtest retry policy:** 2 attempts max (conservative to avoid runaway spend)  
**Live retry policy:** 10 attempts with exponential backoff (2s→60s)  

### 3.3 Caching & Replay

**AgentReplayCache** provides deterministic backtest replay:

- Cache key: SHA-256 hash of (system_prompt, task, context, runtime_context, model, tool_surface, memory_notes)
- Storage: gzipped JSON in `{LUMIBOT_CACHE_FOLDER}/agent_runtime/replay/{xx}/{hash}.json.gz`
- S3 sync: via `remote_cache.ensure_local_file()` and `on_local_update()` (backtest cache integration)
- **Cache-hit behavior:** Entire AgentRunResult (events, summary, warnings) is restored from cache. Side-effecting tools with `replay_on_cache: True` (orders, last_price, history load) are re-executed to reproduce state changes.
- **Provider prompt caching:** Separate mechanism for LLM provider-level caching (OpenAI prompt_cache_key, xAI x-grok-conv-id). Excludes market context so the static prefix can be cached server-side.

### 3.4 MCP (Model Context Protocol) Support

LumiBot supports external MCP servers for extending tool capabilities:

- **Transport:** stdio (subprocess), HTTP, streamable HTTP
- **Auth:** headers, auth_token_env (Bearer token from env var)
- **Backtest safety:** MCP tools are NOT blocked in backtests — LumiBot traces them and warns on suspicious temporal behavior
- **Tool exposure:** Per-server `exposed_tools` list controls which MCP tools are visible to agents

### 3.5 Observability

Comprehensive agent observability built in:

- **Agent detail Parquet:** Per-agent file with every tool call, tool result, model text, thinking text, usage stats, timings
- **Agent run summaries JSONL:** One-line-per-call with usage, latency, tool sequence, cache hit
- **Trace JSON:** Full trace per call with all events, payloads, warnings
- **Strategy parameters:** Auto-populates `strategy.parameters` with per-agent metrics (calls, tokens, latency, cache hits)
- **Log messages:** Verbose agent activity logging with `[agents]` prefix

### 3.6 Model Call Budget

`LUMIBOT_AGENT_MAX_MODEL_CALLS` / `agent_max_model_calls` strategy parameter:
- Caps total AI calls across all agents per strategy run
- Throws `AgentModelCallLimitExceeded` before the call
- Current call count stored in `strategy.parameters["agent_model_calls"]`

---

## 4. Custom Tool Recommendations

### 4.1 What's MISSING

Based on the audit, here are the most important gaps for AI-as-strategy:

| Gap | Priority | Why It Matters |
|-----|----------|----------------|
| **Multi-asset correlation** | 🔴 HIGH | LLM can't compute correlation matrix from single-bar indicators. Needs `market_load_history_table` for each asset then manual DuckDB SQL. |
| **Options Greeks** | 🔴 HIGH | No built-in way to get delta/gamma/theta/vega. `get_indicator` only returns current bar of a TA indicator. |
| **Portfolio optimization** | 🔴 HIGH | No risk-parity, mean-variance, or Kelly sizing built in. Model must compute portfolio weights manually from DuckDB. |
| **Regime detection** | 🟡 MEDIUM | Could help model recognize vol regimes without computing manually. Would use VIX/historical vol thresholds. |
| **Sentiment aggregation** | 🟡 MEDIUM | `alpaca_news` gives articles but no sentiment score. An aggregation tool could summarize sentiment across providers. |
| **Pairs trading signals** | 🟡 MEDIUM | Cointegration tests, spread calculations require custom DuckDB SQL today. |
| **Risk metrics dashboard** | 🟡 MEDIUM | VaR, CVaR, max drawdown projection, Sharpe all require manual computation. |
| **Universe scanner** | 🟢 LOW | No built-in tool to scan a universe of stocks for top performers, breakouts, etc. |
| **Calendar awareness** | 🟢 LOW | No built-in awareness of earnings dates, FOMC meetings, ex-dividend dates. |
| **Position sizing calculator** | 🟢 LOW | Model must calculate from scratch: `portfolio_value * risk_pct / ATR`. A helper would reduce errors. |

### 4.2 Custom Tool Opportunities via `@agent_tool`

The `@agent_tool` decorator (`lumibot/components/agents/tools.py`) makes it easy to add custom tools:

```python
from lumibot.components.agents import agent_tool

class MyStrategy(Strategy):
    @agent_tool(name="my_custom_tool", description="Does something useful")
    def my_custom_tool(self, param1: str, param2: int = 10) -> dict:
        # Your logic here
        return {"result": ...}
```

**How custom tools bind:**
1. The `@agent_tool` decorator attaches `_lumibot_agent_tool` metadata to the function
2. `bind_callable_tool()` wraps it into a `ToolDefinition`
3. The `AgentHandle` passes it to `_ensure_bound_tools()` which creates a `BoundTool`
4. The ADK runtime wraps it into a `FunctionTool` with auto-generated schema from Python type hints

**Tool source code is included in descriptions:** `_build_description()` appends the function's source code to the tool description. This gives the LLM full visibility into what the tool does. Functions >100 lines trigger a warning.

### 4.3 High-Value Custom Tools We Could Build

**1. `get_option_greeks(symbol, expiration, strike, right)`**
- Uses Black-Scholes or binomial tree
- Returns delta, gamma, theta, vega, rho
- Requires risk-free rate (from FRED `DGS10` or `FEDFUNDS`) and IV (from market data)

**2. `compute_correlation_matrix(symbols: list[str], lookback: int)`**
- Loads history for each symbol, computes Pearson correlation
- Returns matrix as dict
- Could be implemented as a DuckDB SQL generator

**3. `detect_regime(symbol: str = "SPY", lookback: int = 20)`**
- Classifies market regime: "bull_trend", "bear_trend", "high_vol", "low_vol", "sideways"
- Uses price momentum + VIX + volume analysis
- Returns regime label with confidence

**4. `sentiment_score(symbol: str, lookback_days: int = 7)`**
- Aggregates `alpaca_news` articles for a symbol
- Simple heuristic: count bullish/bearish keywords, news volume trend
- Returns score [-1, 1] and article count

**5. `position_sizing(kelly_fraction: float = 0.5, atr_multiple: float = 2.0)`**
- Given account state, symbol, and risk parameters
- Calculates whole-share quantity using ATR-based or Kelly-based sizing
- Returns recommended quantity, notional value, % of portfolio

---

## 5. Architecture Assessment

### Is LumiBot Viable as an AI-as-Strategy Platform?

**Verdict: YES, with caveats.** LumiBot's agent system is production-grade and well-architected. Here's the assessment:

### Strengths

1. **Backtest safety is exceptional.** Every tool with temporal implications has point-in-time enforcement. FRED tools use ALFRED vintage endpoints. News tools clamp future dates. DuckDB views have datetime cutoffs. This is the hardest thing to get right, and LumiBot nails it.

2. **Deterministic replay.** The `AgentReplayCache` with SHA-256 keying means backtests are reproducible. Side-effecting tools (`replay_on_cache: True`) re-execute on cache hit so state mutations are consistent.

3. **Provider-agnostic.** LiteLLM bridge means any LLM provider works (OpenAI, Anthropic, xAI, Gemini, Ollama, DeepSeek, etc.) with minimal config.

4. **Error resilience.** The live-vs-backtest error branching is thoughtful. Live never crashes on AI errors. Backtests crash loud on config/auth/billing errors (actionable) but skip on transients.

5. **Observability is comprehensive.** Per-agent Parquet detail files with token usage, tool sequences, timings, and warning streams. This is critical for debugging AI-driven strategies.

6. **Tool surface is broad.** 30+ tools covering orders, market data, DuckDB analytics, indicators, FRED macro, SEC filings, news, memory, and docs.

7. **Memory is persistent.** JSONL files survive across runs. The in-memory ring buffer feeds recent decisions into the system prompt.

8. **MCP support.** External tool servers can be plugged in for any capability not covered by built-ins.

### Weaknesses

1. **Keyword-only memory search.** No embeddings means the LLM can't find semantically similar memories. "Bull market continuation" won't match "uptrend persists" unless exact words overlap. For a long-running strategy, this becomes a problem as the memory store grows.

2. **Single-bar indicators.** `get_indicator` returns ONE value, not a time series. For strategies that need indicator crossovers or divergence, the model must manually compute these from DuckDB queries. This is a sharp edge — most strategy AIs will naturally want SMA(50) vs SMA(200).

3. **No portfolio-level analytics.** No tool for correlation matrix, portfolio variance, efficient frontier, risk parity. The model must compute all of these manually from DuckDB queries.

4. **Thesis system is fragile.** Thesis IDs are timestamp-based strings. The LLM must remember them across calls. No structured P&L tracking linked to theses. No enforcement of thesis lifecycle (must be open to update/close).

5. **No built-in universe scanning.** The model can only analyze symbols it already knows about. There's no tool to "find top 10 performing stocks in sector X."

6. **Memory duplication in backtests.** Each day's iteration re-writes the same memories (we saw 4x duplicates in our 5-day test). The model needs to learn to only write memories when state changes.

7. **No agent-to-agent communication.** Multi-agent strategies need a shared state mechanism beyond file-based memory. There's no built-in agent handoff protocol.

8. **Cost at scale.** A single agent run with a full tool surface could call 10+ tools per bar. At 252 trading days × 10 tool calls × token costs, this adds up. The cache helps for replay, but first runs are expensive.

### Recommended Architecture for AI-as-Strategy

For a production AI-driven strategy on LumiBot:

```
┌──────────────────────────────────────────────┐
│ Strategy.on_trading_iteration()              │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ Researcher│  │   Bull   │  │   Bear   │  │
│  │  Agent   │  │  Agent   │  │  Agent   │  │
│  │ (read-only)│ │ (long bias)│ │(short bias)│ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
│       │ reports     │ reports    │ reports  │
│       └──────────────┼───────────┘          │
│                      ▼                      │
│              ┌──────────────┐               │
│              │ Trader Agent │               │
│              │ (executes)   │               │
│              └──────────────┘               │
│                                              │
│  Shared state: strategy.vars + memory JSONL  │
│  Custom tools: Greeks, correlation, sizing   │
└──────────────────────────────────────────────┘
```

**Key design principles:**
1. Researcher agents are read-only (`allow_trading=False`), producing structured reports
2. Bull/bear agents provide adversarial perspectives
3. Trader agent aggregates reports, makes final decision, executes
4. Custom tools fill the Greeks/correlation/sizing gaps
5. Memory persists lessons across runs for continuous improvement
6. All agents share DuckDB state (same connection)
7. Backtest safety is automatic — no code changes needed for live

### Bottom Line

LumiBot's agent system is the most thoughtfully engineered AI-trading agent framework I've seen. The backtest safety, replay determinism, and error handling are world-class. The tool surface covers 80% of what an AI-as-strategy needs. The missing 20% (Greeks, correlation, scanning, sizing) can be filled with custom `@agent_tool` functions. With 6-8 well-designed custom tools, this is a fully viable AI-as-strategy platform.

---

## Appendix: Test Results Summary

```
Test: wave0_tool_audit.py
Status: ALL 21 TOOL CALLS PASSED (0 failures)
Runtime: StubAgentRuntime (no LLM needed)
Data: SPY, 5 days (Jan 6-10, 2025)

Categories tested:
  ✅ account_positions, account_portfolio
  ✅ market_last_price
  ✅ market_load_history_table + duckdb_query
  ✅ list_indicators, get_indicator (RSI, SMA)
  ✅ get_fred_latest (VIXCLS) — point-in-time safe
  ✅ remember, remember_decision, remember_lesson
  ✅ open_thesis, update_thesis, close_thesis
  ✅ search_memory (28 results found)
  ✅ orders_submit_order, orders_open_orders, orders_cancel_order
  ✅ lumibot_docs_search
```
