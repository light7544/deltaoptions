import os, sys
# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.client import DeltaClient
from utils.config_loader import load_config
from dotenv import load_dotenv

def main():
    load_dotenv()
    cfg = load_config()
    client = DeltaClient.from_config(cfg)
    
    print("--- COMPREHENSIVE POSITION DIAGNOSTIC ---")
    
    try:
        # 1. Margined Positions (Options/Futures)
        print("\nChecking Margined Positions (Options & Futures)...")
        margined = client.get_all_positions()
        print(f"Raw Response: {margined}")
        if margined:
            p_list = margined if isinstance(margined, list) else [margined]
            for p in p_list:
                print(f"  [{p.get('product_symbol')}] Size: {p.get('size')} | Entry: {p.get('entry_price')} | Mark: {p.get('mark_price')}")
        else:
            print("  No margined positions found.")

        # 2. Wallet Balances
        print("\nChecking Wallet Balances...")
        balances = client.get_wallet_balances()
        for b in (balances if isinstance(balances, list) else [balances]):
            if float(b.get("available_balance", 0)) > 0:
                print(f"  {b.get('asset_symbol')}: {b.get('available_balance')}")

    except Exception as e:
        print(f"\nERROR: Diagnostic failed: {e}")

if __name__ == "__main__":
    main()
