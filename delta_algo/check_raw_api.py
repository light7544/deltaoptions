import requests
import json
url = "https://api.india.delta.exchange/v2/tickers"
params = {"contract_types": "call_options", "underlying_asset_symbols": "BTC"}
res = requests.get(url, params=params)
data = res.json()
if "result" in data and len(data["result"]) > 0:
    print(json.dumps(data["result"][0], indent=2))
else:
    print("No data")
