# Nexus Trader — Architecture & Roadmap

**Last Updated:** 2026-05-31  
**Status:** Alpha — Core built (53/53 tests), integration pending

---

## What Nexus Trader IS

Nexus Trader is the **execution layer** that ties together:
1. **AI reasoning** (GLM-5 via Lumibot's agent system or OpenClaw)
2. **Risk controls** (GuardPipeline with 7 guards + circuit breakers)
3. **Broker execution** (Lumibot as the trading harness)
4. **Data layer** (agentic-quant-os lakehouse + financial-data pipeline)

**Lumibot is the harness, NOT the brain.** It provides:
- `self.get_historical_prices("SPY")` — market data
- `self.submit_order(order)` — trade execution
- `self.cash`, `self.portfolio_value`, `self.positions` — portfolio state
- `self.agents.create()` — **built-in multi-agent system** (evidence → bull → bear → PM)

## Three-Layer Architecture

```
LAYER 1: QUANT-12-AGENT-TS (Discovery)
  "Create new strategies from scratch"
  12 specialized agents: playbook → setup → trade plan → execution → implement → red team → validate → rank → postmortem → mutate
  TypeScript, runs in Node.js, outputs strategy code + backtest results
  ──→ Stores discovered strategies in lakehouse

LAYER 2: AGENTIC-QUANT-OS (Data)
  "Store, query, and analyze everything"
  DuckDB (15 tables, 2141 rows) + LanceDB (3072-dim vector embeddings)
  QuantBridge API: ticker_intelligence, similar_strategies, preflight_check, regime_aware_signals
  SignalBus pub/sub for real-time events
  ──→ Serves as the memory for all layers

LAYER 3: NEXUS TRADER (Execution)  ← THIS PROJECT
  "Execute strategies safely with AI oversight"
  GuardPipeline (7 guards) → GuardedBroker (wraps Lumibot) → Audit Trail (SHA-256)
  Multi-agent reasoning via Lumibot's self.agents or TradingAgents debate
  Circuit breaker + risk limits (YAML-configured)
  ──→ Results feed back into Layers 1 & 2
```

---

## Implementation Status

### ✅ Built (53/53 tests passing)
| Component | File | Lines | Description |
|-----------|------|-------|-------------|
| GuardPipeline | `guards/pipeline.py` | ~80 | Short-circuit pipeline, DENY stops chain |
| Guard ABC | `guards/base.py` | ~60 | GuardResult, GuardAction, GuardContext |
| Symbol Whitelist | `guards/symbol_whitelist.py` | ~30 | Only trade approved symbols |
| Position Limits | `guards/position_limits.py` | ~50 | Max position + max order size |
| Daily Loss Limit | `guards/daily_loss_limit.py` | ~40 | Halt on daily drawdown |
| Cooldown Guard | `guards/cooldown.py` | ~30 | Min time between trades per symbol |
| Market Hours | `guards/market_hours.py` | ~30 | Only trade during market hours |
| Order Types | `guards/order_types.py` | ~25 | Only allow approved order types |
| Audit Trail | `audit/trail.py` | ~100 | SHA-256 hash chain, SQLite, tamper-detectable |
| Lakehouse Connector | `lakehouse/connector.py` | ~80 | Read/write agentic-quant-os + 3 Nexus tables |
| Circuit Breaker | `circuit_breakers/drawdown.py` | ~60 | Progressive: warn 5%, reduce 7%, halt 10% |
| GuardedBroker | `brokers/guarded_broker.py` | ~80 | Wraps any Lumibot broker, intercepts _submit_order() |
| Config System | `config/settings.py` + 3 YAMLs | ~100 | YAML-configurable guards, symbols, risk limits |

### 🚧 In Progress
| Component | Status | What's Needed |
|-----------|--------|---------------|
| Backtesting Integration | Starting now | Wire BacktestingBroker → GuardPipeline → PandasData |
| OpenClaw Skill | Starting now | Skill to launch/manage backtests from OpenClaw |
| Lumibot Agent Bridge | Starting now | Connect Lumibot's self.agents to Nexus Trader |

### ❌ Not Started
| Component | Priority | Notes |
|-----------|----------|-------|
| Paper Trading (Live) | High | Needs Alpaca signup (free, 2 min) or CCXT crypto |
| Post-Backtest Debate | Medium | Feed results to TradingAgents 3-agent debate |
| Discovery Loop | Medium | Feed results back to quant-12-agent-ts |
| Real-Time Monitor | Medium | OpenClaw heartbeat for circuit breaker state |
| WebSocket Crypto | Low | Binance/Kraken WS for live crypto data |
| Futures Support | Low | Via Tradovate (needs account) |
| Forex Support | Low | Limited via Alpaca or CCXT |

---

## Data Available for Backtesting (NO SIGNUP NEEDED)

### Stocks
- **503 S&P tickers** — yfinance daily, 5 years (already on disk)
- **33 tickers** — Polygon daily with real VWAP, 1 year (already on disk)
- Path: `quant-projects/financial-data/stocks/`

### Crypto  
- **12 pairs** — Kraken 1-min bars, 801MB (BTC, ETH, SOL, XRP, ADA, AVAX, DOT, LINK, LTC, BCH, ATOM, DOGE)
- **18GB** Kraken trading history
- **Binance daily** for many more pairs
- Path: `quant-projects/financial-data/crypto/`

### Macro
- **15+ FRED indicators** — unlimited access
- Path: `quant-projects/financial-data/macro/fred/`

### API Keys Available
Polygon, FRED, FMP, Finnhub, Alpha Vantage, Tiingo (stored in OpenBB settings)

### Free APIs (No auth)
Binance (1200 req/min), Kraken (6/sec), Coinbase (10/sec), yfinance, SEC EDGAR

---

## Multi-Agent Reasoning Systems (Already Built)

### 1. Lumibot Investment Committee (RECOMMENDED for trading)
- 4 agents: Evidence Researcher → Bull → Bear → Portfolio Manager
- Built into Lumibot's `self.agents` system
- Sequential chain, each sees previous outputs
- PM agent has `allow_trading=True`
- Already tested with GLM-5
- **Use this for the actual trading decision loop**

### 2. TradingAgents Debate Pipeline  
- 3 agents: Conservative → Aggressive → Neutral Judge
- Built into TradingAgents framework
- Takes backtest results → debates validity → ADOPT/MODIFY/REJECT
- **Use this for post-backtest strategy evaluation**

### 3. Quant-12-Agent-TS Discovery Pipeline
- 12 specialized agents for strategy creation
- TypeScript, independent of Lumibot
- **Use this for discovering new strategies to feed into Nexus Trader**

### Decision: Hybrid Architecture
- **Lumibot agents** for in-loop trading decisions (fast, tested)
- **OpenClaw** for research, monitoring, backtesting coordination
- **TradingAgents** for post-backtest evaluation
- **quant-12-agent-ts** for strategy discovery

---

## Backtesting Options in Lumibot

| Source | Auth | Speed | Best For |
|--------|------|-------|----------|
| YahooDataBacktesting | None | Fast (downloads on fly) | Stock daily bars |
| CcxtBacktesting | None | Fast (auto-downloads) | Crypto any timeframe |
| PandasDataBacktesting | None | Instant (local files) | Our cached parquet data |
| PolygonDataBacktesting | API key | Slow (5/min) | High-quality stock data |

---

## Vehicle Support

| Vehicle | Backtesting | Paper Trading | Live | Notes |
|---------|-------------|---------------|------|-------|
| **US Stocks** | ✅ Yahoo/Polygon | Alpaca (free) | Alpaca | 503 tickers on disk |
| **Crypto** | ✅ CCXT/Pandas | Binance Testnet | Binance/Kraken | 12 pairs on disk |
| **Futures** | ✅ (margin tables) | Tradovate | Tradovate | Needs Tradovate account |
| **Forex** | ✅ (via Alpaca/CCXT) | Limited | Limited | Not primary focus |
| **Options** | ✅ (limited) | Limited | Limited | Low priority |

---

## Next Steps (Immediate)

### Step 1: Wire Backtesting (TODAY)
1. Create backtest runner that uses PandasDataBacktesting with our parquet files
2. Connect GuardPipeline to BacktestingBroker via GuardedBroker
3. Simple momentum strategy as first test
4. Verify: trades pass through guards → audit trail records everything

### Step 2: Lumibot Agent Integration (TODAY)
1. Create AICommitteeStrategy using Lumibot's self.agents
2. Evidence → Bull → Bear → PM chain
3. PM decisions pass through GuardedBroker
4. Backtest the committee on SPY + QQQ with Yahoo data

### Step 3: OpenClaw Skill (NEXT)
1. Create SKILL.md for launching backtests from OpenClaw
2. Skill reads config from YAML files
3. Parses tearsheet + trades CSV → sends summary to Telegram
4. Circuit breaker monitoring heartbeat

### Step 4: Post-Backtest Debate (NEXT)
1. Feed backtest results to TradingAgents debate pipeline
2. Conservative/Aggressive/Neutral evaluate strategy
3. Store verdict in lakehouse

### Step 5: Paper Trading (WHEN READY)
1. Sign up for Alpaca paper trading (free, 2 min)
2. Swap BacktestingBroker for Alpaca broker
3. Same GuardPipeline + guards + audit trail
4. Start with $100K paper, 3-5 tickers, conservative limits

---

## File Structure

```
nexus-trader/
├── nexus_trader/
│   ├── __init__.py
│   ├── audit/
│   │   └── trail.py              # SHA-256 hash chain audit
│   ├── brokers/
│   │   └── guarded_broker.py     # Wraps any Lumibot broker
│   ├── circuit_breakers/
│   │   └── drawdown.py           # Progressive drawdown breaker
│   ├── config/
│   │   ├── settings.py           # Config loader
│   │   ├── guards.yaml           # Guard parameters
│   │   ├── symbols.yaml          # Symbol whitelist
│   │   └── risk_limits.yaml      # Risk limits
│   ├── guards/
│   │   ├── base.py               # Guard ABC
│   │   ├── pipeline.py           # GuardPipeline
│   │   ├── symbol_whitelist.py
│   │   ├── position_limits.py
│   │   ├── daily_loss_limit.py
│   │   ├── cooldown.py
│   │   ├── market_hours.py
│   │   └── order_types.py
│   └── lakehouse/
│       ├── connector.py          # Lakehouse read/write
│       └── tables.py             # Nexus-specific tables
├── tests/                        # 53 tests, all passing
├── research_financial_data.md    # Data inventory
├── research_agent_systems.md     # Agent systems analysis
├── research_backtesting.md       # Backtesting options
└── ROADMAP.md                    # This file
```

---

## Key Insight

**Lumibot already has everything we need for multi-agent trading.** The `self.agents` system provides:
- Agent creation with model selection
- Sequential chaining (output → context for next agent)
- Trading permission control
- Built-in error handling

We don't need to build the agent reasoning layer — we just need to:
1. **Guard it** (GuardPipeline → GuardedBroker) ✅ Done
2. **Audit it** (hash chain trail) ✅ Done
3. **Monitor it** (circuit breaker + OpenClaw) 🚧 In progress
4. **Connect it** to our data layer 🚧 In progress
