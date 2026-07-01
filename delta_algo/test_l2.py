import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from utils.config_loader import load_config
from api.client import DeltaClient

try:
    cfg = load_config()
    client = DeltaClient.from_config(cfg)
    
    products = client.get_products("perpetual_futures")
    btc_perps = [p for p in products if p.get("underlying_asset_symbol") == "BTC"]
    
    if not btc_perps:
        print("No BTC perps found.")
        sys.exit()
        
    top_perp = btc_perps[0]
    symbol = top_perp["symbol"]
    print(f"Testing L2 for {symbol}...")
    
    r = client._request("GET", f"/v2/l2orderbook/{symbol}", auth=False)
    
    if isinstance(r, dict) and "result" in r:
        res = r["result"]
        print("Success! Orderbook sample:")
        print("Buy (Bids):", len(res.get("buy", [])))
        print("Sell (Asks):", len(res.get("sell", [])))
        print(json.dumps(res, indent=2)[:500])
    else:
        print(json.dumps(r, indent=2)[:500])
        
except Exception as e:
    print("Error:", e)
