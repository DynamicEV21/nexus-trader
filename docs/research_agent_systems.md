# Agent Systems Research

**Date:** 2026-05-31  
**Examined:** strat-depot, quant-12-agent-ts, TradingAgents

## 1. Investment Committee (strat-depot/integrations/lumibot/)

**Location:** `/home/Zev/development/strat-depot/integrations/lumibot/test_investment_committee.py`  
**Status:** ✅ Working, 207 lines, uses Lumibot's built-in `self.agents` system

### Architecture
```
Lumibot Strategy
├── agents.create("evidence_researcher", model="glm-5")  ← Cannot trade
├── agents.create("bull_researcher", model="glm-5")      ← Cannot trade
├── agents.create("bear_researcher", model="glm-5")      ← Cannot trade
└── agents.create("portfolio_manager", model="glm-5")    ← CAN trade

Every 5 days (sleeptime="5D"):
  1. Evidence Researcher gathers price data + technicals
  2. Bull builds long case from evidence
  3. Bear attacks the bull case
  4. PM reviews all three and makes final decision
```

### Key Features
- Uses **Lumibot's built-in `self.agents`** framework — not OpenClaw sub-agents
- Sequential: Evidence → Bull → Bear → PM
- PM agent has `allow_trading=True`, others are `allow_trading=False`
- Each agent sees previous agents' outputs via `context` dict
- Uses GLM-5 via Ollama Cloud (`openai/glm-5`)
- Backtested with `YahooDataBacktesting`, no Alpaca needed

### Why This Matters for Nexus Trader
**This is already production-grade multi-agent reasoning inside Lumibot.** We don't need to build this from scratch in OpenClaw. We can:
1. Use Lumibot's `self.agents` system for the debate/committee
2. Add Nexus Trader's GuardedBroker wrapper for risk controls
3. The PM agent's trading decisions pass through our guard pipeline

---

## 2. Backtest Debate Pipeline (strat-depot/integrations/lumibot/)

**Location:** `backtest_debate_p1.py` + `backtest_debate_p2.py`  
**Status:** ✅ Working, 2-phase pipeline

### Architecture
```
Phase 1 (Lumibot venv):
  Run backtest → extract trades CSV → compute metrics (win rate, P&L, round trips) → JSON

Phase 2 (TradingAgents venv):
  Load metrics JSON → run 3-agent debate:
    1. Conservative debater (risk-averse)
    2. Aggressive debater (opportunity-seeking)  
    3. Neutral judge (consensus builder)
  → Produce verdict: ADOPT / MODIFY / REJECT
```

### Key Features
- Uses **TradingAgents** framework's risk management debate agents
- Real backtest results fed to LLM debate (not simulated)
- Dual debate: factor validation + model validation
- Uses GLM-5 via Ollama Cloud
- Outputs: debate transcripts (markdown) + pipeline metrics (JSON)

### Agents from TradingAgents
- `create_conservative_debator(LLM)` — risk-averse perspective
- `create_aggressive_debator(LLM)` — growth-seeking perspective  
- `create_neutral_debator(LLM)` — synthesis/consensus

---

## 3. Quant-12-Agent-TS (12-Agent Discovery Pipeline)

**Location:** `/home/Zev/development/quant-12-agent-ts/`  
**Status:** ✅ Working, TypeScript, 12-stage strategy discovery

### Architecture (12 Stages)
```
Prompt → generatePlaybookPlan
      → designSetupSpec (indicators, conditions, universe)
      → designTradePlan + designExecution (parallel)
      → implementStrategy (Python code generation)
      → redTeamStrategy (adversarial testing)
      → [BACKTEST]
      → validation + overfit check (parallel)
      → rankStrategy (scorecard)
      → analyzeRunPostmortem (failure analysis)
      → proposeMutationPlan (improvements)
      → visionKeepMutationPlan (approve/reject changes)
      → [LOOP if needed]
```

### Key Features
- **12 specialized LLM agents**, each focused on one stage
- Pipeline mode: `full_llm` | `hybrid` | `scripts_only`
- Has loop targets: SPEC | TRADE_EXEC | TEST_ONLY | NONE
- Stage persistence + caching (avoids re-running unchanged stages)
- Python worker pool for backtesting
- Regime matrix integration
- Research bridge to `quant-research-mas`

### Why This Matters for Nexus Trader
This is a **strategy DISCOVERY** system — it creates new strategies from scratch. Nexus Trader is a **strategy EXECUTION** system. They're complementary:
- quant-12-agent-ts discovers strategies → lakehouse stores them
- Nexus Trader executes strategies → lakehouse records results
- Loop: results feed back into discovery for improvement

---

## 4. Lumibot's Built-in Agent System

**Key Discovery:** Lumibot has `self.agents.create()` built in.

```python
class MyStrategy(Strategy):
    def initialize(self):
        self.agents.create(
            name="analyst",
            model="openai/glm-5",
            allow_trading=False,
            system_prompt="You are an analyst...",
        )
        self.agents.create(
            name="trader",
            model="openai/glm-5", 
            allow_trading=True,
            system_prompt="You are a trader...",
        )
    
    def on_trading_iteration(self):
        analysis = self.agents["analyst"].run(
            task_prompt="Analyze SPY",
            context={"price": 500, "rsi": 65},
        )
        # trader agent can submit orders
        self.agents["trader"].run(
            task_prompt="Decide whether to trade",
            context={"analysis": analysis},
        )
```

### What This Means
**We DON'T need OpenClaw sub-agents for the trading loop.** Lumibot already has:
- Multi-agent creation with `self.agents.create()`
- Sequential agent chaining (agent output → next agent context)
- Trading permission control (`allow_trading=True/False`)
- Built-in retry, error handling, logging
- Works with GLM-5 via OpenAI-compatible API

---

## Recommendation for Nexus Trader

### Option A: Use Lumibot's Agent System (RECOMMENDED)
- Pros: Already built, tested, works inside the trading loop
- Cons: Agents live inside Lumibot, harder to monitor from OpenClaw
- Best for: The actual trading decision loop

### Option B: Use OpenClaw Sub-Agents
- Pros: Full OpenClaw tool access, persistent sessions
- Cons: Session lock errors, not inside trading loop, latency
- Best for: Pre-trade research, post-trade analysis, monitoring

### Option C: Hybrid (BEST LONG-TERM)
1. **Lumibot agents** for real-time trading decisions (fast, in-loop)
2. **OpenClaw** for: research, risk monitoring, circuit breaker oversight, backtesting coordination
3. **quant-12-agent-ts** for strategy discovery (feeds into lakehouse)
4. **TradingAgents debate** for post-backtest strategy evaluation

### What to Build in Nexus Trader
1. A thin adapter that connects Lumibot's `self.agents` to our GuardedBroker
2. An OpenClaw skill for launching and monitoring backtests
3. Post-backtest: feed results to TradingAgents debate pipeline
4. Discovery loop: feed results back to quant-12-agent-ts
