# NexusTrade Integration Map

> **Created:** 2026-05-31
> **Purpose:** Map all trading/quant repos to NexusTrade integration opportunities
> **Context:** NexusTrade is an AI trading harness built on LumiBot v4.5.25

---

## Executive Summary

NexusTrade sits at the intersection of 8 quant projects. The integration strategy follows the **agentic-quant-os** master vision: unified lakehouse, memory bridge, and tool ecosystem.

**Highest priority integrations:**
1. **quant-loop-testnet** — Strategy Genome DB becomes NexusTrade's persistent memory
2. **CrabQuant** — Arena harness + regime detection as custom LumiBot tools
3. **strat-depot** — 7,000+ strategies for AI to study, RSI-Centered-Pivots as baseline
4. **agentic-quant-os** — DuckDB lakehouse for unified data layer

---

## Repo Overview Table

| Repo | Purpose | Integration Type | Key Components |
|------|---------|------------------|----------------|
| **agentic-quant-os** | Master OS orchestrator | Data lakehouse | DuckDB + LanceDB, 8 connectors, Hermes agents |
| **CrabQuant** | Backtest engine + guardrails | Custom LumiBot tools | Arena harness, regime detection, vectorized backtest |
| **quant-loop-testnet** | Strategy factory + memory | Memory bridge | Genome DB, strategy classifier, conveyor pipeline |
| **strat-depot** | Strategy repository | Source material | 7,062 strategies, Pine→Python converter, RSI-Centered-Pivots |
| **quant-pipeline** | 5-stage orchestrator | Pipeline consumer | Classify→Factor→Evolve→ML→Deploy |
| **quant-12-agent-ts** | Web dashboard | Visualization | React/TS UI, factory.db viewer |
| **LobsterQuant** | Order book / microstructure | Data source | Hybrid data loader, massive.com parquet |
| **CrabQuant-agents** | Parallel workers | Infrastructure | Worker clones (dormant) |

---

## Integration Priority Order

### Phase 1: Core Memory + Tools (Week 1-2)

| Priority | Repo | Integration | Effort | Impact |
|----------|------|-------------|--------|--------|
| **P0** | quant-loop-testnet | Memory bridge: LumiBot JSONL → Genome DB | Medium | **Critical** |
| **P0** | CrabQuant | Custom tool: `regime_detect()` | Low | High |
| **P0** | strat-depot | Custom tool: `strategy_history()` | Medium | High |
| **P1** | agentic-quant-os | DuckDB connector for market data | Low | High |
| **P1** | CrabQuant | Custom tool: `arena_backtest()` | Medium | High |

### Phase 2: Pipeline + Signals (Week 3-4)

| Priority | Repo | Integration | Effort | Impact |
|----------|------|-------------|--------|--------|
| **P1** | quant-loop-testnet | Custom tool: `daily_brief()` | Low | Medium |
| **P2** | quant-pipeline | Consume strategy manifest | Low | Medium |
| **P2** | strat-depot | Custom tool: `scan_strategies()` | Medium | Medium |
| **P2** | LobsterQuant | Custom tool: `order_book_depth()` | Medium | Medium |

### Phase 3: Visualization + Advanced (Week 5+)

| Priority | Repo | Integration | Effort | Impact |
|----------|------|-------------|--------|--------|
| **P3** | quant-12-agent-ts | Dashboard for NexusTrade runs | Medium | Low |
| **P3** | agentic-quant-os | LanceDB embeddings for memory search | Medium | Medium |
| **P3** | quant-loop-testnet | Custom tool: `signal_dashboard()` | Medium | Medium |

---

## Detailed Integration Maps

### 1. agentic-quant-os → NexusTrade

**What it does:** Master orchestrator connecting all quant repos via DuckDB + LanceDB lakehouse. 8 connectors, Hermes cron agents, unified data layer.

**Key files:**
- `src/client.py` — DuckDB client (510 LOC)
- `src/schema.py` — 11 tables, 20 indexes
- `src/signal_bus.py` — Pub/sub callbacks
- `src/vector_memory.py` — LanceDB + Gemini embeddings
- `src/bridge.py` — Unified query API
- `src/connectors/` — QRM, alpha-lab, alpha-factory, regime, bella

**Integration points:**

| Component | NexusTrade Use | Integration Method |
|-----------|----------------|-------------------|
| DuckDB client | Market data queries via `duckdb_query` tool | Direct import |
| LanceDB vectors | Embedding-based memory search | Add to search_memory |
| Signal bus | Subscribe to regime changes, new signals | Custom callback |
| Connectors | Read signals from other systems | Query bridge API |

**Code bridge needed:**
```python
# nexus-trade/src/tools/lakehouse_tools.py
from agentic_quant_os.src.client import DuckDBClient
from agentic_quant_os.src.bridge import UnifiedBridge

def get_lakehouse_client():
    """Return connected DuckDB client for market data queries."""
    return DuckDBClient("/home/Zev/development/agentic-quant-os/data/lakehouse.duckdb")

def query_regime_state():
    """Query current regime from lakehouse."""
    bridge = UnifiedBridge()
    return bridge.get_regime_state()
```

**Data available:**
- OHLCV (all sources, centralized)
- Factors (alpha-lab + alpha-factory)
- Signals (cross-system)
- Strategy registry (all repos)
- Backtest results (unified)
- Regime state (10 detectors ensemble)
- Catalyst grades (SEC EDGAR)
- Agent memory (episodes)

---

### 2. CrabQuant → NexusTrade

**What it does:** Core backtest engine with arena harness, regime detection, guardrails, and walk-forward validation. Used by QRM, alpha-factory, strat-depot.

**Key files:**
- `crabquant/engine/backtest.py` — Vectorized backtest (467 LOC)
- `crabquant/regime.py` — 10-detector regime classification (10245 LOC)
- `crabquant/guardrails.py` — Overfit detection, risk checks (6579 LOC)
- `crabquant/strategies/` — 34 strategy implementations
- `crabquant/invention.py` — Strategy generation (6880 LOC)
- `crabquant/validation/` — Walk-forward, Monte Carlo

**Integration points:**

| Component | NexusTrade Use | Integration Method |
|-----------|----------------|-------------------|
| `regime.py` | Custom tool: `regime_detect()` | Import + wrap |
| `engine/backtest.py` | Custom tool: `arena_backtest()` | Import + wrap |
| `guardrails.py` | Custom tool: `preflight_check()` | Import + wrap |
| `strategies/` | AI study material | Read strategy registry |

**Custom LumiBot tools to build:**

```python
# nexus-trade/src/tools/regime_tool.py
from crabquant.regime import RegimeDetector

def regime_detect(symbol: str, lookback: int = 100) -> dict:
    """
    Classify current market regime using CrabQuant's 10-detector ensemble.
    
    Returns:
        {
            "regime": "trending_up" | "trending_down" | "ranging" | "volatile",
            "confidence": 0.85,
            "detectors": {"trend": "up", "volatility": "normal", ...},
            "recommendation": "momentum strategies preferred"
        }
    """
    detector = RegimeDetector()
    # Load data from LumiBot's DuckDB
    # Run classification
    # Return regime + strategy hints
    pass
```

```python
# nexus-trade/src/tools/arena_tool.py
from crabquant.engine.backtest import VectorizedBacktest
from crabquant.guardrails import Guardrails

def arena_backtest(strategy_code: str, symbol: str, start: str, end: str) -> dict:
    """
    Run strategy through arena harness with guardrails.
    
    Returns:
        {
            "passed": True/False,
            "sharpe": 1.2,
            "max_drawdown": -0.15,
            "guardrails": {"overfit_score": 0.3, "regime_stability": 0.8},
            "walk_forward": {"retention": 0.65, "folds_tested": 5}
        }
    """
    # Run vectorized backtest
    # Apply all guardrails
    # Return results
    pass
```

**Guardrails available:**
- Overfitting detection (parameter stability)
- Regime stability checks
- Transaction cost sensitivity
- Look-ahead bias detection
- HODL baseline comparison

---

### 3. quant-loop-testnet → NexusTrade

**What it does:** Strategy factory with conveyor pipeline, genome DB (persistent memory), 6-axis classifier, daily brief, template selector, smart params. **This is the critical integration.**

**Key files:**
- `conveyor/genome_db.py` — SQLite persistent memory (28,348 LOC)
- `conveyor/strategy_classifier.py` — 6-axis gate (37,885 LOC)
- `conveyor/daily_brief.py` — Intelligence brief (10,262 LOC)
- `conveyor/strategy_ingest.py` — Bridge from strat-depot (12,523 LOC)
- `conveyor/strategy_pool.py` — Strategy scanning (15,014 LOC)
- `conveyor/pipeline.py` — 6-station factory orchestrator (18,736 LOC)
- `conveyor/template_selector.py` — Weighted sampling (7,486 LOC)
- `conveyor/smart_params.py` — Historical distributions (6,568 LOC)
- `conveyor/data_adapter.py` — Financial data loading (16,627 LOC)

**Integration points:**

| Component | NexusTrade Use | Integration Method |
|-----------|----------------|-------------------|
| `genome_db.py` | **Memory bridge** — persist all AI decisions | Direct SQLite connection |
| `strategy_classifier.py` | Custom tool: `classify_strategy()` | Import + wrap |
| `daily_brief.py` | Custom tool: `get_brief()` | Import + wrap |
| `strategy_pool.py` | Custom tool: `scan_strategies()` | Import + wrap |
| `template_selector.py` | Strategy recommendations by regime | Import + wrap |
| `smart_params.py` | Parameter initialization from history | Import + wrap |

**CRITICAL: Memory Bridge Architecture**

```python
# nexus-trade/src/memory/genome_bridge.py
import sqlite3
from pathlib import Path
from datetime import datetime

class GenomeBridge:
    """
    Bridge between LumiBot's JSONL memory and Strategy Genome DB.
    
    This is THE KEY INTEGRATION: every AI trading decision, lesson,
    and thesis is persisted to the genome DB for cross-run learning.
    """
    
    def __init__(self, db_path: str = "/home/Zev/development/quant-loop-testnet/conveyor/genome.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
    
    def extract_from_lumibot_memory(self, jsonl_path: str):
        """Extract decisions/lessons from LumiBot's JSONL after each run."""
        # Parse remember_decision entries
        # Parse remember_lesson entries
        # Parse thesis open/update/close
        # Insert into genome_db with cycle_id
        pass
    
    def get_lessons_for_regime(self, regime: str, limit: int = 10) -> list[dict]:
        """Query lessons from similar regimes for system prompt injection."""
        # SELECT FROM strategy_genome WHERE regime=? AND status='winner'
        # ORDER BY score DESC LIMIT ?
        pass
    
    def get_strategy_history(self, strategy_name: str) -> dict:
        """Full lifecycle: discovery → testing → deployment → outcome."""
        # Query all records for this strategy
        # Aggregate results
        pass
    
    def get_regime_strategy_matrix(self) -> dict:
        """Hit rates by regime and strategy template."""
        # Matrix: regime × template → win_rate
        pass
    
    def insert_ai_decision(self, decision: dict):
        """
        Insert an AI trading decision from NexusTrade.
        
        Records:
        - strategy_name (generated by AI)
        - regime (from regime_detect tool)
        - decision details (entry/exit reasoning)
        - outcome (filled after bar closes)
        """
        pass
```

**Genome DB Schema (simplified):**
```sql
CREATE TABLE strategy_genome (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    cycle_id TEXT,
    strategy_name TEXT,
    template TEXT,
    indicators TEXT,  -- JSON
    params TEXT,       -- JSON
    pair TEXT,
    timeframe TEXT,
    regime TEXT,
    status TEXT,       -- 'winner', 'near_miss', 'failure'
    score REAL,
    sharpe REAL,
    total_return REAL,
    max_drawdown REAL,
    discovery_method TEXT,  -- 'llm_discover', 'mutation', etc.
    archetype TEXT,
    gate_decision TEXT,  -- 'PASS', 'CONDITIONAL', 'REJECT'
    -- Plus classifier scores, walk-forward results, etc.
);
```

**Custom tools to build:**

```python
# nexus-trade/src/tools/genome_tools.py
from memory.genome_bridge import GenomeBridge

def strategy_history(strategy_name: str) -> dict:
    """Query Genome DB for strategy's full history."""
    bridge = GenomeBridge()
    return bridge.get_strategy_history(strategy_name)

def scan_strategies(regime: str, limit: int = 20) -> list[dict]:
    """Find strategies that worked in similar regimes."""
    bridge = GenomeBridge()
    return bridge.get_lessons_for_regime(regime, limit)

def get_daily_brief() -> dict:
    """Get intelligence brief from genome DB analysis."""
    from conveyor.daily_brief import DailyBrief
    brief = DailyBrief()
    return brief.generate()
```

---

### 4. strat-depot → NexusTrade

**What it does:** Massive Pine→Python strategy repository (22K files, 7,062 converted, 3,886 usable). Contains RSI-Centered-Pivots — the one walk-forward validated strategy.

**Key files:**
- `strategies/` — 22K Pine Script sources
- `openclaw/results/openclaw-converted/` — 7,062 Python conversions
- `winners/` — 23 curated strategies (manually validated)
- `tools/complexity_classifier.py` — Pine complexity scorer
- `docs/RSI_CENTERED_PIVOTS_DEEP_DIVE.md` — Verified strategy
- `POSTMORTEM.md` — Investigation results (what works, what doesn't)

**Integration points:**

| Component | NexusTrade Use | Integration Method |
|-----------|----------------|-------------------|
| `winners/` | Baseline strategies | Import as LumiBot strategies |
| `openclaw-converted/` | AI study material | Read + classify |
| `RSI-Centered-Pivots` | Baseline comparison | Import as strategy |
| `tools/complexity_classifier.py` | Strategy scoring | Import for filtering |

**Custom tool:**

```python
# nexus-trade/src/tools/strategy_scanner.py
from pathlib import Path

def scan_strategies(min_fidelity: float = 50.0, archetype: str = None) -> list[dict]:
    """
    Scan strat-depot for strategies matching criteria.
    
    Returns:
        [
            {
                "name": "rsi_centered_pivots",
                "path": "/home/Zev/development/strat-depot/winners/rsi_centered_pivots.py",
                "fidelity": 95.0,
                "archetype": "mean_reversion",
                "indicators": ["RSI", "Pivot"],
                "verified": True
            },
            ...
        ]
    """
    # Scan winners/ first (verified strategies)
    # Then openclaw/results/openclaw-converted/
    # Filter by fidelity score
    # Classify by archetype
    pass
```

**Key insight from POSTMORTEM.md:**
> "736 strategies were backtested, top candidates deep-validated with walk-forward across 525 tickers. **The result: only RSI-Centered-Pivots survived full walk-forward validation.**"

This becomes the **baseline strategy** for NexusTrade. AI-generated strategies must beat RSI-Centered-Pivots in walk-forward to be considered for live deployment.

---

### 5. quant-pipeline → NexusTrade

**What it does:** 5-stage orchestrator that chains Classify → Factor Score → Evolve → ML Filter → Deploy. Consumes from strat-depot, alpha-factory, quant-loop-testnet.

**Key files:**
- `unified_pipeline.py` — Main orchestrator (19,518 LOC)
- `strategy_classifier.py` — Stage 1: Classify (19,518 LOC)
- `factor_batch_scorer.py` — Stage 2: Factor scoring (13,240 LOC)
- `evolution_feeder.py` — Stage 3: Feed evolution (13,240 LOC)
- `ml_filter.py` — Stage 4: ML filter (12,738 LOC)
- `deployment_gate.py` — Stage 5: Deploy (10,752 LOC)
- `config.yaml` — All paths and thresholds

**Integration points:**

| Component | NexusTrade Use | Integration Method |
|-----------|----------------|-------------------|
| `strategy_manifest.json` | Pipeline output | Read classified strategies |
| `factor_scores.json` | Pipeline output | Read factor scores |
| `ml_filtered_signals.json` | Pipeline output | Read ML-filtered signals |
| `unified_pipeline.py` | Pipeline status | Query `--status` |

**NexusTrade as consumer:**

```python
# nexus-trade/src/tools/pipeline_tools.py
import json
from pathlib import Path

PIPELINE_OUTPUT = Path("/home/Zev/development/quant-pipeline/output/")

def get_pipeline_status() -> dict:
    """Get current pipeline status and stage."""
    # Read config.yaml for paths
    # Check which JSON files exist
    # Return last run times and counts
    pass

def get_approved_signals() -> list[dict]:
    """Get strategies that passed ML filter."""
    signals_file = PIPELINE_OUTPUT / "ml_filtered_signals.json"
    if signals_file.exists():
        return json.loads(signals_file.read_text())
    return []
```

---

### 6. quant-12-agent-ts → NexusTrade

**What it does:** React/TypeScript dashboard + Node.js server for viewing QRM results. Viewer/control layer, not core quant logic.

**Key files:**
- `server.ts` — Node.js backend (26,167 LOC)
- `src/` — React frontend
- `factory.db` — SQLite with strategy results

**Integration points:**

| Component | NexusTrade Use | Integration Method |
|-----------|----------------|-------------------|
| React UI | Dashboard for NexusTrade runs | Extend or fork |
| factory.db | Read strategy results | SQLite connection |

**Low priority** — This is a visualization layer. Better to build NexusTrade-specific dashboard or extend the agentic-quant-os Streamlit dashboard.

---

### 7. LobsterQuant → NexusTrade

**What it does:** Order book / microstructure data platform. Hybrid data loader: yfinance base + massive.com parquet overlay.

**Key files:**
- `lobsterquant/data/__init__.py` — Hybrid data loader
- `VISION.md` — Project north star

**Integration points:**

| Component | NexusTrade Use | Integration Method |
|-----------|----------------|-------------------|
| Hybrid loader | High-quality OHLCV | Import for data loading |
| massive.com parquet | TIER 1 data (real VWAP, n_trades) | Read directly |

**Custom tool (future):**

```python
# nexus-trade/src/tools/order_book.py
def order_book_depth(symbol: str) -> dict:
    """
    Get order book depth from LobsterQuant.
    
    Returns:
        {
            "bid_depth": [...],
            "ask_depth": [...],
            "spread": 0.01,
            "imbalance": 0.15
        }
    """
    # Requires LobsterQuant order book data
    pass
```

**Note:** This requires live/paper trading. Not Phase 1.

---

### 8. CrabQuant-agents → NexusTrade

**What it does:** Empty worker clones for parallel execution. Never ran. **Dormant.**

**Status:** Skip — no data, no code.

---

## Dependency Graph

```
                    ┌────────────────────┐
                    │  agentic-quant-os  │
                    │  (DuckDB lakehouse)│
                    └──────────┬─────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                  │
            ▼                  ▼                  ▼
    ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
    │ quant-loop-   │  │   CrabQuant   │  │  strat-depot  │
    │ testnet       │  │   (engine)    │  │  (source)     │
    │ (memory)      │  │               │  │               │
    └───────┬───────┘  └───────┬───────┘  └───────┬───────┘
            │                  │                  │
            └──────────────────┼──────────────────┘
                               │
                               ▼
                    ┌────────────────────┐
                    │    NexusTrade      │
                    │  (LumiBot harness) │
                    │                    │
                    │ ┌────────────────┐ │
                    │ │ Custom Tools   │ │
                    │ │ - regime_detect│ │
                    │ │ - arena_test   │ │
                    │ │ - strategy_hist│ │
                    │ │ - daily_brief  │ │
                    │ └────────────────┘ │
                    │                    │
                    │ ┌────────────────┐ │
                    │ │ Memory Bridge  │ │
                    │ │ LumiBot JSONL  │ │
                    │ │      ↓         │ │
                    │ │   Genome DB    │ │
                    │ └────────────────┘ │
                    └────────────────────┘
                               │
                    ┌──────────┴─────────┐
                    │                    │
                    ▼                    ▼
            ┌───────────────┐    ┌───────────────┐
            │ quant-pipeline│    │quant-12-agent│
            │ (orchestrator)│    │(dashboard)   │
            └───────────────┘    └───────────────┘
```

---

## Code Snippets / Interfaces

### Memory Bridge (Critical Integration)

```python
# nexus-trade/src/memory/genome_bridge.py

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

class GenomeBridge:
    """
    Persistent memory bridge between LumiBot and Strategy Genome DB.
    
    Every AI trading decision is persisted for cross-run learning.
    """
    
    GENOME_DB = "/home/Zev/development/quant-loop-testnet/conveyor/genome.db"
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or self.GENOME_DB
        self.conn = None
    
    def connect(self):
        """Connect to genome DB."""
        self.conn = sqlite3.connect(self.db_path)
        return self
    
    def close(self):
        """Close connection."""
        if self.conn:
            self.conn.close()
    
    def insert_decision(self, decision: dict) -> int:
        """
        Insert an AI trading decision.
        
        Args:
            decision: {
                "strategy_name": str,
                "cycle_id": str,
                "pair": str,
                "timeframe": str,
                "regime": str,
                "entry_reasoning": str,
                "exit_reasoning": str,
                "params": dict,
                "indicators": list
            }
        
        Returns:
            record_id
        """
        # Implementation
        pass
    
    def get_regime_winners(self, regime: str, limit: int = 20) -> list[dict]:
        """
        Get strategies that won in this regime.
        
        Used for system prompt injection: "In trending_up regimes,
        RSI-Centered-Pivots has 85% retention across 525 tickers..."
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT strategy_name, sharpe, total_return, params, indicators
            FROM strategy_genome
            WHERE regime = ? AND status = 'winner'
            ORDER BY score DESC
            LIMIT ?
        """, (regime, limit))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def get_near_misses(self, regime: str) -> list[dict]:
        """
        Get near-miss strategies for this regime.
        
        Useful for targeted mutation: "RSI-Bollinger failed by 2%
        in trending_up... try adding volume filter."
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT strategy_name, score, failure_category, failure_detail
            FROM strategy_genome
            WHERE regime = ? AND status = 'near_miss'
            ORDER BY score DESC
        """, (regime,))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def update_outcome(self, record_id: int, outcome: dict):
        """
        Update decision with actual outcome after bar closes.
        
        Args:
            outcome: {
                "status": "winner" | "near_miss" | "failure",
                "sharpe": float,
                "total_return": float,
                "max_drawdown": float,
                "num_trades": int
            }
        """
        # Implementation
        pass


# LumiBot integration hook
def on_backtest_complete(lumibot_memory_jsonl: str):
    """
    Hook called after each LumiBot backtest run.
    
    Extracts decisions/lessons from JSONL and persists to Genome DB.
    """
    bridge = GenomeBridge()
    bridge.connect()
    
    # Parse JSONL
    for line in Path(lumibot_memory_jsonl).read_text().strip().split('\n'):
        entry = json.loads(line)
        
        if entry.get('type') == 'decision':
            bridge.insert_decision(entry)
        elif entry.get('type') == 'lesson':
            bridge.insert_lesson(entry)
    
    bridge.close()
```

### Custom Tool Wrapper Pattern

```python
# nexus-trade/src/tools/wrapper.py

"""
Pattern for wrapping existing repos as LumiBot tools.

All custom tools must:
1. Be point-in-time safe (no future data in backtests)
2. Return structured dicts (not strings)
3. Log all calls for replay
4. Handle errors gracefully
"""

from lumibot.tools import tool

@tool
def regime_detect(symbol: str, lookback: int = 100) -> dict:
    """
    Classify current market regime using CrabQuant's detector ensemble.
    
    This tool is point-in-time safe: it only uses data available
    at the current bar in backtest mode.
    
    Args:
        symbol: Trading symbol (e.g., 'AAPL', 'BTC-USD')
        lookback: Bars to analyze (default 100)
    
    Returns:
        {
            "regime": "trending_up" | "trending_down" | "ranging" | "volatile",
            "confidence": 0.0-1.0,
            "detectors": {
                "trend": "up" | "down" | "sideways",
                "volatility": "low" | "normal" | "high",
                "momentum": "strong" | "weak" | "divergent"
            },
            "recommendation": "momentum strategies preferred",
            "similar_strategies": ["rsi_centered_pivots", "bb_stoch_macd"]
        }
    """
    # Import from CrabQuant
    from crabquant.regime import RegimeDetector
    from memory.genome_bridge import GenomeBridge
    
    # Get regime from CrabQuant
    detector = RegimeDetector()
    regime_result = detector.classify(symbol, lookback)
    
    # Get similar strategies from Genome DB
    bridge = GenomeBridge()
    bridge.connect()
    similar = bridge.get_regime_winners(regime_result['regime'])
    bridge.close()
    
    return {
        **regime_result,
        "similar_strategies": [s['strategy_name'] for s in similar[:5]]
    }
```

---

## Recommended Implementation Order

### Week 1: Memory Bridge (Critical Path)

1. **Create `nexus-trade/src/memory/genome_bridge.py`**
   - Connect to Genome DB
   - Implement `insert_decision()`
   - Implement `get_regime_winners()`
   - Implement `update_outcome()`

2. **Create LumiBot hook**
   - After each backtest, extract JSONL memory
   - Parse decisions/lessons
   - Persist to Genome DB

3. **Test memory bridge**
   - Run simple backtest with AI
   - Verify decisions appear in Genome DB
   - Query winners for regime

### Week 2: Custom Tools

1. **`regime_detect()`**
   - Import CrabQuant's regime detector
   - Wrap as LumiBot tool
   - Add similar strategy lookup

2. **`strategy_history()`**
   - Query Genome DB
   - Return full lifecycle

3. **`arena_backtest()`**
   - Import CrabQuant's arena harness
   - Run strategy through guardrails
   - Return pass/fail + scores

### Week 3: Data Integration

1. **Connect to agentic-quant-os lakehouse**
   - Import DuckDB client
   - Add `query_lakehouse()` tool

2. **Connect to strat-depot**
   - Add `scan_strategies()` tool
   - Import RSI-Centered-Pivots as baseline

### Week 4: Testing + Iteration

1. **A/B test: AI with vs without memory**
   - Same strategy, same period
   - Measure improvement from memory bridge

2. **Model comparison**
   - GLM-5 vs DeepSeek-V4 vs Gemini
   - Which makes better decisions with same tools?

3. **Refine system prompts**
   - Inject lessons from Genome DB
   - Test different prompt strategies

---

## Success Metrics

| Metric | Baseline | Target | How to Measure |
|--------|----------|--------|----------------|
| Cross-run memory retention | 0% (fresh each run) | 100% (all decisions persisted) | Genome DB record count |
| Regime-aware decisions | 0% (no regime tool) | 100% (always query regime) | Tool call logs |
| Strategy improvement | RSI-Centered-Pivots baseline | Beat baseline in WF | Arena harness results |
| Memory-based improvements | 0% | Measurable Sharpe improvement | A/B test results |

---

## Appendix: File Paths

| Repo | Key Path | Purpose |
|------|----------|---------|
| agentic-quant-os | `/home/Zev/development/agentic-quant-os/data/lakehouse.duckdb` | DuckDB lakehouse |
| quant-loop-testnet | `/home/Zev/development/quant-loop-testnet/conveyor/genome.db` | Strategy Genome DB |
| CrabQuant | `/home/Zev/development/CrabQuant/crabquant/regime.py` | Regime detector |
| CrabQuant | `/home/Zev/development/CrabQuant/crabquant/engine/backtest.py` | Arena harness |
| strat-depot | `/home/Zev/development/strat-depot/winners/` | Curated strategies |
| strat-depot | `/home/Zev/development/strat-depot/openclaw/results/openclaw-converted/` | Converted strategies |
| quant-pipeline | `/home/Zev/development/quant-pipeline/output/` | Pipeline outputs |
| NexusTrade | `/home/Zev/development/nexus-trade/src/memory/` | Memory bridge (to build) |
| NexusTrade | `/home/Zev/development/nexus-trade/src/tools/` | Custom tools (to build) |

---

*This integration map is the roadmap for building NexusTrade. Start with the memory bridge (Week 1), then add custom tools (Week 2), then integrate data sources (Week 3), then test and iterate (Week 4).*