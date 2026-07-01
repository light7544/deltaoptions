import os
import sys
import time
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config_loader import load_config
from api.client import DeltaClient
from fetch_option_chain import get_spot_price, get_option_products, get_expiry_dates, get_option_chain, display_option_chain

app = FastAPI(title="Delta Algo Dashboard")

# Load Config and Client
try:
    cfg = load_config()
    env_str = str(cfg["exchange"]["environment"]).lower()
    base_url = cfg["exchange"]["base_url_production"] if env_str in ("production", "live") else cfg["exchange"]["base_url_testnet"]
    client = DeltaClient.from_config(cfg)
except Exception as e:
    print(f"Warning: Could not load config/credentials. Protected endpoints will fail. ({e})")
    base_url = "https://cdn-ind.testnet.deltaex.org"
    client = DeltaClient(base_url, "", "")

# Mount Static Files
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def read_root():
    index_path = os.path.join(static_dir, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse(content="<h1>Dashboard UI Not Found</h1><p>Please create static/index.html</p>")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/account")
def get_account():
    try:
        bals = client.get_wallet_balances()
        return {"status": "success", "data": bals}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/positions")
def get_positions():
    try:
        pos = client.get_all_positions()
        return {"status": "success", "data": pos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/orders")
def get_orders():
    try:
        orders = client.get_active_orders()
        return {"status": "success", "data": orders}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/option-chain/{underlying}")
def get_chain(underlying: str):
    spot = get_spot_price(base_url, underlying)
    products = get_option_products(base_url, underlying)
    if not products:
        raise HTTPException(status_code=404, detail="No products found")
    
    expiries = get_expiry_dates(products)
    if not expiries:
        raise HTTPException(status_code=404, detail="No future expiries found")
        
    weekly = [e for e in expiries if 2 <= e["dte"] <= 8]
    selected = weekly[0] if weekly else expiries[0]
    
    chain = get_option_chain(base_url, underlying, selected["api_format"])
    if not chain:
        raise HTTPException(status_code=404, detail="No chain data for this expiry")
        
    # display_option_chain prints to stdout but also returns the structured dict
    chain_obj = display_option_chain(chain, spot, underlying, selected["display"])
    
    return {
        "status": "success",
        "data": {
            "spot": spot,
            "expiry": selected["display"],
            "dte": selected["dte"],
            "calls": chain_obj.get("calls", {}),
            "puts": chain_obj.get("puts", {}),
            "strikes": chain_obj.get("strikes", [])
        }
    }

@app.get("/api/signals")
def get_signals():
    try:
        import os
        log_file = "logs/delta_algo.log"
        if not os.path.exists(log_file):
            return {"status": "success", "data": [{"time": "Now", "message": "Bot is pending/No logs yet."}]}
        signals = []
        with open(log_file, "r", encoding="utf-8") as f:
            for line in reversed(f.readlines()[-200:]):
                if any(x in line for x in ["Scanning", "skipped", "EXIT SIGNAL", "Entry result"]):
                    parts = line.strip().split(" - ")
                    try:
                        timestamp = parts[0].split(",")[0]
                        msg = parts[-1].replace("INFO - ", "").replace("WARNING - ", "")
                        signals.append({"time": timestamp, "message": msg})
                    except: continue
                if len(signals) >= 5: break
        if not signals:
            signals.append({"time": "Now", "message": "Waiting for strategy signals..."})
        return {"status": "success", "data": signals}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/api/liquidity-zones")
def get_liquidity_zones():
    """Fetch global BTC/USDT L2 orderbook to spot massive resting liquidity walls/icebergs"""
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get("https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=50", headers=headers, timeout=5)
        if res.status_code == 200:
            return {"status": "success", "data": res.json()}
        return {"status": "error", "detail": "Upstream error"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/api/chart-data/{symbol}")
def get_chart_data(symbol: str):
    try:
        resolution = "15m"
        end = int(time.time())
        start = end - (200 * 15 * 60)
        res = client.get_ohlc(symbol, resolution, start, end)
        if not res:
            return {"status": "error", "detail": "No candle data"}
            
        candles = []
        for c in res:
            try:
                # Delta API returns time conceptually or strings
                candles.append({
                    "time": int(c["time"]) if isinstance(c["time"], (int, float)) or str(c["time"]).isdigit() else c["time"], 
                    "open": float(c["open"]), 
                    "high": float(c["high"]), 
                    "low": float(c["low"]), 
                    "close": float(c["close"])
                })
            except: pass
            
        if not candles:
             return {"status": "error", "detail": "Error parsing candle data"}
             
        closes = [c["close"] for c in candles]
        latest_close = closes[-1]
        
        def ema(data, period):
            k = 2 / (period + 1)
            ema_data = [data[0]]
            for price in data[1:]:
                ema_data.append(price * k + ema_data[-1] * (1 - k))
            return ema_data
            
        ema9 = ema(closes, 9)
        ema20 = ema(closes, 20)
        
        diff = ema9[-1] - ema20[-1]
        trend = 1 if diff > 0 else -1
        if abs(diff) / latest_close < 0.001:
            trend = 0
            
        tr_list = []
        for i in range(1, len(candles)):
            c = candles[i]
            prev_c = candles[i-1]
            tr = max(c["high"] - c["low"], abs(c["high"] - prev_c["close"]), abs(c["low"] - prev_c["close"]))
            tr_list.append(tr)
        atr = sum(tr_list[-14:]) / 14 if len(tr_list) >= 14 else (tr_list[-1] if tr_list else 100)
        
        signals = {}
        if trend == 1:
            signals = {"T1": latest_close + atr * 1.5, "T2": latest_close + atr * 2.5, "T3": latest_close + atr * 4.0, "SL": latest_close - atr * 1.5}
        elif trend == -1:
            signals = {"T1": latest_close - atr * 1.5, "T2": latest_close - atr * 2.5, "T3": latest_close - atr * 4.0, "SL": latest_close + atr * 1.5}
        else:
            signals = {"T1": latest_close + atr * 1.5, "T2": latest_close + atr * 2.5, "T3": latest_close - atr * 2.5, "SL": latest_close - atr * 1.0}
            
        return {"status": "success", "data": {"candles": candles, "signals": signals, "trend": trend}}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
