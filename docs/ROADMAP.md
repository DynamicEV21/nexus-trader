# NexusTrade — Implementation Roadmap

> **Version:** 0.1  
> **Last Updated:** 2026-05-31  
> **Status:** Research phase complete, implementation planning

---

## Phase 0: Validation (Week 0) ✅ IN PROGRESS

**Goal:** Prove LumiBot agents can actually trade and generate signals.

### Completed
- [x] Wave 0: Tool audit — all 21 built-in tools verified working
- [x] GitHub/docs research — architecture, best practices, patterns understood
- [x] Integration map — all 8 repos mapped to NexusTrade integration points
- [x] Model guide — preliminary model assignments drafted

### In Progress
- [ ] Wave 1: Multi-call backtest (GLM-5, 60 bars) — fix model name, re-run
- [ ] Rapid-fire benchmarks across models — tool-calling success rates

### Remaining
- [ ] Wave 2: Thesis multi-asset test (GLM-5, structured thesis lifecycle)
- [ ] Wave 3: Multi-agent committee test (bull/bear + PM)
- [ ] Wave 4: Custom tool test (regime_detect, signal_dashboard)

### Exit Criteria
- At least one model successfully trades with tools across 20+ bars
- Tool-calling benchmarks for top 5 models
- Preliminary P&L comparison vs SPY benchmark
- Decision: proceed to Phase 1 or pivot approach

---

## Phase 1: Memory Bridge + Core Tools (Week 1-2)

**Goal:** Make AI memory persistent across backtest runs. Build first custom tools.

### 1.1 Memory Bridge: LumiBot → Genome DB
```
Source: LumiBot memory JSONL files (decisions.jsonl, lessons.jsonl, theses.jsonl)
Target: quant-loop-testnet/conveyor/genome_db.py (SQLite)

Implementation:
1. Parse LumiBot memory JSONL after each backtest run
2. Extract: decisions (symbol, action, thesis), lessons (text, tags), outcomes (P&L, dates)
3. Write to genome_db: new tables for agent_decisions, agent_lessons, agent_theses
4. On next backtest: query genome_db for top lessons matching current regime
5. Inject into system prompt as "past experience" context
```

**Files:**
- `src/memory/bridge.py` — JSONL parser + genome_db writer
- `src/memory/injector.py` — Genome DB → system prompt formatter
- `src/memory/schema.sql` — New tables for agent memory

### 1.2 Custom Tool: `regime_detect()`
```
Source: CrabQuant regime detection (10-detector ensemble)
Implementation: @agent_tool decorator wrapping CrabQuant classify_window_regime()
Returns: {regime: "BULL_TRENDING", confidence: 0.82, volatility_state: "low", ...}
```

### 1.3 Custom Tool: `strategy_history()`
```
Source: quant-loop-testnet genome_db
Implementation: @agent_tool that queries similar regime/period results
Returns: "In similar regimes (VIX<15, SPY SMA50>SMA200), momentum strategies had 68% win rate over 20 tests"
```

### Exit Criteria
- [ ] AI in backtest N+2 references lessons from backtest N+1
- [ ] regime_detect() returns accurate classification
- [ ] strategy_history() queries genome_db successfully

---

## Phase 2: Signal Tools + Multi-Agent (Week 3-4)

**Goal:** Pre-computed signal dashboard. Multi-agent committee architecture.

### 2.1 Custom Tool: `signal_dashboard()`
```
One tool call returns:
{
  "spy": {"rsi_14": 62, "sma_cross": "bullish", "momentum_5d": 0.03, "regime": "BULL"},
  "qqq": {"rsi_14": 55, "sma_cross": "bullish", "momentum_5d": 0.02, "regime": "BULL"},
  "vix": {"level": 14.2, "trend": "falling", "percentile_30d": 25},
  "fed": {"rate": 5.25, "trend": "stable"},
  "portfolio": {"beta": 0.8, "concentration": 0.45, "max_loss_potential": -0.04},
  "recommendation": "RISK_ON — moderate conviction, VIX low, trend aligned"
}
```

### 2.2 Multi-Agent Committee
```
Agents:
1. bull_agent (GLM-5) — role="research", optimistic analysis
2. bear_agent (DeepSeek-V4) — role="research", contrarian analysis  
3. researcher_agent (Gemini-3FP) — role="research", deep data analysis
4. pm_agent (GLM-5.1) — role="trading", final decision, executes

Flow:
Each research agent gets signal_dashboard + strategy_history
Each produces a recommendation (buy/hold/sell with conviction)
PM agent sees all three + portfolio state, makes final call
Disagreement = reduce position size
Unanimous = full conviction trade
```

### 2.3 Custom Tool: `arena_backtest()`
```
Source: CrabQuant arena harness
Implementation: @agent_tool that validates a strategy idea in real-time
The AI proposes a strategy → arena_backtest tests it → returns result
"Proposed: Buy TQQQ when RSI < 30 and VIX < 20. Result: Sharpe 0.8, 14 trades, 62% win rate over 2yr"
```

### Exit Criteria
- [ ] Multi-agent committee produces decisions with structured reasoning
- [ ] signal_dashboard() returns actionable data in one tool call
- [ ] arena_backtest() validates strategies from within the agent loop

---

## Phase 3: Paper Trading + Iteration (Week 5-8)

**Goal:** Move from backtesting to live paper trading with real market data.

### 3.1 Paper Trading Setup
- Connect LumiBot to Alpaca paper trading account
- Daily agent decision loop (market close → AI reasons → execute before close)
- Observability: monitor agent traces, warnings, P&L daily

### 3.2 Cross-Run Learning Validation
- Compare AI performance on week 1 vs week 4
- Measure if Genome DB lessons improve decision quality
- A/B test: with memory bridge vs without

### 3.3 Signal Refinement
- Tune signal_dashboard thresholds based on paper trading results
- Add new signals based on what the AI finds useful
- Remove signals that don't add value

### Exit Criteria
- [ ] 2+ weeks of paper trading data
- [ ] P&L tracking vs SPY benchmark
- [ ] Measurable improvement from memory bridge (A/B comparison)
- [ ] Decision: proceed to Phase 4 or iterate

---

## Phase 4: Production (Week 9+)

**Goal:** Small live capital deployment with safeguards.

### 4.1 Safety Infrastructure
- Maximum daily loss limit (kill switch)
- Maximum drawdown limit (auto-liquidate)
- Position size limits (never > 40% single position)
- Drift detection (is AI behaving differently than backtest?)

### 4.2 Scaling
- Start with $500-1000
- Scale to $5000 if profitable for 4 weeks
- Scale to $10000+ if Sharpe > 1.0 over 8 weeks

### 4.3 Continuous Improvement
- Weekly strategy review
- Genome DB grows with each run
- Model upgrades as better models become available
- Custom tools evolve based on agent behavior analysis

---

## Dependencies

| Phase | Requires | External |
|-------|----------|----------|
| 0 | LumiBot v4.5+ installed, Ollama Cloud API | None |
| 1 | quant-loop-testnet Genome DB, CrabQuant regime detector | FRED API key |
| 2 | Phase 1 complete, multiple models available | None |
| 3 | Alpaca paper trading account | Alpaca API key |
| 4 | Phase 3 successful | Alpaca live account |

---

## Risk Gates

| Gate | Criteria | Action if Failed |
|------|----------|----------------|
| Phase 0→1 | At least 1 model trades successfully | Debug tool calling, try different models |
| Phase 1→2 | Memory bridge shows AI references past lessons | Improve extraction/injection quality |
| Phase 2→3 | Multi-agent committee outperforms single agent | Simplify to single agent, focus on tools |
| Phase 3→4 | Paper trading beats SPY over 2 weeks | Iterate on signals/prompts, extend paper period |
| Live | Sharpe > 0.5, max DD < 15% | Reduce position size, add more safeguards |

---

*Roadmap updated as research results come in.*
