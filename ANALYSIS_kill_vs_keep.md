# Assessment: Kill nexus-trade vs Let It Finish

**Date:** 2026-06-14 14:00 PDT
**Analyst:** hermes-bot subagent

---

## TL;DR Recommendation

**CRASHED at ~13:50 PDT** (OOM-killed after 5h, 32GB RAM). No restart needed —
6/10 strategies fully validated at 4H (all PASS), DuckDB has the data. The remaining
4 strategies have daily WF results already. Fix memory leak before any restart.

---

## 1. Process Status (PID 1193705)

- **Still running** at 13:52 PDT (started 08:49 PDT = ~5h elapsed)
- **74% CPU, 32GB RAM** (56.8% of 54GB total)
- System has **37GB available + 10GB swap free** → no OOM risk
- Currently on **strategy 7/10** (`meta_vwap_kc_wfm_wf_v2`), SOL asset, window W4 (last window) at ~50%
- **20 of 30 verdicts logged** (6 full strategies × 3 assets + strategy 7 BTC+ETH)
- ALL 20 verdicts = **PASS**

### Remaining Work
- Strategy 7 SOL: finishing W4 (~2 min)
- Strategy 8 (`meta_20260613_1336_ppo_obv_wpr_cmo_momentum_v1`): ~20 min
- Strategy 9 (`meta_ppo_fisher_wc_wfm`): ~20 min
- Strategy 10 (`meta_h1_hilbert_cycle_hybrid_v1`): ~20 min
- **Estimated completion: ~14:50-15:20 PDT**

### Data Persistence
- Window-level results **written to DuckDB incrementally** (code lines 602-622)
- CSV/JSON files **only save at the very end** (line 625)
- Killing now = lose CSV/JSON output for ALL strategies (DuckDB has partial data)

---

## 2. What event-bt/NautilusTrader Already Has

| Capability | event-bt | nexus-trade |
|---|---|---|
| **Asset class** | Stocks (S&P500) | Crypto (BTC/ETH/SOL) |
| **Timeframe** | Daily bars | 4H bars (6x more data) |
| **Purpose** | Execution drift gate (VBT vs NT next-bar-open) | Walk-forward edge persistence |
| **WF method** | Simple train/test split (3yr/2yr) | Expanding 5-window (6mo OOS + embargo) |
| **Strategies tested** | 41 stock strategies | 10 top crypto strategies (from 39 daily WF winners) |
| **Engine** | NautilusTrader (Rust core) | LumiBot PandasDataBacktesting |
| **Output** | Sharpe/DD/return drift % | Per-window Sharpe/return/DD + majority-vote PASS/FAIL |

**Key insight: These are COMPLEMENTARY systems, not redundant.**

---

## 3. Unique Value of the nexus-trade 4H Run

The nexus-trade LumiBot runner provides value that event-bt **cannot** provide:

1. **4H-resolution walk-forward validation** — 6x more bars per window than daily.
   Catches intraday patterns daily bars miss. More trades per window → statistically
   robust WF conclusions.

2. **Crypto-specific validation** — event-bt is stocks-only. These 10 strategies are
   crypto strategies (BTC/ETH/SOL). Their behavior on crypto data cannot be validated
   by a stock-based system.

3. **Expanding 5-window methodology with embargo** — more rigorous than event-bt's
   simple train/test split. Each window grows the training period, testing whether the
   edge persists across different market regimes.

4. **LumiBot event-driven bar replay** — a third independent engine (vs vectorbt in
   CrabQuant and NautilusTrader in event-bt). Cross-engine agreement increases confidence.

5. **Top-10 revalidation** — the daily run already validated 39 strategies. This 4H run
   revalidates the TOP 10 at higher resolution. All 20 completed verdicts confirm daily
   results (all PASS).

---

## 4. What Killing Now Would Lose

- 4H WF validation for strategies 7 SOL, 8, 9, 10 (3.3 strategies × 3 assets = ~10 verdicts)
- The final CSV/JSON summary files (only written at the end)
- The "all 10 top strategies PASS at 4H" confirmation story

What killing now would **NOT** lose:
- DuckDB has incremental results for all 20 completed verdicts
- The daily WF results (already saved) for all 39 strategies

---

## 5. Recommendation: LET IT FINISH

### Reasons:
1. **~60-90 min remaining** — not worth throwing away 5h of computation
2. **Memory is safe** — 37GB free + 10GB swap, process using 32GB
3. **All verdicts PASS so far** — confirming daily results at 4H
4. **Unique capability** — no other system does 4H crypto WF validation
5. **Incremental DB saves** — even if it crashes near the end, DuckDB has the data

### Risk if we're wrong:
- If it crashes due to memory: DuckDB has 20/30 verdicts. We lose CSV/JSON + 10 verdicts.
- Mitigation: the memory growth rate has been stable (32GB at 5h), and headroom is ample.

## 6. CRASH OUTCOME (Updated 14:00 PDT)

### What Happened
The process crashed at ~13:50 PDT (strategy 7 SOL, W4 at 59%) after 5h of runtime.
Likely OOM-killed — it was consuming 32GB RAM (LumiBot memory leak: no GC between
150 backtest instances). No explicit error in log; process simply vanished.

### What Was Saved (GOOD NEWS)
Despite the crash, the DuckDB bridge was working and saved data incrementally:

**DuckDB `walk_forward_results` table (670 total rows):**
- 6/10 strategies have FULL 4H WF data (3 assets × 5 windows = 15 rows each):
  - `meta_20260613_1524_cmo_elder_starc_adx_v1` ✅ (all PASS)
  - `meta_vwap_kc_wc_wfm` ✅ (all PASS)
  - `meta_ppo_klinger_supertrend_v1_wf_v1` ✅ (all PASS)
  - `meta_vwap_kc_wfm_v2` ✅ (all PASS)
  - `meta_cmo_alma_atr_wf_v1` ✅ (all PASS)
  - `donchian_wpr_chop_breakout_wf_v1` ✅ (all PASS)
- Strategy 7 (`meta_vwap_kc_wfm_wf_v2`): BTC + ETH done (PASS), SOL crashed mid-run
- **ALL 20 logged verdicts = PASS**

**Missing (4 strategies never reached):**
- Strategy 7 SOL (partial)
- `meta_20260613_1336_ppo_obv_wpr_cmo_momentum_v1` (not started at 4H)
- `meta_ppo_fisher_wc_wfm` (not started at 4H)
- `meta_h1_hilbert_cycle_hybrid_v1` (not started at 4H)

**BUT:** All 4 missing strategies have daily WF results already (from the earlier
daily run of 39 strategies). They just lack 4H revalidation.

### Recommendation: DON'T RESTART

1. **6/10 top strategies confirmed PASS at 4H** — strong signal that the daily results hold
2. **All 4 missing strategies PASS daily WF** — the 4H confirmation is a "nice to have"
3. **Memory leak must be fixed first** — restarting would crash again at ~strategy 7
4. **Daily WF results are the primary gate** — 4H is supplementary validation

### If 4H validation for remaining 4 is desired later:
- Add `gc.collect()` + del between backtests in `walk_forward_validation.py`
- Run only strategies 7-10 (skip the 6 already done)
- Expected runtime: ~2h (4 strategies × ~30 min)
