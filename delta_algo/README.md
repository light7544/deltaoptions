# Delta Exchange Options Selling Algo

Production-grade options selling algorithm for BTC/ETH on Delta Exchange India.

## Strategies Supported
- **Short Strangle** (default) — Sell OTM call + OTM put
- **Iron Condor** — Sell OTM call spread + OTM put spread

## Quick Start

### 1. Install dependencies
```bash
cd delta_algo
pip install -r requirements.txt
```

### 2. Configure credentials
Edit `config/config.yaml`:
```yaml
exchange:
  environment: "testnet"   # Start with testnet!
  api_key:    "YOUR_KEY"
  api_secret: "YOUR_SECRET"
```
Get keys at: https://testnet.delta.exchange → Account → API Keys

### 3. Run backtest (no API keys needed)
```bash
python run_backtest.py --synthetic
python run_backtest.py --underlying ETH --start 2024-01-01 --end 2024-12-31
```

### 4. Run live on testnet
```bash
python run_live.py --config config/config.yaml
```

### 5. Emergency stop
```bash
python run_live.py --emergency-stop
```

## Architecture
```
delta_algo/
├── api/
│   └── client.py          # Delta Exchange REST client (auth, retry, rate limits)
├── strategies/
│   ├── base_strategy.py   # Abstract base
│   ├── short_strangle.py  # Short Strangle implementation
│   ├── iron_condor.py     # Iron Condor implementation
│   └── strategy_factory.py
├── risk/
│   └── risk_manager.py    # Position sizing, circuit breakers, daily loss limits
├── utils/
│   ├── options_math.py    # B-S pricing, delta, IV percentile, ATR
│   ├── config_loader.py
│   ├── logger.py
│   └── trade_logger.py    # CSV trade log
├── backtest/
│   └── engine.py          # Full backtesting engine with plots
├── live/
│   ├── live_trader.py     # Main trading loop
│   └── heartbeat.py       # Deadman switch
├── config/config.yaml
├── run_live.py            # Master live script
├── run_backtest.py        # Backtest script
└── requirements.txt
```

## Key Config Parameters
| Parameter | Default | Description |
|---|---|---|
| `strategy.target_delta` | 0.15 | Delta of short strikes |
| `strategy.iv_percentile_min` | 40 | Min IV% to enter |
| `exit.profit_target_pct` | 0.35 | Close at 35% profit |
| `exit.stop_loss_multiple` | 1.5 | Stop if premium 1.5x |
| `risk.max_risk_per_trade_pct` | 0.02 | 2% portfolio risk/trade |
| `risk.max_consecutive_losses` | 3 | Halt after 3 losses |
| `risk.max_daily_loss_pct` | 0.05 | Halt if -5% daily |

## Safety Features
- **Heartbeat / Deadman Switch** — auto-cancels orders if algo crashes
- **Circuit breakers** — halt on consecutive losses or IV spike
- **Daily loss limit** — stops trading if daily PnL < -5%
- **DTE auto-close** — force-closes positions 1 day before expiry
- **Graceful shutdown** — SIGINT/SIGTERM cancels all orders cleanly
- **Rate limit tracking** — prevents 429 errors

## Deploying on Antigravity
```bash
# Install deps
pip install -r requirements.txt

# Set env vars (optional, overrides config.yaml)
export DELTA_API_KEY=your_key
export DELTA_API_SECRET=your_secret

# Run with nohup
nohup python run_live.py > logs/nohup.out 2>&1 &

# Or with systemd (recommended)
# See systemd/delta-algo.service
```

## Logs
- `logs/algo_YYYYMMDD.log` — rotating daily log
- `logs/trades.csv` — all trade entries/exits
- `reports/` — backtest equity curves + plots
