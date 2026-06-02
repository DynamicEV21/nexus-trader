# Backtesting Research

**Date:** 2026-05-31  
**Source:** `/home/Zev/development/trading-bots/lumibot/`

## Lumibot Backtesting Architecture

### BacktestingBroker
**File:** `lumibot/backtesting/backtesting_broker.py`

The BacktestingBroker simulates a real broker:
- Maintains portfolio state (cash, positions, P&L)
- Fills orders at historical bar prices (close or VWAP)
- Supports market/limit/stop orders
- Handles dividends, stock splits
- Simulates margin for futures (has margin table for CME products)
- Emits trade CSV + tearsheet HTML

### How Time Simulation Works
```
1. You specify: datetime_start, datetime_end
2. BacktestingBroker advances time bar-by-bar
3. For each bar:
   a. Update portfolio state (mark-to-market)
   b. Check all orders (fill limit/stop if triggered)
   c. Call strategy.on_trading_iteration()
   d. Strategy sees data up to current bar (no lookahead)
4. At end: save tearsheet + trades CSV
```

### Data Sources for Backtesting

| Source | File | Auth | What It Does |
|--------|------|------|-------------|
| **YahooDataBacktesting** | `yahoo_backtesting.py` | None | Downloads from yfinance, free |
| **PolygonDataBacktesting** | `polygon_backtesting.py` | API key | Downloads from Polygon, 5/min |
| **CcxtBacktesting** | `ccxt_backtesting.py` | None | Downloads from Binance/Kraken via ccxt |
| **PandasData** | `pandas_data.py` | None | Load ANY pandas DataFrame (our parquets!) |
| **AlpacaBacktesting** | `alpaca_backtesting.py` | Alpaca | Alpaca historical data |
| **DatabentoBacktesting** | `databento_backtesting.py` | Paid | Professional tick data |

### CRITICAL: PandasData — Load Our Existing Data
```python
from lumibot.backtesting import PandasDataBacktesting
import pandas as pd

# Load our Kraken 1-min crypto data
df = pd.read_parquet("crypto/1 min/ETHUSD.parquet")
df = df.rename(columns={"timestamp": "date"})
df["date"] = pd.to_datetime(df["date"])

data_source = PandasDataBacktesting(
    datetime_start=datetime(2025, 1, 1),
    datetime_end=datetime(2026, 1, 1),
    pandas_data={"ETH/USD": df},  # Just pass a dict of DataFrames!
)
```

This means we can use our 801MB of crypto data + 503 SP500 daily files directly, no API calls needed.

### CCXT Backtesting — Crypto
```python
from lumibot.backtesting import CcxtBacktesting

# Downloads from Binance automatically (no auth needed)
data_source = CcxtBacktesting(
    datetime_start=datetime(2024, 1, 1),
    datetime_end=datetime(2026, 1, 1),
    exchange_id="binance",  # or "kraken"
)
```

CcxtBacktesting uses `CcxtCacheDB` to download and cache data. It supports:
- **Binance** (default): OHLCV for all pairs, all timeframes
- **Kraken**: OHLCV + VWAP + trade count
- Minimum timestep: 1 minute
- Auto-downloads with buffer (300 bars before start date)

### Polygon Backtesting
```python
from lumibot.backtesting import PolygonDataBacktesting

data_source = PolygonDataBacktesting(
    datetime_start=datetime(2024, 1, 1),
    datetime_end=datetime(2026, 1, 1),
    api_key="YOUR_KEY",
    max_memory=500_000_000,  # 500MB memory limit (LRU eviction)
)
```

- Uses Polygon's REST client (polygon-api-client)
- Has storage limit with LRU eviction
- 500 bars max per daily request
- Intraday is paginated

### Yahoo Backtesting — Simplest Option
```python
from lumibot.backtesting import YahooDataBacktesting

data_source = YahooDataBacktesting(
    datetime_start=datetime(2024, 1, 1),
    datetime_end=datetime(2026, 1, 1),
)
```

- No API key needed
- Downloads on the fly from yfinance
- Good for daily bars (up to 5 years)
- 1-min only 5 trading days

## What Works for Us RIGHT NOW

### Stocks (Daily) — Zero Config
```python
YahooDataBacktesting(datetime(2021, 1, 1), datetime(2026, 5, 31))
```

### Crypto (Any timeframe) — Zero Config
```python
CcxtBacktesting(datetime(2024, 1, 1), datetime(2026, 5, 31), exchange_id="binance")
```

### Crypto (Our cached data) — Zero Config
```python
# Load parquet → PandasDataBacktesting
df = pd.read_parquet("quant-projects/financial-data/crypto/1 min/ETHUSD.parquet")
```

### Stocks (Higher quality) — Polygon key needed
```python
PolygonDataBacktesting(datetime(2024, 1, 1), datetime(2026, 5, 31), api_key=POLYGON_KEY)
```

## Minimal Backtest Example
```python
from datetime import datetime
from lumibot.strategies import Strategy
from lumibot.backtesting import YahooDataBacktesting

class SimpleMomentum(Strategy):
    def initialize(self):
        self.sleeptime = "1D"
    
    def on_trading_iteration(self):
        bars = self.get_historical_prices("SPY", 20, "day")
        if bars is None or len(bars.df) < 20:
            return
        closes = bars.df["close"].values
        sma_short = closes[-5:].mean()
        sma_long = closes[-20:].mean()
        
        if sma_short > sma_long and not self.get_positions():
            price = closes[-1]
            qty = int((self.get_cash() * 0.5) / price)
            if qty > 0:
                self.submit_order(self.create_order("SPY", qty, "buy"))
        elif sma_short < sma_long and self.get_positions():
            self.submit_order(self.create_order("SPY", self.get_positions()[0].quantity, "sell"))

SimpleMomentum.backtest(
    YahooDataBacktesting,
    backtesting_start=datetime(2024, 1, 1),
    backtesting_end=datetime(2026, 1, 1),
    benchmark_asset="SPY",
    budget=10000,
    name="simple_momentum",
)
```

## Futures Support
BacktestingBroker has margin tables for:
- CME Micro E-mini: MES, MNQ, MYM, M2K, MCL, MGC
- CME E-mini: ES, NQ, YM, RTY  
- CME Full-Size: CL, GC, SI, NG, HG
- CME Currency: 6E, 6J, 6B, 6C
- CME Interest Rates: ZB, ZN, ZF, ZT
- CME Agricultural: ZC, ZS, ZW, ZL

Supported via Tradovate broker (needs Tradovate account).

## What "Kashy" Might Be
No platform called "Kashy" found. Possibly:
- **Cash App** (not a trading platform)
- **Kaspa** (crypto coin) — could trade via Binance
- Something else? Need clarification.
