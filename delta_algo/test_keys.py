import sys
import io
import json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from utils.config_loader import load_config
from api.client import DeltaClient

try:
    cfg = load_config("config/config.yaml")
    client = DeltaClient.from_config(cfg)
    print("✅ Config loaded successfully!")
    print("Environment:", cfg["exchange"]["environment"])
    
    # Test authentication
    bals = client.get_wallet_balances()
    print("✅ Authentication successful!")
    
    if isinstance(bals, list):
        for b in bals:
            balance = float(b.get("balance", "0"))
            if balance > 0:
                print(f"- {b.get('asset_symbol')}: {balance} (Available: {b.get('available_balance')})")
    elif isinstance(bals, dict):
        print("Balances Data:", json.dumps(bals, indent=2))
        
    # Get positions
    pos = client.get_all_positions()
    print("\n✅ Positions fetched successfully!")
    if pos:
        print(f"Found {len(pos)} open positions.")
        for p in pos:
            print(f"  {p.get('product_symbol')}: {p.get('size')} contracts")
    else:
        print("No open positions found.")
        
except Exception as e:
    print("\n❌ Error connecting to Delta Exchange:")
    print(str(e))
