# Financial Data Research

**Date:** 2026-05-31  
**Source:** `/home/Zev/development/quant-projects/financial-data/`

## Directory Structure

```
quant-projects/financial-data/
├── .env                          # POLYGON_API_KEY
├── crypto/
│   ├── 1 min/                    # 801MB — 12 parquet files (BTC, ETH, SOL, XRP, ADA, etc.)
│   ├── Kraken_Trading_History/   # 18GB — real Kraken trading history
│   ├── binance_daily/            # Binance daily bars
│   ├── kraken_daily/             # Kraken daily bars (VWAP + trade count)
│   ├── daily/                    # Aggregated daily crypto
│   └── data_fetchers/            # Download scripts
├── stocks/
│   ├── daily/                    # 33 tickers, 1yr Polygon (real VWAP + n_trades)
│   ├── sp500_daily/              # 503 S&P tickers, yfinance daily (5yr, ~1,255 bars each)
│   ├── polygon_1min/             # Polygon 1-min (currently minimal)
│   ├── ohlcv_1min/               # Additional 1-min data
│   └── 1m/                       # 1-min data
├── macro/
│   └── fred/                     # FRED macro indicators
├── news/                         # News data
├── shared-scripts/               # Reusable download scripts
├── DATA_PROVIDERS.md             # Comprehensive API reference
├── DATA.md                       # Data inventory
├── CRYPTO_NEWS_APIS.md           # Crypto news pipeline reference
└── OPENBB_INTEGRATION.md         # OpenBB integration docs
```

## API Keys Available (NO SIGNUP NEEDED)

| Provider | Key | Auth Needed | Free Tier | What We Get |
|----------|-----|-------------|-----------|-------------|
| **Polygon** | ✅ Yes | Yes | 5 calls/min, 500 bars/call | Stock + crypto OHLCV, VWAP, n_trades |
| **FRED** | ✅ Yes | Yes | Unlimited | Macro indicators (15+) |
| **FMP** | ✅ Yes | Yes | ~15 calls then 402 | Fundamentals (BS/IS/CF, 5yr) |
| **Finnhub** | ✅ Yes | Yes | Limited | News + profiles |
| **Alpha Vantage** | ✅ Yes | Yes | 25 calls/day | Sentiment |
| **Tiingo** | ✅ Yes | Yes | Daily only | Daily stocks/crypto |
| **Binance** | None | **NO** | 1200 req/min | OHLCV all timeframes, futures |
| **Kraken** | None | **NO** | 6 calls/sec | OHLCV + VWAP + trade count |
| **Coinbase** | None | **NO** | 10 req/sec | Public data |
| **SEC EDGAR** | None | **NO** | 10 req/sec | All filings |
| **yfinance** | None | **NO** | No cap | OHLCV deep history |

## What We Can Backtest RIGHT NOW

### Stocks (No signup)
- **503 S&P tickers** — yfinance daily, 5 years of history, already on disk
- **33 tickers** — Polygon daily with real VWAP, 1 year, already on disk
- Source: `stocks/sp500_daily/` and `stocks/daily/`

### Crypto (No signup)
- **12 pairs** — Kraken 1-minute bars, 801MB, already on disk
- **BTC, ETH, SOL, XRP, ADA, AVAX, DOT, LINK, LTC, BCH, ATOM, DOGE**
- **18GB** Kraken trading history available
- **Binance daily** for many more pairs
- Source: `crypto/1 min/`, `crypto/kraken_daily/`, `crypto/binance_daily/`

### Macro (No signup)
- **15+ FRED indicators** — unlimited access
- Source: `macro/fred/`

## Data NOT Available (Need Signup)

| What | Provider | Why |
|------|----------|-----|
| **Alpaca paper trading** | Alpaca | Need Alpaca account (free, 2 min signup) |
| **Live stock 1-min** | Polygon | Free tier = 5 calls/min (slow) |
| **Live crypto stream** | Binance/Kraken WebSocket | Available via free APIs, no signup |

## Polygon Free Tier Limitations

- **5 calls/min** then 429. Cooldown: 45 seconds.
- **Daily bars:** 500 per request (~2 years max). No pagination.
- **1-min bars:** 50,000 per page, paginated via next_url. Full 2+ year history.
- **Intraday stock data = "DELAYED"** (15-min lag). Daily = OK. Crypto = OK.
- **Workaround:** Download once, cache locally. We already have 503 tickers on disk.

## WebSocket (Real-Time) — Free, No Signup

- **Binance WebSocket:** Live klines, ticker, trades — instant, no auth
- **Kraken WebSocket:** Live OHLC, ticker — instant, no auth
- **Polygon WebSocket:** Requires paid plan

## For Nexus Trader Backtesting

### Immediate (Today)
1. Use yfinance daily SP500 data (503 tickers, 5 years) → stocks
2. Use Kraken 1-min crypto data (12 pairs, 801MB) → crypto
3. Use Polygon daily data (33 tickers, real VWAP) → higher-quality stocks

### For Lumibot Integration
- **Lumibot has `YahooDataBacktesting`** — works out of the box with yfinance
- **Lumibot has `CcxtBacktesting`** — works with Binance/Kraken via ccxt, auto-downloads
- **Lumibot has `PolygonDataBacktesting`** — uses our Polygon API key, 5 calls/min
- **Lumibot has `PandasData`** — can load ANY pandas DataFrame (our parquet files!)
