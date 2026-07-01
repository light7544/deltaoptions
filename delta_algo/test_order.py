import sys
import io
import json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from utils.config_loader import load_config
from api.client import DeltaClient

try:
    cfg = load_config()
    client = DeltaClient.from_config(cfg)
    
    print("Checking Authentication...")
    bal = client.get_wallet_balances()
    print("✅ API connection verified successfully!")
    
    print("Searching for C-BTC-70400 Option...")
    products = client.get_products("call_options")
    target = None
    for p in products:
        sym = p.get("symbol", "")
        # Prioritize the most recent / Weekly expiry by checking string or DTE. 
        # Delta returns live products. We just grab the first one that matches C-BTC-70400
        if sym.startswith("C-BTC-70400"):
            target = p
            break
            
    if not target:
        print("❌ Could not find an active 70400 Call.")
        sys.exit(1)
        
    product_id = target["id"]
    symbol = target["symbol"]
    print(f"✅ Found Target Option: {symbol} (ProductID: {product_id})")
    
    # Get ticker to find best ask
    ticker = client.get_ticker(symbol)
    best_ask = ticker.get("quotes", {}).get("best_ask")
    print(f"💰 Current Best Ask Price: {best_ask} USDT")
    print(f"⚠️ Recommendation: Submit Limit Order at {best_ask}")
    
except Exception as e:
    print("❌ Error:", e)
