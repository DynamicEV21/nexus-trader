# Graph Report - .  (2026-06-08)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 155 nodes · 216 edges · 18 communities (10 shown, 8 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 8 edges (avg confidence: 0.64)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `7d3236d5`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Vector Memory Management|Vector Memory Management]]
- [[_COMMUNITY_Agent Strategy Prompts|Agent Strategy Prompts]]
- [[_COMMUNITY_Memory Data Synchronization|Memory Data Synchronization]]
- [[_COMMUNITY_Project Documentation|Project Documentation]]
- [[_COMMUNITY_Trade Memory Tools|Trade Memory Tools]]
- [[_COMMUNITY_Memory Data Schema|Memory Data Schema]]
- [[_COMMUNITY_System Infrastructure|System Infrastructure]]
- [[_COMMUNITY_Technical Indicator Tools|Technical Indicator Tools]]
- [[_COMMUNITY_Market Regime Analysis|Market Regime Analysis]]
- [[_COMMUNITY_User Identity|User Identity]]
- [[_COMMUNITY_Workspace State|Workspace State]]
- [[_COMMUNITY_System Logs|System Logs]]
- [[_COMMUNITY_Memory Subsystem|Memory Subsystem]]
- [[_COMMUNITY_Platform Core|Platform Core]]
- [[_COMMUNITY_Trading Strategies|Trading Strategies]]
- [[_COMMUNITY_Agent Tool Implementations|Agent Tool Implementations]]
- [[_COMMUNITY_Strategy Database|Strategy Database]]
- [[_COMMUNITY_Local Tool Notes|Local Tool Notes]]

## God Nodes (most connected - your core abstractions)
1. `NexusVectorMemory` - 20 edges
2. `MemoryBridge` - 16 edges
3. `Nexus Committee Strategy` - 15 edges
4. `get_nexus_memory()` - 10 edges
5. `NexusVectorMemory` - 8 edges
6. `Any` - 8 edges
7. `Nexus Trader` - 7 edges
8. `get_memory_stats_tool()` - 6 edges
9. `LumiBot v4.5.25` - 6 edges
10. `NexusDecisionRecord` - 5 edges

## Surprising Connections (you probably didn't know these)
- `Multi-agent investment committee with persistent vector memory.      Parameters` --rationale_for--> `Nexus Committee Strategy`  [EXTRACTED]
  src/strategies/nexus_committee.py → AGENTS.md
- `Nexus Trader` --shares_data_with--> `Strategy Genome DB`  [INFERRED]
  AGENTS.md → docs/INTEGRATION_MAP.md
- `Financial Data Research` --references--> `LumiBot v4.5.25`  [INFERRED]
  docs/research_financial_data.md → README.md
- `Any` --uses--> `MemoryBridge`  [INFERRED]
  src/strategies/nexus_committee.py → src/memory/bridge.py
- `Nexus Trader` --conceptually_related_to--> `agentic-quant-os`  [EXTRACTED]
  AGENTS.md → docs/INTEGRATION_MAP.md

## Import Cycles
- 1-file cycle: `src/memory/bridge.py -> src/memory/bridge.py`

## Communities (18 total, 8 thin omitted)

### Community 0 - "Vector Memory Management"
Cohesion: 0.12
Nodes (17): NexusVectorMemory, Nexus Trader-specific vector memory backed by LanceDB + local Qwen3 embeddings., Open (or create) the LanceDB connection and both tables., Load the SentenceTransformer model (one-time, lazy)., Return the actual embedding dimension of the loaded model., Embed a single text string using the local model., Embed multiple texts using the local model in batches., Construct a human-readable decision DNA string for embedding.          Format:: (+9 more)

### Community 1 - "Agent Strategy Prompts"
Cohesion: 0.14
Nodes (11): Gemini 3 Flash, GLM-5 Turbo, Nexus Committee Strategy, Any, Set up the committee agents, tools, and state., Register custom @agent_tools so agents can call them., Sync LumiBot JSONL memory into vector memory (non-blocking, best-effort)., Run a full investment committee session. (+3 more)

### Community 2 - "Memory Data Synchronization"
Cohesion: 0.22
Nodes (10): MemoryBridge, Deduplicate a list of entries by their ``id`` field.          Parameters, Parse decisions.jsonl and bridge to vector memory.          Maps LumiBot decisio, Parse lessons.jsonl and bridge to vector memory.          Returns         ------, Parse theses.jsonl and store as lessons (category='thesis').          LumiBot th, Parse memories.jsonl and store as lessons (category='insight').          Returns, Run all sync operations — decisions, lessons, theses, memories.          Returns, Bridges LumiBot JSONL memory files to Nexus Trader vector memory.      Parameter (+2 more)

### Community 3 - "Project Documentation"
Cohesion: 0.16
Nodes (14): CrabQuant, DeepSeek-V4, Gemini-3FP, GLM-5, LumiBot v4.5.25, Execution Roadmap, LumiBot Research, Model Selection Guide (+6 more)

### Community 4 - "Trade Memory Tools"
Cohesion: 0.24
Nodes (12): get_nexus_memory(), Return the process-wide :class:`NexusVectorMemory` singleton.      The instance, Any, get_memory_stats_tool(), query_trade_memory_tool(), Trade Memory Tool — LumiBot @agent_tool for semantic memory search =============, Store the current trade decision in persistent vector memory.      Records the d, Store a lesson learned in persistent vector memory.      Captures trading insigh (+4 more)

### Community 5 - "Memory Data Schema"
Cohesion: 0.18
Nodes (9): LanceModel, Memory Bridge — LumiBot JSONL ↔ Nexus Trader Vector Memory =====================, Lazy-initialized NexusVectorMemory instance., NexusDecisionRecord, NexusLessonRecord, Nexus Trader Vector Memory — LanceDB-backed persistent trade memory ============, LanceDB row schema for a Nexus Trader trade decision., LanceDB row schema for a lesson learned from trading experience. (+1 more)

### Community 6 - "System Infrastructure"
Cohesion: 0.20
Nodes (11): DuckDB Time Wall, Nexus Trader, LumiBot Replay Cache, Memory & Thesis System, agentic-quant-os, CrabQuant, Strategy Genome DB, quant-loop-testnet (+3 more)

### Community 7 - "Technical Indicator Tools"
Cohesion: 0.18
Nodes (10): Any, _compute_atr(), _compute_macd(), _compute_rsi(), Signal Dashboard Tool — Pre-computed indicator snapshot in one call ============, Compute RSI (Relative Strength Index)., Compute MACD line, signal line, and histogram., Compute Average True Range. (+2 more)

### Community 8 - "Market Regime Analysis"
Cohesion: 0.29
Nodes (5): Any, Nexus Trader Investment Committee — Multi-Agent Strategy with Vector Memory ====, detect_regime_tool(), Regime Detection Tool — LumiBot @agent_tool for CrabQuant regime detection =====, Detect the current market regime using SPY + VIX data.      Calls CrabQuant's ``

### Community 9 - "User Identity"
Cohesion: 0.50
Nodes (4): Bootstrap, Identity, Soul, User

## Knowledge Gaps
- **28 isolated node(s):** `version`, `bootstrapSeededAt`, `Any`, `Any`, `LumiBot Replay Cache` (+23 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `MemoryBridge` connect `Memory Data Synchronization` to `Vector Memory Management`, `Agent Strategy Prompts`, `Memory Data Schema`, `Market Regime Analysis`?**
  _High betweenness centrality (0.205) - this node is a cross-community bridge._
- **Why does `NexusVectorMemory` connect `Vector Memory Management` to `Memory Data Synchronization`, `Trade Memory Tools`, `Memory Data Schema`?**
  _High betweenness centrality (0.204) - this node is a cross-community bridge._
- **Why does `Nexus Committee Strategy` connect `Agent Strategy Prompts` to `Market Regime Analysis`, `Memory Data Synchronization`?**
  _High betweenness centrality (0.102) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `NexusVectorMemory` (e.g. with `MemoryBridge` and `NexusVectorMemory`) actually correct?**
  _`NexusVectorMemory` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `MemoryBridge` (e.g. with `Nexus Committee Strategy` and `NexusVectorMemory`) actually correct?**
  _`MemoryBridge` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `version`, `bootstrapSeededAt`, `Nexus Trader — Multi-agent investment platform with persistent vector memory.` to the rest of the system?**
  _81 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Vector Memory Management` be split into smaller, more focused modules?**
  _Cohesion score 0.11827956989247312 - nodes in this community are weakly interconnected._