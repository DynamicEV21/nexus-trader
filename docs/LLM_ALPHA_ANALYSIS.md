# Nexus Trader — Where LLMs Add Alpha vs Where Rules Win

> **Audience:** Builder/quant making build-vs-buy decisions on the LLM layer
> **Verdict up front:** The LLM adds genuine alpha in exactly ONE place —
> news/event/sentiment integration. Everywhere else in the current design,
> deterministic rules are faster, cheaper, more reproducible, and likely more
> profitable. The current 4-agent committee is mis-allocated: it's doing TA
> interpretation that rules do better, while the one job it should own (news
> filtering) isn't wired up at all.

---

## 1. The Honest Scorecard: LLM vs Rules, by Task

This is the core finding. Score each task on whether an LLM beats a
deterministic rule for THIS specific job, in a crypto backtest/live context.

| Task | Rules Better | LLM Better | Why |
|------|:---:|:---:|-----|
| Compute RSI/MACD/SMA/ATR | ✅ | | Deterministic, instant, free. `signal_dashboard_tool.py` already does this perfectly. LLM "interpreting" the output is a fixed mapping (RSI>70=overbought) — no reasoning added. |
| Regime classification | ✅ | | CrabQuant `detect_regime` already returns the label + scores. LLM reading the label adds nothing. |
| Entry/exit signal generation | ✅ | | StratForge strategies are walk-forward validated (≥3/5 windows profitable, Sharpe>0). The LLM cannot beat a validated deterministic strategy on the same OHLCV. |
| Position sizing math | ✅ | | Kelly, fixed-fractional, ATR-based sizing are optimal and deterministic. LLM "judgment" introduces variance with no expected gain. |
| Order execution (type, timing) | ✅ | | LLMs are slow (5-30s), non-deterministic, and the LumiBot engine already handles market/limit/stop. |
| Strategy SELECTION per regime | tie | tie | A `regime→strategy` lookup table works and is auditable. LLM *might* handle edge cases, but you can't backtest the "might." |
| **News / event / sentiment filtering** | | ✅ | **The only genuinely new information.** Rules cannot read "SEC rejected ETF" or "FTX hack" or "Binance outage." |
| **Narrative synthesis across data** | | ✅ | Synthesizing funding + on-chain + news into "short squeeze risk rising" is hard to codify. LLMs *can* do this — but reliability is the question. |
| Cross-asset divergence reasoning | | ✅? | "BTC dumping, SOL holding" pattern recognition. Plausibly useful, hard to validate. |
| Anomaly / regime-break flagging | | ✅? | "This move matches nothing in history, stand down." Statistical methods (z-score, isolation forest) also do this and are backtestable. |

**Bottom line:** Of the ~10 distinct tasks in the system, the LLM is clearly
superior in exactly **2** (news filtering, narrative synthesis), plausibly
useful in **2 more** (cross-asset reasoning, anomaly flagging), and strictly
**inferior** in **5** that it's currently being asked to do.

The current committee spends 90% of its tokens and latency on the 5 tasks
where it loses to rules.

---

## 2. What's Actually Wrong With the Current Committee Design

Reading `nexus_committee.py` against the walk-forward validator reveals a
structural mismatch:

### 2.1 The committee is built for equities, the alpha is in crypto
- `DEFAULT_UNIVERSE` = `["AAPL", "MSFT", "NVDA", ...]` — stocks.
- `__main__` runs `YahooDataBacktesting` with `Asset("SPY")` benchmark.
- `regime_tool.py` fetches **SPY + VIX** — equity volatility proxies that tell
  you nothing about crypto regime. There is no BTC dominance, no funding rate,
  no stablecoin flow, no on-chain signal.
- The entire validated edge (29M rows of Kraken 1-min, walk-forward on BTC/ETH/
  SOL, StratForge strategies passing majority vote) lives in the rules-based
  path and is **never seen by the committee**.

### 2.2 The bull/bear debate is theater in a long-only strategy
The PM prompt says "Do not short. Do not use options." So the bear agent's
entire output collapses to "don't buy" or "buy less." You're paying for 3 LLM
round-trips (evidence → bull → bear → PM) to produce a go/no-go that a single
conviction-scored rule produces for free.

### 2.3 The LLM is interpreting indicators it shouldn't touch
`signal_dashboard_tool.py` already computes RSI, MACD, SMA crossovers, ATR,
Bollinger position, trend alignment, and a `risk_recommendation` — all
deterministically. The evidence researcher then calls this and "writes it up"
in prose for the bull/bear/PM to read. That's **4 LLM calls to restate a dict
in English.** The dict was already the answer.

### 2.4 It can't even complete a backtest
Per `EXECUTION_ROADMAP.md` Phase 0 results: GLM-5 overflows context at ~4
months of daily bars. Memory notes grow unboundedly within a run, breaking
replay cache keys. So the committee **cannot be walk-forward validated** on the
same 5-window, 6-month-OOS standard the rules path uses. You'd be comparing an
unvalidatable system against a validated one.

### 2.5 Cost and latency make it unviable at crypto frequency
- 4 agents × 1 call/day × ~180 days = 720 LLM calls per 6-month backtest.
- At 4H bars (the validated crypto timeframe), that's 4,320+ calls per 6 months.
- Crypto moves intraday; a daily-sleeptime LLM misses everything that matters.
- An RTX 4050 can't run these models locally, so every call is a paid API round
  trip with 5-30s latency.

---

## 3. The Optimal Hybrid Architecture

**Principle: Rules generate the signal. The LLM filters it. The LLM never
invents trades from price data it's worse at reading than the rules.**

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1 — DETERMINISTIC (fast, free, backtestable)          │
│                                                              │
│  Kraken 4H OHLCV                                             │
│       │                                                      │
│       ▼                                                      │
│  StratForge strategy_fn(df)  ──►  raw entry/exit signal     │
│       │                                                      │
│  Crypto regime detector      ──►  regime label + score      │
│  (BTC dom, funding, vol — NOT SPY/VIX)                       │
│       │                                                      │
│  ▼                                                           │
│  CONVICTION SCORE (rules-based, 0-1):                        │
│   = f(signal_agreement, regime_fit, atr, volume_confirm)     │
│       │                                                      │
│       ▼                                                      │
│  DECISION GATE:                                              │
│   conviction ≥ 0.7  →  EXECUTE (rules handle sizing)         │
│   conviction < 0.3  →  SKIP                                  │
│   0.3 – 0.7        →  ROUTE TO LLM OVERLAY  ◄── only here    │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  LAYER 2 — LLM OVERLAY (fires only on ambiguous signals)     │
│                                                              │
│  Single fast model (GLM-5 Flash / Gemini Flash), NOT 4 agents│
│  Input: signal + regime + NEWS/SENTIMENT (the real edge)     │
│  Question: "Does news/events confirm or invalidate this?"    │
│  Output: {confirm | veto | reduce_size | pass} + reason      │
│                                                              │
│  Fires maybe 5-15% of bars, not 100%.                        │
└─────────────────────────────────────────────────────────────┘
```

### Why this beats the current committee
1. **The LLM only runs when it can add information** — when the rules are
   uncertain AND there's news to evaluate. On clear signals, rules execute
   instantly with zero cost.
2. **1 model, 1 call, not 4 agents × 4 calls.** ~16x cheaper, ~16x faster.
3. **The LLM's input is NEWS, not indicators.** This is where it has
   information the rules don't. Feeding it RSI is feeding it a worse version of
   what it already has.
4. **Fully backtestable.** The rules path runs through the existing walk-forward
   validator unchanged. The LLM overlay runs only on the ambiguous-signal
   subset — a much smaller, faster, cheaper backtest.
5. **Position sizing stays deterministic.** The LLM can say "reduce_size by
   50%" but the math is rules-based.

### What happens to the 4-agent debate?
Kill it for execution. **Repurpose it as a daily research brief**, run once per
day (not per bar), that produces a written market commentary injected into the
trading context. This is where multi-agent reasoning shines — slow, deep,
once-daily synthesis — not real-time execution.

---

## 4. Crypto-Specific Tools That Add Real Edge (Ranked)

The current `regime_tool.py` (SPY/VIX) and `signal_dashboard_tool.py` (generic
TA) are equity tools mislabeled for crypto. For BTC/ETH/SOL on Kraken, here's
what actually carries information:

### Tier 1 — Build these, they have proven predictive value

1. **Funding rate signal** (highest priority)
   - Perpetual futures funding rates are a direct measure of positioning.
   - Negative + extreme = crowded short → squeeze risk.
   - Positive + extreme = crowded long → flush risk.
   - Free data: Coinglass, exchange APIs. Deterministic, backtestable.
   - *This is a RULES signal, not an LLM signal — but it's missing entirely.*

2. **Crypto-native Fear & Greed Index**
   - Alternative.me's index is free, daily, historical.
   - Contrarian at extremes (<20 = potential bottom, >80 = potential top).
   - Deterministic threshold rules. Backtestable.

3. **BTC Dominance regime** (replaces SPY/VIX for crypto)
   - Rising dominance = risk-off within crypto (alt sell-off).
   - Falling dominance = alt season (capital rotating to alts).
   - This is the crypto equivalent of VIX. The current regime tool doesn't have it.

### Tier 2 — High value, harder to source historically

4. **Stablecoin supply / market cap**
   - Growing USDT/USDC supply = capital inflow to crypto (bullish).
   - Proxy for liquidity. CoinGecko / DefiLlama, free.

5. **Exchange netflow** (on-chain)
   - BTC flowing TO exchanges = sell pressure.
   - BTC flowing FROM exchanges = accumulation.
   - Glassnode (paid) or on-chain parsing. Partially backtestable.

6. **News sentiment stream** ← **the LLM's actual job**
   - CryptoPanic, CoinDesk RSS, Twitter/X sentiment.
   - THIS is what the LLM should be reading, not RSI values.
   - The single highest-value LLM integration: feed headlines → get
     impact score. Rules cannot do this.

### Tier 3 — Nice to have, lower signal-to-noise

7. **Open interest** (perps) — alongside funding, confirms positioning.
8. **Liquidation heatmaps** — where stops cluster. Hard to get historically.
9. **Whale transaction alerts** — high noise.
10. **MVRV / on-chain valuation** — more for macro timing than intraday.

### What NOT to build
- **SEC filings / FRED macro / earnings tools** — these are in LumiBot's
  built-in set and are equity-specific. They add zero crypto alpha and the
  committee wastes calls on them.
- **Options Greeks** — crypto perp markets don't have clean greeks; skip.

---

## 5. What to Build FIRST (The Decisive Test)

The question is simple: **does the LLM layer improve PnL over the validated
rules baseline?** Answer it with the cheapest possible experiment before
investing in the full hybrid.

### Build: "LLM News Veto" — a single-overlay A/B backtest

```
ARM A (baseline):   StratForge signal → execute (current validated path)
ARM B (treatment):  StratForge signal → LLM news check → execute or veto
```

**Implementation (minimal):**

1. **Take ONE walk-forward-PASSING strategy** on ONE asset (e.g., the best
   StratForge strategy on BTC, 4H bars).

2. **Build a `news_sentiment_tool`** that returns, for a given timestamp:
   - Recent headlines (CryptoPanic API, free tier).
   - A pre-computed sentiment score (use a simple model or even VADER —
     deterministic, backtestable, no LLM needed for the baseline signal).

3. **Run ARM A**: existing `StratForgeSignalAdapter` through
   `walk_forward_validation.py`. You already have this.

4. **Run ARM B**: same adapter, but before each entry, call a SINGLE LLM
   (Gemini Flash — cheapest, fastest) with:
   - The signal (entry/exit).
   - The regime.
   - The last 5 headlines + sentiment score.
   - Prompt: *"Given this long entry signal and these headlines, is there
     news that invalidates this trade? Reply ONLY: {EXECUTE|VETO|REDUCE_50}."*

5. **Compare** total return, Sharpe, max drawdown, win rate across the same
   5 walk-forward windows.

**Decision rule:**
- If ARM B's avg OOS Sharpe > ARM A's by a meaningful margin (e.g., >0.2) AND
  survives ≥3/5 windows → the LLM layer has alpha. Invest in the full hybrid.
- If ARM B ≈ ARM A or worse → **the LLM doesn't add alpha to a validated
  rules system. Drop it, or confine it to the daily-research-brief role.**

**Why this test is decisive:**
- It isolates the ONE variable (LLM news filtering) against a controlled
  baseline.
- It uses the SAME walk-forward methodology as the validated rules, so the
  comparison is apples-to-apples.
- It's cheap: 1 model, veto-only, runs only on entry bars (not every bar).
- It answers the question in days, not weeks.

**Estimated cost:** A few hundred LLM calls (entries only, ~5-15% of bars).
At Flash-tier pricing, under $5. RTX 4050 is irrelevant — all calls are remote.

---

## 6. Failure Modes — Where LLMs Hurt Trading Performance

These are concrete, observed, or near-certain risks for THIS system:

### 6.1 Latency kills crypto entries
Crypto 4H bars are fine, but the moment you go intraday or live, a 10-30s LLM
round-trip means you enter after the move. **Mitigation:** LLM never blocks a
time-critical entry; it only vetoes pre-signal or runs async.

### 6.2 Non-determinism destroys reproducibility
Same prompt + same context → different trade decisions on re-run. This makes
walk-forward validation meaningless (you validated one sample path, not the
strategy). **Mitigation:** temperature=0, seed pinning, and/or the LLM only
outputs {EXECUTE|VETO|REDUCE} (3 options), not free-form reasoning.

### 6.3 Overtrading / action bias
LLMs trained on helpful-assistant data want to "do something." The committee
prompt says "prefer doing nothing when evidence is weak" but in practice the
PM agent places orders to seem decisive. The AGENTS.md notes already flag
limit-order-chasing. **Mitigation:** the hybrid gate (rules decide; LLM only
vetoes) structurally prevents this.

### 6.4 Lookahead bias via internet tools
`EXECUTION_ROADMAP.md` Phase 0 explicitly flags this as **untested and
high-risk**: news tools, FRED, web search may return future-dated data in
backtest mode, silently injecting lookahead. If the LLM reads tomorrow's news
in a backtest, every result is fake. **Mitigation:** before trusting ANY
LLM+news backtest, run the controlled experiment from the roadmap (P&L must be
identical with/without internet tools). Until that passes, the LLM news layer
is **unbacktestable and therefore unusable for live capital.**

### 6.5 Context-window overflow → silent degradation
Already documented: GLM-5 overflows at ~4 months. When context overflows,
models either truncate (losing early context) or error. A strategy that works
for 3 months and then degrades in month 4 is a strategy that fails walk-forward
window 1. **Mitigation:** the hybrid architecture sidesteps this — the LLM
overlay has no memory accumulation (stateless per-call).

### 6.6 Hallucinated fundamentals/narrative
GLM/Gemini will confidently invent "BTC ETF inflows surged today" if pressed.
In a backtest this is undetectable noise; in live it's real money lost.
**Mitigation:** the LLM must cite a real headline from the news tool; if no
news tool returned data, the LLM gets no news input (it can't hallucinate what
it wasn't given).

### 6.7 Regime-tool mismatch (already present)
`regime_tool.py` uses SPY/VIX for a crypto system. An LLM making decisions
based on "SPY is trending up" while trading SOL is reasoning about the wrong
market. **Mitigation:** replace with crypto-native regime (BTC dominance,
funding, realized vol) before any LLM sees regime context.

### 6.8 Cost explosion at scale
4 agents × every bar is financially untenable for live crypto trading. Even
the hybrid (1 agent, ambiguous bars only) needs a cost ceiling. **Mitigation:**
hard cap on LLM calls/day; fall through to rules when budget exhausted.

---

## 7. Concrete Recommendations (Prioritized)

### Do now (this week)
1. **Run the decisive A/B test** (§5). This is the single highest-value action.
   Until you know if the LLM adds PnL, everything else is speculation.
2. **Fix the regime tool for crypto** — swap SPY/VIX for BTC dominance +
   funding rate + realized vol. The current tool feeds the LLM (and any rules)
   wrong-market data.

### Do if the A/B test shows alpha
3. **Build the hybrid gate** (§3) — conviction-scored routing with LLM overlay
   only on ambiguous signals.
4. **Build `news_sentiment_tool`** with backtest-safe time filtering (§6.4).
5. **Wire crypto-native signals** (funding, F&G, stablecoin supply) as
   deterministic inputs to both the rules and the LLM.

### Do regardless (improves the rules baseline too)
6. **Add funding-rate and F&G as StratForge strategy filters** — these are
   rules signals, cheap, and likely improve the baseline that the LLM has to beat.
7. **Keep the 4-agent committee as a daily research brief only** — run once/day,
   output written commentary, inject as context. Don't let it trade.

### Stop doing
8. **Stop running the committee as the execution layer.** It's slower, more
   expensive, less reproducible, and unvalidatable against the rules baseline.
9. **Stop feeding the LLM indicator values** (RSI, MACD). It adds no value over
   the dict it came from. Feed it NEWS.
10. **Stop using equity tools (SPY, VIX, SEC filings, FRED) for crypto.** They
    consume context and tokens for zero crypto-relevant signal.

---

## 8. The One-Sentence Summary

**Your validated rules-based path is the strategy; the LLM's only defensible
job is reading news the rules can't see — test that single hypothesis with a
cheap A/B veto experiment before building anything else, and confine the
4-agent committee to a daily research brief where slow reasoning is an asset,
not a latency penalty.**
