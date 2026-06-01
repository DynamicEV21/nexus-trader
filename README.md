# NexusTrade — AI Trading Harness

> **Status:** Research & Prototyping Phase  
> **Goal:** Make LumiBot the universal AI trading harness — plug any model in, give it tools, data, and memory, and it becomes an autonomous trader.

## What This Is

NexusTrade is an AI-native trading system built on top of **LumiBot v4.5.25** that turns any LLM into a trading agent with:
- Full market data access (real-time, historical, fundamentals, macro)
- Institutional-grade backtest safety (no future data leakage)
- Persistent memory and thesis lifecycle management
- Custom quantitative tools (regime detection, portfolio optimization, signal generation)
- Multi-model investment committee architecture

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   NEXUSTRADE LAYER                     │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │ Custom Tools │  │ Memory Bridge │  │    Models    │ │
│  │ (regime,     │  │ (LumiBot →    │  │ GLM-5, DS-V4,│ │
│  │  signals,    │  │  Genome DB)   │  │ Gemini, etc) │ │
│  │  portfolio)  │  │               │  │             │ │
│  └──────┬──────┘  └──────┬────────┘  └──────┬──────┘ │
│         │                │                  │         │
│  ┌──────┴────────────────┴──────────────────┴──────┐ │
│  │              LUMIBOT TRADING ENGINE              │ │
│  │  30+ built-in tools | DuckDB | FRED | SEC      │ │
│  │  Backtest | Paper Trading | Live Trading        │ │
│  └────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
         │                        │
  ┌──────┴────────┐      ┌───────┴─────────┐
  │   Existing     │      │   Existing        │
  │   Projects     │      │   Projects        │
  │                │      │                    │
  │ quant-loop-    │      │ CrabQuant          │
  │ testnet        │      │ arena harness      │
  │ (strategy      │      │ (backtesting       │
  │  factory +     │      │  engine)           │
  │  genome DB)    │      │                    │
  └───────────────┘      └────────────────────┘
```

## Project Structure

```
nexus-trade/
├── src/
│   ├── tools/          # Custom LumiBot tools (regime, signals, portfolio)
│   ├── agents/         # Agent configurations, system prompts, committee setup
│   ├── strategies/     # LumiBot strategy wrappers
│   ├── memory/         # Memory bridge: LumiBot JSONL → Strategy Genome DB
│   └── custom_tools/   # Standalone tools that get bound into LumiBot agents
├── tests/              # Rapid-fire test scripts
├── docs/
│   ├── PRD.md          # This document — product requirements & vision
│   ├── ROADMAP.md      # Implementation roadmap
│   ├── AUDIT_REPORT.md  # Wave 0 tool audit findings
│   └── MODEL_GUIDE.md  # Which models to use for what
├── config/             # Agent configs, model routing, API keys
└── README.md
```

## Key Files

| File | Purpose |
|------|---------|
| `docs/PRD.md` | Full product requirements, capabilities, integration plan |
| `docs/ROADMAP.md` | Phased implementation plan |
| `docs/AUDIT_REPORT.md` | LumiBot tool audit (Wave 0) |
| `docs/MODEL_GUIDE.md` | Model selection guide with tool-calling benchmarks |

## Dependencies

- **LumiBot v4.5.25** — core trading engine (installed at `~/development/trading-bots/lumibot/`)
- **quant-loop-testnet** — strategy factory + genome DB
- **CrabQuant** — arena harness + backtesting validation
- **strat-depot** — 7,000+ converted strategies to feed the pipeline
- **agentic-quant-os** — master vision & architecture reference
