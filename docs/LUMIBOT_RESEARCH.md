# LumiBot Research Report

**Research Date:** May 31, 2026  
**Repository:** https://github.com/Lumiwealth/lumibot  
**Documentation:** https://lumibot.lumiwealth.com  
**Cloud Platform:** https://botspot.trade

---

## Executive Summary

LumiBot is a **production-ready AI trading agent framework** that uniquely runs LLM agents **inside the backtest simulation loop**. Unlike QuantConnect (LLM outside loop), CrewAI/AutoGen (no backtesting), or hobby scripts, LumiBot provides:

- **LLM in the loop**: Agent reasons on every bar with point-in-time data
- **Replay caching**: Deterministic warm reruns, zero LLM/API costs
- **Same code for backtest/live**: Write once, test, deploy
- **MCP + @agent_tool**: 20,000+ external servers + custom REST API wrappers
- **Full observability**: Traces, warnings, artifacts for debugging

---

## 1. GitHub Repository Analysis

### Latest Release
- **Current Version:** v4.5.41 (as of research date)
- **PyPI:** https://pypi.org/project/lumibot/
- **Active Development:** Regular releases, active issue/PR activity

### Example Strategies
The repository includes **25+ example strategies** covering:

| Category | Examples |
|----------|----------|
| **Stocks** | `stock_buy_and_hold.py`, `stock_bracket_trade.py` |
| **Options** | `options_basic.py`, `options_pretty_good.py` |
| **Crypto** | `crypto_ema_crossover.py`, `crypto_buy_and_hold.py` |
| **Futures** | `futures_sma_crossover.py` |
| **Forex** | `forex_basic.py` |
| **AI Agents** | `ai_trading_team_bull_bear_leveraged_etf.py`, `agent_news_sentiment.py`, `agent_macro_risk.py`, `agent_momentum_allocator.py`, `agent_m2_liquidity.py` |

### AI Agent Examples (Canonical)

| Strategy | Description | Key Pattern |
|----------|-------------|-------------|
| **Bull/Bear Leveraged ETF** | Aggressive bull/bear team for leveraged ETF rotation | Multi-agent team with opposing views |
| **Bull/Bear Large-Cap Stocks** | Large-cap stock team with evidence, bull, bear, portfolio-manager roles | Structured debate format |
| **Ray Dalio Idea-Meritocracy** | Macro specialists argue growth, inflation, debt, liquidity | Disagreement agent stress-tests views |
| **Warren Buffett Value** | Annual-report, valuation, long-term business-quality | Fundamental analysis focus |
| **Bill Ackman Concentrated** | High-conviction large-cap with activist bull case and short-seller bear case | Concentrated portfolio approach |
| **Citadel Sector-Pods** | Sector ETF comparison through cyclical, defensive, risk lenses | Sector rotation |

### Notable Issues & PRs

| Issue/PR | Significance |
|----------|--------------|
| **Issue #1065** | Proposes "capital-aware pre-trade review as MCP tool" — indicates active MCP development interest |
| **PR #1061** | Adds optional Adanos market sentiment tool — shows extensible tool architecture |

---

## 2. Documentation Deep Dive

### Agent Architecture

**Agent Types:**
| Type | Permissions | Use Case |
|------|-------------|----------|
| **Research Agent** | Inspect only, no trading | Market analysis, data gathering |
| **Trading Agent** | Full trading permissions | Execute orders, manage positions |

**Agent Manager Pattern:**
```python
class MyStrategy(Strategy):
    def initialize(self):
        self.agents.create(
            name="portfolio_manager",
            default_model="gemini-3.1-flash-lite-preview",
            system_prompt=(
                "Use economic data to decide between TQQQ and SHV. "
                "Check interest rates, inflation, and growth conditions."
            ),
        )
    
    def on_trading_iteration(self):
        result = self.agents["portfolio_manager"].run()
        # result.summary, result.trades, result.warnings
```

**Key Insight:** Agents are created in `initialize()` and run inside `on_trading_iteration()` — they receive **point-in-time state** on every bar.

### Built-In Tools (Auto-Included)

| Category | Tools |
|----------|-------|
| **Account** | `account.positions`, `account.portfolio` |
| **Market** | `market.last_price`, `market.load_history_table` |
| **DuckDB** | `duckdb.query` (time-series SQL queries) |
| **Orders** | `orders.submit`, `orders.cancel`, `orders.modify`, `orders.open_orders` |
| **Docs** | `docs.search` |
| **SEC Fundamentals** | `get_income_statement`, `get_balance_sheet`, `get_cash_flow`, `get_company_facts`, `get_filings`, `search_filing`, `get_filing_document` |
| **FRED Macro** | `get_fred_series` (requires `FRED_API_KEY`, uses point-in-time parameters) |

### Custom Tools: @agent_tool Pattern

**Primary recommended approach** — works reliably in both backtests and live:

```python
from lumibot.components.agents import agent_tool
import requests

@agent_tool(
    name="get_stock_bars",
    description="Get historical daily price bars for a stock from Alpaca.",
)
def get_stock_bars(self, symbol: str, start: str = "", end: str = "", limit: int = 30) -> dict:
    """Get historical OHLCV bars from Alpaca.
    
    Args:
        symbol: Stock ticker (e.g., TQQQ, SPY, QQQ)
        start: Start date YYYY-MM-DD
        end: End date YYYY-MM-DD
        limit: Max bars to return
    """
    # Direct HTTP call — you control it fully
    resp = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars", ...)
    return resp.json()
```

**Critical Feature:** `@agent_tool` **auto-includes source code** in the tool description sent to the AI. The agent can see parameters, defaults, and implementation details.

### MCP Server Support

```python
from lumibot.components.agents import MCPServer

MCPServer(
    name="my-data-server",
    url="https://my-mcp-server.example.com/mcp",
    timeout_seconds=120,
)
```

- **20,000+ MCP servers** available
- Works over HTTP JSON-RPC
- Best for live trading or when third-party provides a dedicated server
- **@agent_tool is preferred for backtesting** (more reliable, full control)

### Memory System

**Storage:** SQLite-backed local storage
- Append-only events
- Retrieval provenance (track where memories came from)
- Query interface for semantic search

**Memory Tools (Auto-added):**
- Store observations/decisions
- Retrieve relevant context
- Track trade history

### Observability System

**Every agent run produces:**

| Artifact | Description |
|----------|-------------|
| **Compact log line** | Agent name, model, cache status, tool count, warning count, summary |
| **JSON trace file** | Full prompt, tool calls, results, warnings, cache metadata |
| **agent_run_summaries.jsonl** | One JSON line per run for programmatic analysis |
| **agent_traces.zip** | Packaged traces for full backtest |

**Observability Warnings:**
| Warning | Severity | Meaning |
|---------|----------|---------|
| No tools called | Medium | Agent decided without consulting tools |
| Tool error | High | A tool returned an error |
| Future-dated data | **Critical** | Tool result references data after simulated time — **look-ahead bias risk** |
| Unsupported order | High | Order submitted without visible supporting evidence |

**Trace Location:** `~/Library/Caches/lumibot/agent_runtime/` (macOS)  
Override with `LUMIBOT_CACHE_FOLDER`

### Replay Cache (Critical Feature)

**How it works:**
- SHA-256 hash of: prompt + context + model + tools + simulated timestamp
- Identical inputs → cached result instantly
- Warm reruns: **zero LLM calls, zero API costs**

**Benefits:**
- Deterministic backtests
- Fast iteration (seconds vs minutes)
- Cost control

**Clear cache:** Delete `~/Library/Caches/lumibot/agent_runtime/replay/`

---

## 3. BotSpot Cloud Platform

### What BotSpot Offers

**Managed LumiBot Infrastructure:**
| Feature | Description |
|---------|-------------|
| **Data Workers** | Pre-wired data feeds (no data pipeline setup) |
| **Backtest Workers** | Distributed backtesting infrastructure |
| **Broker Connections** | 10+ brokers pre-integrated |
| **Scheduling** | Automated strategy execution |
| **Monitoring & Alerts** | Real-time bot health monitoring |
| **Kill Switches** | Emergency stop controls |
| **Logs** | Centralized logging and analysis |

**Not a Generic Chatbot:** BotSpot is purpose-built for LumiBot — AI workflows, prompts, MCP tools, backtest setup, and deployment flow are all optimized for trading.

### BotSpot MCP Server

**Canonical endpoints:**
| Endpoint | Purpose |
|----------|---------|
| `https://mcp.botspot.trade` | Production MCP root (hosted connectors) |
| `https://mcp.botspot.trade/mcp` | Explicit MCP endpoint |
| `https://api.botspot.trade` | Non-MCP REST API |

**Authentication:**
- **OAuth 2.1 + PKCE** — recommended for Claude app/web, ChatGPT custom apps
- **API key bearer token** — recommended for Claude Code, Cursor, Codex, scripts

**What BotSpot MCP enables:**
- Generate/refine strategies from plain English
- Start/stop/status backtests
- List/sort/filter backtests with server-side ordering
- Query CSV artifacts using SQL
- Fetch strategy visuals and backtest artifacts
- Deploy live to 10+ brokers

**Client Setup Examples:**

```bash
# Claude Code (API key)
export BOTSPOT_API_KEY="botspot_YOUR_API_KEY"
claude mcp add --transport http botspot https://mcp.botspot.trade/mcp \
  --header "Authorization: Bearer $BOTSPOT_API_KEY"

# Cursor (.cursor/mcp.json)
{
  "mcpServers": {
    "botspot": {
      "url": "https://mcp.botspot.trade/mcp",
      "headers": {
        "Authorization": "Bearer botspo…_KEY"
      }
    }
  }
}
```

### BotSpot.trade Marketplace

- **Bot marketplace:** Browse strategies created by others
- **Custom bot building:** Use AI to generate your own strategies
- **Free trial:** Available at https://botspot.trade/agents

---

## 4. Community & Best Practices

### Found Community Content

| Resource | Type | Description |
|----------|------|-------------|
| **YouTube: Live Algorithmic Trading Bot** | Video tutorial | Building live trading bot with Python, LumiBot, Alpaca |
| **Toolify.ai Guide** | Tutorial | Step-by-step Python algorithmic trading bot |
| **LevelUp GitConnected** | Article | Backtesting trading indicators with mean reversion strategy |
| **Medium Article** | Blog | Real-world trade strategy backtest with pre-computed signals |
| **DeepWiki** | Docs | Getting started guide with strategy patterns |

### Best Practices from Documentation

**1. System Prompt Guidelines:**
- **Keep it short:** 2-3 sentences describing strategy thesis
- **Focus on:** What data to use, what assets to trade, allocation logic
- **Don't repeat:** Position sizing, order execution, look-ahead bias, tool usage (handled by base prompt)

**Good Example:**
```python
system_prompt=(
    "Use economic data to decide between TQQQ and SHV. "
    "Check interest rates, inflation, and growth conditions."
)
```

**2. @agent_tool over MCP for Backtesting:**
- `@agent_tool` gives full control over HTTP calls
- Works reliably in both backtest and live
- MCP servers are useful for live trading or third-party services

**3. Point-in-Time Data Discipline:**
- LumiBot gates SEC facts/filings by strategy datetime
- `FRED_API_KEY` tools use realtime parameters automatically
- Custom tools should respect date parameters

**4. Debugging Workflow:**
1. Read summary log line → check cache status, tool count, warnings
2. Inspect tool calls/results in logs
3. Open JSON trace for full record
4. Check for future-dated data warnings
5. Compare summary to actual trade decision

**5. Warm Backtest Iteration:**
- First cold run takes 20-40 minutes (6-year daily)
- Warm reruns: seconds
- Use cache for rapid strategy tuning

### Common Pitfalls

| Pitfall | Prevention |
|---------|-------------|
| **Agent only buys SHV** | Defensive parking asset — check tools return meaningful data, verify API keys, adjust system prompt for risk-on conditions |
| **Future-dated data warnings** | Critical in backtests — ensure tools don't request data after simulated time |
| **No tools called warning** | May be fine on quiet days, but investigate if frequent |
| **Stale tool schema in chat** | Start fresh chat/session so tool metadata reloads |

---

## 5. Feature Inventory

### What Exists (Confirmed)

| Feature | Status | Notes |
|---------|--------|--------|
| LLM inside backtest loop | ✅ Production | Unique differentiator |
| Replay cache | ✅ Production | Deterministic, fast reruns |
| @agent_tool decorator | ✅ Production | Primary tool pattern |
| MCP server support | ✅ Production | 20,000+ servers available |
| DuckDB queries | ✅ Production | Time-series SQL inside agent |
| SEC fundamentals | ✅ Production | Point-in-time gated |
| FRED macro data | ✅ Production | Requires API key |
| Multi-agent teams | ✅ Production | Bull/bear, sector-pod patterns |
| Observability traces | ✅ Production | Full debugging support |
| BotSpot managed cloud | ✅ Production | MCP server available |
| 10+ broker integrations | ✅ Production | Alpaca, Interactive, Tradier, etc. |
| 25+ example strategies | ✅ Production | Stocks, options, crypto, futures, forex |

### What We Thought Existed (Clarifications)

| Assumption | Reality |
|------------|---------|
| "Need to list built-in tools" | **Auto-included** — all built-in tools are added automatically |
| "MCP is primary for external data" | **@agent_tool is preferred** for backtesting reliability |
| "Need complex system prompt" | **2-3 sentences** — base prompt handles most concerns |
| "Agent trading requires special setup" | **Same code** for backtest and live — just switch mode |

---

## 6. Recommended Agent Architecture

### Single-Agent Strategy (Simplest)

```python
from lumibot.strategies import Strategy

class SimpleAgentStrategy(Strategy):
    def initialize(self):
        self.sleeptime = "1D"
        self.agents.create(
            name="portfolio_manager",
            default_model="gemini-3.1-flash-lite-preview",
            system_prompt=(
                "Use market momentum and sentiment to decide between "
                "SPY (risk-on) and SHV (defensive). Check recent price "
                "trends and news sentiment."
            ),
        )
        # Add custom tools
        self.agents.add_tool("portfolio_manager", get_my_custom_data)

    def on_trading_iteration(self):
        result = self.agents["portfolio_manager"].run()
        self.log_message(f"Decision: {result.summary}", color="yellow")
```

### Multi-Agent Team (Best for Complex Decisions)

```python
class TradingTeamStrategy(Strategy):
    def initialize(self):
        self.sleeptime = "1D"
        
        # Research agents (no trading permission)
        self.agents.create("bull_analyst", role="research", ...)
        self.agents.create("bear_analyst", role="research", ...)
        
        # Portfolio manager (can trade)
        self.agents.create(
            "portfolio_manager",
            role="trading",
            system_prompt=(
                "Review bull and bear arguments. Decide allocation. "
                "Only trade when conviction is high."
            ),
        )

    def on_trading_iteration(self):
        # Research agents gather data
        bull_view = self.agents["bull_analyst"].run()
        bear_view = self.agents["bear_analyst"].run()
        
        # Pass context to portfolio manager
        result = self.agents["portfolio_manager"].run(
            context={"bull_view": bull_view.summary, "bear_view": bear_view.summary}
        )
```

### External Data Integration

```python
from lumibot.components.agents import agent_tool
import requests
import os

@agent_tool(
    name="get_sentiment",
    description="Get news sentiment score for a stock.",
)
def get_sentiment(self, symbol: str, days: int = 7) -> dict:
    """Fetch sentiment from your proprietary API.
    
    Args:
        symbol: Stock ticker
        days: Lookback period in days
    """
    api_key = os.environ.get("MY_SENTIMENT_API_KEY")
    resp = requests.get(
        f"https://api.example.com/sentiment/{symbol}",
        params={"days": days, "key": api_key},
        timeout=15,
    )
    return resp.json()

# In strategy:
self.agents.add_tool("portfolio_manager", get_sentiment)
```

### Flows (No Fixed Graph Required)

LumiBot agents use **flexible Python code** for orchestration:
- No DAG/framework required
- Call agents in any order
- Pass data between agents via context
- Full Python control flow

---

## 7. Key Technical Details

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Default LLM provider |
| `OPENAI_API_KEY` | OpenAI model support |
| `ANTHROPIC_API_KEY` | Anthropic model support |
| `FRED_API_KEY` | FRED macro data tools |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | Alpaca data/broker |
| `LUMIBOT_CACHE_FOLDER` | Override cache location |
| `LUMIBOT_SEC_CACHE_DIR` | SEC response cache |
| `LUMIBOT_SEC_USER_AGENT` | Custom SEC API user agent |

### Cache Locations

| Platform | Default Path |
|----------|--------------|
| macOS | `~/Library/Caches/lumibot/agent_runtime/` |
| Linux | `~/.cache/lumibot/agent_runtime/` |
| Windows | `%LOCALAPPDATA%\lumibot\agent_runtime\` |

### Supported LLM Providers

| Provider | Models |
|----------|--------|
| Google Gemini | `gemini-3.1-flash-lite-preview` (default), others |
| OpenAI | GPT-4, GPT-4o, GPT-4-turbo, etc. |
| Anthropic | Claude 3.5 Sonnet, Claude 3 Opus, etc. |
| Others | Via configuration |

### Broker Integrations (10+)

- Alpaca
- Interactive Brokers
- Tradier
- Coinbase
- Binance
- Kraken
- Oanda (forex)
- And more...

---

## 8. References & Resources

### Official Documentation
- **Main docs:** https://lumibot.lumiwealth.com/
- **Agent guide:** https://lumibot.lumiwealth.com/agents.html
- **Agent examples:** https://lumibot.lumiwealth.com/agents_examples.html
- **Observability:** https://lumibot.lumiwealth.com/agents_observability.html
- **BotSpot MCP:** https://lumibot.lumiwealth.com/botspot_mcp.html
- **Backtesting:** https://lumibot.lumiwealth.com/backtesting.html

### GitHub
- **Repo:** https://github.com/Lumiwealth/lumibot
- **AI Trading Agents doc:** https://github.com/Lumiwealth/lumibot/blob/dev/docs/AI_TRADING_AGENTS.md
- **Backtesting Architecture:** https://github.com/Lumiwealth/lumibot/blob/dev/docs/BACKTESTING_ARCHITECTURE.md

### External Resources
- **BotSpot MCP Server (Glama):** https://glama.ai/mcp/servers/Lumiwealth/botspot-mcp
- **BotSpot MCP Server (LobeHub):** https://lobehub.com/mcp/lumiwealth-botspot-mcp
- **PyPI:** https://pypi.org/project/lumibot/

### Community Tutorials
- YouTube: "Building a LIVE Algorithmic Trading Bot with Python, Lumibot and Alpaca"
- Toolify.ai: "Step-by-Step Guide: Building a Python Algorithmic Trading Bot"
- LevelUp GitConnected: "Backtesting Trading Indicators with Lumibot"

---

## 9. Summary for Nexus Trade

### What LumiBot Provides That Nexus Trade Could Leverage

1. **Proven agent-in-backtest-loop pattern** — unique in the market
2. **Replay caching** — solves LLM cost/determinism issues
3. **Built-in point-in-time data gating** — SEC, FRED, market data
4. **Observability by default** — traces, warnings, artifacts
5. **Multi-agent team patterns** — bull/bear, sector pods, debate formats
6. **MCP ecosystem access** — 20,000+ external tools

### Key Architectural Decisions to Consider

1. **@agent_tool vs MCP**: Prefer `@agent_tool` for backtesting reliability
2. **System prompts**: Keep minimal (2-3 sentences), let base prompt handle infrastructure
3. **Agent roles**: Research (no trading) vs Trading (can mutate orders)
4. **Debugging workflow**: Start with summary log, then trace JSON, then warnings
5. **Cache strategy**: Warm reruns for iteration, clear for fresh results

### Potential Integration Points

- **BotSpot MCP server**: Could use for managed backtesting/deployment
- **SEC fundamentals tools**: Direct integration for US equity analysis
- **Multi-agent patterns**: Borrow bull/bear debate format for decision-making
- **Observability artifacts**: Use trace format for debugging AI decisions

---

*Report generated: May 31, 2026*