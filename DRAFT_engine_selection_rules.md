# DRAFT: Engine-Selection Rules for StratForge Workers

**Status:** DRAFT FOR REVIEW — do not apply yet.
**Target:** Add as a new section (e.g., "Section 3.5") to
`/home/Zev/.hermes/skills/strat-forge-worker/SKILL.md`

---

## Problem

The pipeline has THREE backtesting engines, each with different fidelity levels:

| Engine | Speed | Fidelity | Location |
|---|---|---|---|
| **CrabQuant/vectorbt** | ⚡ Instant | ⚠️ Vectorized (same-bar-close fills) | `quant-research-mas/crabquant/` |
| **sf_validation** | ⚡ Fast | ⚠️ Vectorbt WF (expanding windows) | `strat-forge tools/sf_validation.py` |
| **LumiBot** | 🐢 Slow (2-3 min/backtest) | ✅ Event-driven bar replay | `nexus-trade/src/runners/` |
| **NautilusTrader** | 🐢 Slow (4-7s/backtest) | ✅ Event-driven (Rust core, next-bar-open) | `quant-research-mas/event-bt/` |

Workers currently generate strategies, backtest them in vectorbt, and run WF through
sf_validation — all vectorized. Some strategies' execution semantics are **not faithfully
modeled** by vectorbt. Those strategies get inflated backtest results that won't hold up
in live trading.

The rules below tell workers how to self-classify their strategy and tag it so the system
knows which validation engine to route it to.

---

## Engine-Selection Classification

### Step 1: Check the BANNED list first (existing Section 3)

If your strategy uses ANY of these, it is **structurally broken** in vectorbt. You must
either redesign it OR flag it for event-driven validation:

- ❌ **Intra-bar stop references** (`if low[i] < stop`, `if high[i] >= target`)
- ❌ **Pyramiding / scaling / DCA** (multiple entries while in position)
- ❌ **Fractional position sizing** (Kelly, vol-targeting, % of equity)

### Step 2: Classify by execution complexity

Ask yourself: "Does my strategy's P&L depend on WHEN within a bar the fill happens?"

#### TIER 1 — vectorbt-SAFE (use CrabQuant + sf_validation)

Your strategy is **safe for vectorbt** if ALL of these are true:

- ✅ Entries are computed at bar CLOSE and the exact entry price doesn't matter
  (crossover signals, threshold crosses, indicator-based entries)
- ✅ Exits use **close-based stops** only: `if close[i] <= stop_price`
  (conservative one-bar lag, but fill price is accurate)
- ✅ Exits are **time-based**: `bars_held >= max_hold`
- ✅ Exits are **signal-based**: opposite indicator cross, regime change
- ✅ No pyramiding — one entry, one exit, flat between trades
- ✅ Full capital deployed per trade (100% in/out)
- ✅ Cooldown gating between trades

**Tag:** `engine_tier: vectorbt_safe`
**Validation path:** CrabQuant backtest → sf_validation (WF + param stability + regime coverage)
**Examples:** `ema_crossover`, `macd_momentum`, `rsi_crossover`, `bollinger_squeeze`,
`donchian_breakout`, `atr_channel_breakout`

#### TIER 2 — NEEDS EVENT-DRIVEN VALIDATION

Your strategy **MUST go through event-driven validation** (LumiBot or NautilusTrader)
if ANY of these are true:

- ⚠️ **Position sizing**: You deploy a fraction of capital per trade
  (e.g., 50% in, scale in over 3 bars, risk-based sizing)
- ⚠️ **Pyramiding / scaling**: You add to an existing position
  (e.g., buy 1/3 now, 1/3 at +2%, 1/3 at +5%)
- ⚠️ **Intra-bar stops**: Your exit logic references `high[i]` or `low[i]`
  (e.g., `if low[i] <= stop_price: exit`)
- ⚠️ **DCA / grid**: Multiple entries at staggered price levels
- ⚠️ **Trailing stops with intra-bar reset**: Stop recalculated using intrabar extremes
- ⚠️ **Multi-asset portfolio**: Strategy trades BTC + ETH + SOL simultaneously with
  capital allocation logic

**Tag:** `engine_tier: event_driven_required`
**Validation path:** CrabQuant backtest (for initial screening) → LumiBot WF validation
OR NautilusTrader drift gate
**If you generate a Tier 2 strategy:** The vectorbt backtest is a SCREEN ONLY. The
strategy cannot be promoted to `winner` status without passing event-driven validation.

**IMPORTANT:** Under the current CrabQuant engine constraints, Tier 2 strategies produce
INACCURATE backtests. The preferred path is to **redesign the strategy to be Tier 1**
(move stops to close-based, remove pyramiding, deploy 100% capital). Only use Tier 2 if
the strategy concept genuinely requires these features.

#### TIER 3 — EVENT-DRIVEN NATIVE (vectorbt CANNOT model at all)

- 🔴 **Limit orders / conditional orders** (resting orders that may or may not fill)
- 🔴 **Order book / L2 data dependencies**
- 🔴 **Sub-bar execution logic** (e.g., "buy if price touches X then immediately set
  stop at Y within the same bar")
- 🔴 **Rebalancing** (periodic portfolio rebalancing across N assets)

**Tag:** `engine_tier: event_driven_only`
**Validation path:** LumiBot ONLY. Do not backtest in CrabQuant at all — the results are
meaningless. Write the strategy as a LumiBot Strategy class directly.

---

## How to Tag Strategies

Add the `engine_tier` field to the strategy's metadata when writing to the lakehouse:

```sql
-- In backtest_results_v2 or strategy_potential
engine_tier VARCHAR DEFAULT 'vectorbt_safe'  -- one of:
  -- 'vectorbt_safe'       → Tier 1, vectorbt results are trustworthy
  -- 'event_driven_required' → Tier 2, needs LumiBot/NT validation before promotion
  -- 'event_driven_only'   → Tier 3, vectorbt cannot model this strategy
```

For the DuckDB insert, add the field to the params_json or as a column:

```python
# When recording a backtest result
record = {
    "strategy_name": name,
    "engine_tier": "vectorbt_safe",  # or "event_driven_required"
    # ... other fields
}
```

---

## Routing Logic (for the pipeline / meta agents)

When a strategy reaches the WF validation stage:

1. **`engine_tier = vectorbt_safe`** → Run `sf_validation.walk_forward_validate()`
   (vectorbt-based, fast). If it passes → eligible for promotion to `winner`.

2. **`engine_tier = event_driven_required`** → Run vectorbt WF as a SCREEN. If it passes
   the screen → route to LumiBot WF validation
   (`nexus-trade/src/runners/walk_forward_validation.py`). Only promote to `winner` if
   it passes BOTH.

3. **`engine_tier = event_driven_only`** → Skip vectorbt entirely. Route directly to
   LumiBot. (Rare case; most workers should not generate these.)

### Cross-Engine Final Gate (optional, for top candidates)

For the top N strategies that pass their respective validation tier, run them through
the NautilusTrader drift gate (`event-bt/run_backtest.py`) as a final cross-engine
sanity check. This measures execution drift between vectorbt's same-bar-close fills and
NT's next-bar-open fills. If drift > 40% (Sharpe) → flag for review.

---

## Worker Decision Checklist

Before finalizing any strategy, the worker should run through this checklist:

```
[ ] Does my strategy use intra-bar stops (high[i]/low[i] in exit logic)?
    YES → Tier 2+ (redesign to close-based, or tag event_driven_required)

[ ] Does my strategy pyramid / scale in / DCA?
    YES → Tier 2+ (redesign to single entry, or tag event_driven_required)

[ ] Does my strategy use fractional position sizing?
    YES → Tier 2+ (redesign to 100% deploy, or tag event_driven_required)

[ ] Are all exits close-based, time-based, or signal-based?
    YES → Good, likely Tier 1

[ ] Does my strategy deploy 100% capital per trade?
    YES → Good, likely Tier 1

[ ] If all above check out → Tag: engine_tier = vectorbt_safe
```

---

## Integration Notes

- These rules **supplement** existing Section 3 (Engine Execution Model) — they don't
  replace it. Section 3 explains WHY vectorbt has fidelity limits; these rules explain
  WHAT TO DO about it.
- The `engine_tier` tag should be added to the DuckDB schema as a new column or stored
  in `params_json`. This requires a migration.
- The routing logic in the meta/validator agents needs to be updated to check
  `engine_tier` and dispatch accordingly.
- The LumiBot WF runner (`walk_forward_validation.py`) is already built and operational
  — it just needs to be integrated into the automated pipeline (currently manually invoked).
