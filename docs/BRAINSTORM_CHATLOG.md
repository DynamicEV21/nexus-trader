# Nexus Trader — Brainstorm Chat Log

> Source: Nexus Trader brainstorming session (transcribed from voice chat)
> Date: 2026-06-01
> Participants: Tristan + LLM assistant

---

## Overview

A deep-dive brainstorming session exploring the architecture for an adaptive, multi-agent LLM+ML trading system with causal reasoning, alpha factor discovery, and dynamic model selection.

---

## Session Transcript

### LLM's Role: Context Engine, Not Alpha Model

The core thesis: the LLM should **not** be the alpha generator. It should serve as:

- **Context engine** — synthesizing multiple signal streams
- **Research analyst** — interpreting news, filings, sentiment
- **Memory retrieval system** — querying similar historical setups
- **Risk manager** — evaluating position sizing and regime awareness
- **Meta-strategy selector** — choosing which models/strategies to trust

Actual alpha comes from statistical/ML models (LightGBM, CatBoost, Transformers, LSTMs).

### Proposed Architecture Layers

**Layer 1: Market Data**
- 1m, 5m, 15m, 1h, daily bars for a single stock
- Volume, VWAP, options flow, order flow, implied volatility, market breadth

**Layer 2: Prediction Models (Ensemble)**
- *Trend Model* (LightGBM/CatBoost) → predicts next 15m, 1h, 4h returns
- *Sequence Model* (Transformer/LSTM) → last 200 bars → future returns
- *Regime Model* → classifies: trend, range, breakout, reversal, vol expansion/contraction
- *Volatility Model* → forecasts ATR, realized vol, expected vol

**Layer 3: Memory System (LanceDB)**
- Stores important events (Fed meetings, earnings, gaps, vol spikes)
- Stores model decisions, confidence, outcomes
- LLM queries: "Show me 25 most similar situations" before acting

**Layer 4: News Intelligence (LLM-driven)**
- Every 15 minutes: collect SEC filings, earnings, analyst notes, Reddit, X/Twitter, macro news
- Compress into structured sentiment/importance/summary objects
- Feed compressed summaries (not raw articles) to models

**Layer 5: Meta-Decision Layer (LLM)**
- Receives all model outputs + news + memory
- Solves: "Given all evidence, should I act?" (not "Predict the market")

### Effort Allocation (Single-Stock 15m Strategy)
- 50% prediction models
- 20% feature engineering
- 15% risk management
- 10% memory/retrieval
- 5% LLM reasoning

---

## Alpha Factors Deep Dive

### Factor Discovery Loop
- LLM acts as **curator** — surfaces new alpha factors from structured/semi-structured sources
- Continuous feedback loop: LLM scans financial reports, market data, social sentiment
- Identifies factors that gained/lost predictive power
- Rolling basis feature set updates

### Multi-Agent Factor System
- **Factor Discovery Agent** — LLM-driven, scans data to propose new alpha factors
- **Factor Validation Agent** — backtests proposed factors (LightGBM/CatBoost) to quantify predictive power
- **Causal Graph Agent** — validates cause-effect relationships (DoWhy/CausalNex)
- **Risk Assessment Agent** — monitors portfolio exposure based on factors
- **LLM Reasoning Coordinator** — aggregates signals, explains decisions, adapts in real time

---

## Causal Reasoning Layer

### Why Causal Reasoning?
- Correlations break during regime shifts
- Causal relationships are more robust
- Enables counterfactual reasoning ("What would happen if X didn't occur?")

### Implementation Stack
- **DoWhy** / **CausalNex** — Python causal inference libraries
- **NetworkX** / **Neo4j** — graph structure for encoding causal links
- **CausalModel API**:
  - `identify_effect()` — figure out which variables drive outcomes
  - `estimate_effect()` — quantify causal impact
- Factors pass through causal validation before feeding into ML models
- LLM receives causal insights as structured input: "Given that factor X causes Y, and we observed Z…"

### Where DoWhy Applies
- Applied to factors before feeding into ensemble models
- Causal insights formatted as structured summaries for LLM reasoning
- Used when interpretability and causal confidence matter most (LightGBM, CatBoost)
- LLM anchored in causal logic — doesn't guess, reasons from validated cause-effect chains

---

## Memory & Retrieval Architecture

### Embedding Agent
- Dedicated agent converts market data, causal relationships, alpha factors → vector embeddings (~768-dimensional)
- Stores embeddings in **LanceDB** or **Pinecone** (vector database)
- LLM orchestrates retrieval but doesn't store/retrieve itself — stays nimble

### Causal Narrative Memory
- Market events stored in graph structure with causal links
- Each causal chain summarized as a short interpretive story
- LLM compares current conditions to past causal narratives
- Format: "This pattern reminds me of [past cause-effect chain]"

### Graph Database
- Neo4j for persistent causal relationship storage
- Structured memory stores causal chains
- Prompt engineering layer formats causal stories for LLM consumption

---

## Adaptive Multi-Agent System

### Dynamic Model Switching
- **Gating Agent** — monitors real-time signals
- **Factor Scoring Agent** — evaluates which factors are most predictive in real time
- **Model Selector Agent** — maintains model library (LightGBM, LSTM, Transformers, etc.), uses meta-learner (bandit algorithm) to weigh based on recent performance
- **Debate Agent** — LLM-driven, compares which model/strategy makes sense, provides rationale

### Meta-Learner
- Monitors each model's performance
- Adapts strategies based on market regimes
- Uses bandit algorithms or reinforcement learning
- Combines model outputs, causal reasoning, and real-time signals

### Agent Orchestration
- **Ray** for parallel agent execution
- **LangChain** for modular agent construction
- **PostgreSQL** or **Neo4j** for factor and causal graph storage
- Python ML pipelines for training/inference
- Local 8B LLM as decision arbiter

---

## Next-Level Evolutions Discussed

### Multi-Modal Foundation Agents
- Combine price data, news, and visual data (charts)
- Multi-modal inputs for richer context

### Meta-Learning
- System adapts its decision-making horizon over time
- Learns how to learn across market regimes

### Dynamic Agent Weight-Gating
- Different strategy agents weighted depending on market regime
- Real-time weight adjustments based on performance

### Fleet of Specialized LLM Agents
- Agents debate and cross-check each other's insights
- Fundamental analyst agent, sentiment analyst agent, technical analyst agent

### Reinforcement Learning with Simulated Markets
- System adapts to changing regimes through simulation
- Continuous training loop

### Causal Graph-Based Memory
- Explains not just patterns but *why* things happen
- Enables meta-learning and deeper adaptive reasoning

---

## Key Libraries & Tools Referenced

| Component | Tool/Library |
|---|---|
| Causal inference | DoWhy, CausalNex |
| Graph structure | NetworkX, Neo4j |
| Agent orchestration | Ray, LangChain |
| ML models | LightGBM, CatBoost, Transformer, LSTM |
| Vector database | LanceDB, Pinecone |
| Meta-learning | Bandit algorithms, RL |
| Language | Python |
| LLM | Local 8B model (decision arbiter) |
| Database | PostgreSQL |
| Memory | LanceDB (vector), Neo4j (graph) |

---

## Open Questions & Risks

- Overfitting to short-term noise in continuous factor discovery
- Out-of-sample validation requirements for new factors
- Latency constraints for real-time causal inference
- LLM reasoning reliability under novel market conditions
- Agent coordination overhead vs. alpha generated
