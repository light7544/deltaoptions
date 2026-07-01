import sys
import io
import json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from utils.config_loader import load_config
from api.client import DeltaClient

try:
    cfg = load_config()
    client = DeltaClient.from_config(cfg)
    
    print("Locating active C-BTC-70400 option...")
    products = client.get_products("call_options")
    target = None
    for p in products:
        sym = p.get("symbol", "")
        if sym.startswith("C-BTC-70400"):
            target = p
            break
            
    if not target:
        print("❌ Could not find a match for C-BTC-70400.")
        sys.exit(1)
        
    product_id = target["id"]
    symbol = target["symbol"]
    print(f"Target: {symbol} | Product ID: {product_id}")
    
    # Get current best ask
    ticker = client.get_ticker(symbol)
    best_ask = ticker.get("quotes", {}).get("best_ask")
    print(f"Current Best Ask: {best_ask} USDT")
    
    if not best_ask or float(best_ask) <= 0:
        print("❌ Best Ask is 0 or unavailable.")
        sys.exit(1)
        
    size = 1 # Minimum contract size
    print(f"\nSubmitting LIMIT BUY order for {size} contract(s) at {best_ask} USDT...")
    
    res = client.place_order(
        product_id=product_id,
        size=size,
        side="buy", 
        order_type="limit_order",
        limit_price=best_ask
    )
    
    print("\n✅ Order successfully submitted!")
    print(json.dumps(res, indent=2))
    
except Exception as e:
    print(f"\n❌ Error submitting order: {e}")
