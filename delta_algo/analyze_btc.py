import sys
import io
import time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from utils.config_loader import load_config
from api.client import DeltaClient

try:
    cfg = load_config()
    client = DeltaClient.from_config(cfg)
    
    resolution = "15m"
    end = int(time.time())
    start = end - (200 * 15 * 60) # last 200 candles
    
    print(f"Fetching {resolution} candles for BTCUSD...")
    res = client.get_ohlc("BTCUSD", resolution, start, end)
    
    if not res:
        print("No candle data returned.")
        sys.exit()
        
    candles = res
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    
    latest_close = closes[-1]
    
    def ema(data, period):
        k = 2 / (period + 1)
        ema_data = [data[0]]
        for price in data[1:]:
            ema_data.append(price * k + ema_data[-1] * (1 - k))
        return ema_data
        
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    
    print(f"\n📊 BTCUSD Analysis ({resolution} chart):")
    print(f"Current Price : {latest_close:,.2f}")
    print(f"9 EMA         : {ema9[-1]:,.2f}")
    print(f"20 EMA        : {ema20[-1]:,.2f}")
    
    diff = ema9[-1] - ema20[-1]
    trend = "BULLISH 🟢" if diff > 0 else "BEARISH 🔴"
    if abs(diff) / latest_close < 0.001:
        trend = "NEUTRAL / CHOPPY 🟡"
        
    print(f"Short-Term    : {trend}")
    
    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])
    range_pct = (recent_high - recent_low) / recent_low * 100
    
    print(f"Recent Range  : {recent_low:,.2f} - {recent_high:,.2f} ({range_pct:.2f}%)")
    
    print("\n==============================================")
    if range_pct < 1.5 or trend == "NEUTRAL / CHOPPY 🟡":
        print("💡 Trade Thesis: CONSOLIDATING")
        print("Price is ranging tightly. This is an optimal environment for a ")
        print("SHORT STRANGLE (Selling safely wide OTM Calls and Puts to collect theta decay).")
        print("You can execute the 0.15 Delta strangle outputted by your chain scanner.")
    elif "BULLISH" in trend:
        print("💡 Trade Thesis: TRENDING UP")
        print("Price has solid momentum above the 20 EMA.")
        print("Selling OTM Puts (Bull Put Spread) or taking Naked Calls offers the highest probability.")
        print("Avoid selling Calls right now to prevent steamrolling.")
    else:
        print("💡 Trade Thesis: TRENDING DOWN")
        print("Price is slipping under the 20 EMA.")
        print("Selling OTM Calls (Bear Call Spread) or taking Naked Puts offers the best RR.")
        print("Avoid selling Puts until a base is established.")
    print("==============================================\n")
        
except Exception as e:
    print("Error:", e)
