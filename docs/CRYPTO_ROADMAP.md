# Nexus Trader — Crypto Roadmap

> **Version:** 2.0  
> **Created:** 2026-06-14  
> **Focus:** CRYPTO-FIRST (BTC/ETH/SOL)  
> **Stock support:** Deferred to separate track  

---

## What Changed From v1

The original roadmap (EXECUTION_ROADMAP.md) was stock-focused (S&P 500, Yahoo data). We're pivoting to crypto because:
1. **StratForge strategies are already crypto-tested** (BTC/ETH/SOL daily, CrabQuant engine)
2. **LumiBot fully supports crypto backtesting** via `CcxtBacktesting` (CCXT/Binance, no API keys needed)
3. **Crypto trades 24/7** — no overnight gaps, cleaner signal-to-noise for indicator strategies
4. **We have 5 years of test data** already at `strat-depot/test_data/{BTC,ETH,SOL}_5yr.parquet`

The stock version will be built later as a separate configuration — same adapter, different data source.

---

## System Architecture (Crypto Mode)

```
┌─────────────────────────────────────────────────────────┐
│                    NEXUS TRADER (CRYPTO)                   │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐ │
│  │            StratForgeSignalAdapter                    │ │
│  │  Wraps any strategy(df, params) → (entries, exits)   │ │
│  │  as a LumiBot Strategy class for CcxtBacktesting     │ │
│  └──────────────────────┬──────────────────────────────┘ │
│                         │                                 │
│  ┌──────────────────────┴──────────────────────────────┐ │
│  │              LumiBot v4.5.25 Engine                   │ │
│  │  CcxtBacktesting | CCXT Broker | DuckDB OHLCV cache  │ │
│  │  Point-in-time safety | Replay cache | Tearsheets     │ │
│  └──────────────────────┬──────────────────────────────┘ │
│                         │                                 │
│  ┌──────────────────────┴──────────────────────────────┐ │
│  │                 Data Sources                          │ │
│  │  CCXT/Binance (public, no keys) → BTC/USDT, ETH/USDT │ │
│  │  strat-depot/test_data/{BTC,ETH,SOL}_5yr.parquet     │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐ │
│  │            StratForge DuckDB (read/write)             │ │
│  │  Path: ~/.hermes/profiles/herm-bot/home/              │ │
│  │         agentic-quant-os/data/quant.duckdb           │ │
│  │  Tables: backtest_results_v2 (1,344 strategies)      │ │
│  │  New: nexus_backtest_results (LumiBot runs)          │ │
│  └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

---

## Key Facts (Verified 2026-06-14)

### StratForge State
- **1,344 strategies** total: 41 winner, 400 tested, 396 potential, 271 discarded, 236 pruned
- **Only 1 winner actually passed walk-forward**: `trix_chop_multi_indicator` (comp 63.85, wf_avg_sharpe 0.237)
- 38 of 39 "winners" carry the label by composite_score alone — NOT out-of-sample validated
- **100% of winners have source_code** in DuckDB (avg 4,233 chars)
- Strategy contract: `def strategy(df, params=None) → (pd.Series entries, pd.Series exits)` where each value is 0 or 1
- Top by wf_avg_sharpe: `elder_klinger_ultosc_best` (0.688, comp 77.05), `regime_connors_vwapkc` (0.555, comp 68.73)

### LumiBot Crypto Support
- `CcxtBacktesting` data source uses CCXT public API (Binance default), cached in local DuckDB
- `Asset("BTC", "crypto")` + `Asset("USDT", "crypto")` as base/quote pair
- CCXT v4.5.56 installed, 111 exchanges available
- **No API keys needed for backtesting** — public OHLCV endpoints
- Live trading via `Ccxt` broker (needs exchange API keys: Binance/Kraken/Coinbase)

### Memory Systems (3 layers)
1. **JSONL** (LumiBot built-in): `.lumibot/memory/<strategy>/` — durable append-only log
2. **Runtime notes** (agent manager): 20 most recent notes injected into LLM prompt per iteration
3. **LanceDB vectors** (Nexus): `nexus_decisions` + `nexus_lessons` tables, Qwen3-Embedding-0.6B (local, 1024-dim)

### Database Landscape
- **StratForge DB**: `~/.hermes/profiles/herm-bot/home/agentic-quant-os/data/quant.duckdb` (117MB)
- **Nexus/aqos DB**: `~/development/agentic-quant-os/data/quant.duckdb` (2.5GB) — different file, different schema
- **Approach**: Read StratForge DB directly (DuckDB read-only connection). Write LumiBot results to a new `nexus_lumibot_results` table in the same DB.

---

## Phase 0: Build & Verify Adapter (Day 1)

**Goal:** Get a single StratForge strategy running through LumiBot CcxtBacktesting on BTC.

### Step 0.1: Create StratForgeSignalAdapter

The adapter wraps any `strategy(df, params) → (entries, exits)` function as a LumiBot `Strategy`:

```python
# src/strategies/stratforge_adapter.py

class StratForgeSignalAdapter(Strategy):
    """
    Wraps a StratForge strategy(df, params) → (entries, exits) function
    as a LumiBot Strategy for crypto backtesting via CcxtBacktesting.
    """
    parameters = {
        "strategy_fn": None,       # callable: strategy(df, params) → (entries, exits)
        "strategy_params": {},      # dict of params
        "symbol": "BTC",           # base asset
        "quote": "USDT",           # quote asset
        "position_size": 0.95,     # fraction of portfolio per trade
    }

    def initialize(self):
        self.sleeptime = "1D"
        self.signals = None  # pre-computed on first iteration
        self.bar_idx = 0

    def on_trading_iteration(self):
        if self.signals is None:
            df = self._build_ohlcv_df()
            entries, exits = self.parameters["strategy_fn"](df, self.parameters["strategy_params"])
            self.signals = {"entries": entries, "exits": exits}

        idx = self.bar_idx
        entries = self.signals["entries"]
        exits = self.signals["exits"]
        symbol = self.parameters["symbol"]
        quote = self.parameters["quote"]

        # Entry
        if idx < len(entries) and entries.iloc[idx] == 1:
            pos = self.get_position(symbol)
            if not pos or pos.quantity == 0:
                price = self.get_last_price(self.symbol_asset)
                qty = (self.portfolio_value * self.parameters["position_size"]) / price
                if qty > 0:
                    order = self.create_order(self.symbol_asset, qty, "buy", quote=self.quote_asset)
                    self.submit_order(order)

        # Exit
        elif idx < len(exits) and exits.iloc[idx] == 1:
            pos = self.get_position(symbol)
            if pos and pos.quantity > 0:
                self.sell_all(symbol, quote=self.quote_asset)

        self.bar_idx += 1

    def _build_ohlcv_df(self):
        """Get historical OHLCV as a DataFrame matching StratForge's expected format."""
        bars = self.get_historical_prices(
            self.symbol_asset, 2000, "day", quote=self.quote_asset
        )
        df = bars.df
        # StratForge expects lowercase columns: open, high, low, close, volume
        return df
```

### Step 0.2: Run First Backtest

```python
from lumibot.backtesting import CcxtBacktesting
from lumibot.entities import Asset
from datetime import datetime

# Load a StratForge winner
import sys
sys.path.insert(0, "~/.hermes/profiles/herm-bot/home/agentic-quant-os/strategies/stratforge/active")
from connors_rsi_starc_v2 import strategy_connors_rsi_starc_v2

# Run backtest
result = StratForgeSignalAdapter.backtest(
    CcxtBacktesting,
    backtesting_start=datetime(2021, 6, 1),
    backtesting_end=datetime(2024, 12, 31),
    benchmark_asset=Asset("BTC", "crypto"),
    buy_trading_fees=[TradingFee(percent_fee=0.001)],
    sell_trading_fees=[TradingFee(percent_fee=0.001)],
    quote_asset=Asset("USDT", "crypto"),
    budget=10000,
    name="sf_connors_rsi_btc_v1",
    parameters={
        "strategy_fn": strategy_connors_rsi_starc_v2,
        "strategy_params": {},
        "symbol": "BTC",
        "quote": "USDT",
    },
)
```

### Step 0.3: Verify Results

- Backtest completes without errors
- Tearsheet generated (LumiBot auto-generates with `save_tearsheet=True`)
- Compare LumiBot metrics to CrabQuant metrics for the same strategy/asset
- Log any discrepancies (different fill assumptions, commission handling)

**Gate:** First backtest completes and produces sane metrics. Move to Phase 1.

---

## Phase 1: Batch Strategy Screen (Day 2-3)

**Goal:** Run ALL 39 StratForge winners through LumiBot on BTC/ETH/SOL. Find which ones survive LumiBot's execution model.

### Step 1.1: Strategy Loader

Create a loader that reads source_code from the StratForge DuckDB and dynamically imports the strategy function:

```python
def load_strategy_from_db(strategy_name):
    """Load a strategy function from the StratForge lakehouse."""
    con = duckdb.connect(SF_DB_PATH, read_only=True)
    row = con.execute("""
        SELECT source_code FROM backtest_results_v2
        WHERE strategy_name = ? AND status = 'winner'
        AND (is_best_version = true OR is_best_version IS NULL)
        AND source_code IS NOT NULL AND LENGTH(source_code) > 100
    """, [strategy_name]).fetchone()
    con.close()

    if not row:
        return None

    code = row[0]
    # Execute in isolated namespace
    ns = {}
    exec(code, ns)

    # Find the strategy function (named 'strategy' or 'strategy_<name>')
    for name in ['strategy', f'strategy_{strategy_name}']:
        if name in ns and callable(ns[name]):
            return ns[name]
    return None
```

### Step 1.2: Batch Runner

Run each strategy on BTC, ETH, SOL. Store results to a new table:

```sql
CREATE TABLE IF NOT EXISTS nexus_lumibot_results (
    id INTEGER PRIMARY KEY DEFAULT nextval('nexus_seq'),
    strategy_name VARCHAR,
    symbol VARCHAR,
    backtest_start DATE,
    backtest_end DATE,
    total_return DOUBLE,
    max_drawdown DOUBLE,
    sharpe DOUBLE,
    sortino DOUBLE,
    vol DOUBLE,
    num_trades INTEGER,
    win_rate DOUBLE,
    profit_factor DOUBLE,
    calmar DOUBLE,
    source_composite DOUBLE,   -- original StratForge composite_score
    source_wf_pass BOOLEAN,    -- original walk-forward pass
    lumibot_version VARCHAR,   -- LumiBot version
    created_at TIMESTAMP DEFAULT current_timestamp
);
```

### Step 1.3: Results Analysis

For each strategy, compare:
- **LumiBot return vs CrabQuant return** — large gaps = fill/commission mismatch
- **LumiBot trade count vs CrabQuant trade count** — gaps = signal timing differences
- **Cross-asset consistency** — does the strategy work on all 3 or just 1?

**Gate:** At least 5 strategies show positive risk-adjusted returns on ≥2 assets via LumiBot. If zero survive, investigate fill assumptions before proceeding.

---

## Phase 2: Walk-Forward Validation in LumiBot (Day 3-4)

**Goal:** Validate Phase 1 survivors out-of-sample using LumiBot's event-driven engine.

### Step 2.1: Train/Test Split

- Train: 2021-06-01 → 2023-12-31
- Test: 2024-01-01 → 2025-12-31
- Run each survivor on both periods, compare metrics

### Step 2.2: Expanding Window (Optional)

For top-5 survivors, run LumiBot backtest on 5 expanding windows (matching StratForge's WF methodology). This is slower (event-driven) but catches regime sensitivity that vectorized WF might miss.

**Gate:** At least 2 strategies show positive test-period returns. These are deployment candidates.

---

## Phase 3: Database Integration (Day 4)

**Goal:** Connect Nexus Trader to the StratForge DuckDB for bidirectional read/write.

### Step 3.1: Read Path (StratForge → Nexus)

Add a `StratForgeReader` class that connects read-only to the StratForge DB:

```python
class StratForgeReader:
    DB_PATH = "~/.hermes/profiles/herm-bot/home/agentic-quant-os/data/quant.duckdb"

    def get_top_strategies(self, min_composite=50, limit=50):
        """Get top strategies by composite score."""
        ...

    def get_strategy_source(self, strategy_name):
        """Get source code for a strategy."""
        ...

    def get_strategy_params(self, strategy_name):
        """Get params_json for a strategy."""
        ...
```

### Step 3.2: Write Path (Nexus → StratForge DB)

Write LumiBot backtest results to `nexus_lumibot_results` table. This lets StratForge meta-analysis queries include LumiBot results.

### Step 3.3: Write Lessons Back

When LumiBot identifies a strategy failure (e.g., works on BTC but not ETH), write a lesson to the `component_feedback` or a new `nexus_lessons` table so future StratForge iterations can learn from it.

---

## Phase 4: Paper Trading (Day 5+)

**Goal:** Deploy validated strategies to live paper trading via CCXT.

### Step 4.1: Configure CCXT Paper Broker

- Binance testnet or Kraken sandbox (no real funds)
- Or use a mock broker that tracks real prices without executing

### Step 4.2: Deploy Signal Monitors

Run the StratForgeSignalAdapter in live mode, monitoring real BTC/ETH/SOL prices. The adapter generates signals; we track hypothetical P&L.

### Step 4.3: Compare Live Signals to Backtest

Do the signals fire at the same times the backtest predicted? If not, investigate data feed differences (LumiBot CCXT live vs cached).

**Gate:** Live signals match backtest signal timing within 1 bar for ≥80% of signals over a 2-week window.

---

## Phase 5: LLM Committee Integration (Week 2+)

**Goal:** Feed StratForge signals as context to the LLM investment committee.

### Step 5.1: Signal Dashboard Tool

Add a `@agent_tool` that returns current signal states from active StratForge strategies:

```python
@agent_tool(name="stratforge_signals")
def stratforge_signals(self, symbol="BTC") -> dict:
    """Get current entry/exit signals from validated StratForge strategies."""
    # Returns: {"strategies": [{"name": "...", "signal": "ENTRY", "confidence": 0.7}]}
```

### Step 5.2: Committee Architecture (Crypto)

```
Evidence Researcher (Gemini Flash)
  → pulls StratForge signals + market data
Bull Researcher (Gemini Flash)
  → argues for entries
Bear Researcher (Gemini Flash)
  → argues against
Portfolio Manager (GLM-5 Turbo)
  → makes final decision, places orders via CCXT
```

### Step 5.3: Backtest the Committee

Run the full 4-agent committee via CcxtBacktesting on BTC/ETH/SOL. Compare committee P&L to pure signal P&L. Does the LLM add value or just add noise?

**Gate:** Committee Sharpe ≥ pure-signal Sharpe - 0.2 (i.e., committee doesn't destroy alpha).

---

## Phase 6: Live Trading (Future)

- Fund a Binance/Kraken account with small allocation
- Deploy committee with strict position limits
- Monitor daily, compare live to backtest expectations

---

## Architecture Decisions

### 1. Single Adapter, Multiple Modes

The `StratForgeSignalAdapter` is one class. To switch between crypto and stocks:
- **Crypto**: `CcxtBacktesting` + `Asset("BTC", "crypto")` + `Asset("USDT", "crypto")`
- **Stocks** (later): `YahooDataBacktesting` + `Asset("AAPL")` + `Asset("USD", "forex")`

No code changes to the adapter — just different parameters and data source.

### 2. No Database Merge

We read from the StratForge DB (read-only DuckDB connection) and write to a new table in the same DB. The 2.5GB agentic-quant-os DB stays separate — it's for the lakehouse curated views, not strategy backtests.

### 3. Strategy Source Code is Canonical in DuckDB

The `source_code` column in `backtest_results_v2` is the source of truth. We load strategies by `exec()`-ing this code, not by importing .py files (which may be deleted from disk, as the git audit found).

### 4. LumiBot CcxtBacktesting Over Yahoo for Crypto

LumiBot's `CcxtBacktesting` downloads real Binance OHLCV data via CCXT public API. This gives us:
- Verified exchange data (not Yahoo's crypto tracking)
- 1-minute to daily resolution
- 24/7 market coverage
- DuckDB cache for instant re-runs

---

## Quick Reference

| Item | Value |
|---|---|
| **Project root** | `/home/Zev/development/nexus-trade/` |
| **LumiBot venv** | `/home/Zev/development/trading-bots/lumibot/.venv/bin/python` |
| **LumiBot .env** | `/home/Zev/development/trading-bots/lumibot/.env` |
| **StratForge DB** | `~/.hermes/profiles/herm-bot/home/agentic-quant-os/data/quant.duckdb` |
| **StratForge strategies** | `~/.hermes/profiles/herm-bot/home/agentic-quant-os/strategies/stratforge/active/` |
| **Test data** | `/home/Zev/development/strat-depot/test_data/{BTC,ETH,SOL}_5yr.parquet` |
| **CCXT cache** | `/tmp/lumibot_cache/ccxt/binance/` (auto-created) |
| **Adapter file** | `src/strategies/stratforge_adapter.py` |
| **Batch runner** | `src/runners/batch_crypto_screen.py` |

### Models (Crypto Committee)
| Role | Model | Provider |
|---|---|---|
| Evidence Researcher | `openai/gemini-3-flash-preview` | Ollama Cloud |
| Bull Researcher | `openai/gemini-3-flash-preview` | Ollama Cloud |
| Bear Researcher | `openai/gemini-3-flash-preview` | Ollama Cloud |
| Portfolio Manager | `openai/glm-5-turbo` | z.ai |

### Environment Setup
```bash
cd /home/Zev/development/trading-bots/lumibot
source .venv/bin/activate
export LUMIBOT_CACHE_FOLDER=/tmp/lumibot_cache
cd /home/Zev/development/nexus-trade
```
