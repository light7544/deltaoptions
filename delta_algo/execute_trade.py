import os, sys, time
import logging
from utils.config_loader import load_config
from api.client import DeltaClient
from risk.risk_manager import RiskManager
from utils.trade_logger import TradeLogger
from strategies.short_strangle import ShortStrangleStrategy

# Set up logging to console for this script
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("execute_trade")

def main():
    cfg_path = "config/config.yaml"
    if not os.path.exists(cfg_path):
        print("Config not found")
        return

    cfg = load_config(cfg_path)
    client = DeltaClient.from_config(cfg)
    risk = RiskManager(cfg)
    tlog = TradeLogger("logs/trades.csv")
    
    strategy = ShortStrangleStrategy(client, cfg, risk, tlog)
    
    print("--- LIVE EXECUTION: BTC SHORT STRANGLE ---")
    underlying = "BTC"
    
    # 1. Scan for the specific setup we discussed
    setups = strategy.scan_entries(underlying)
    if not setups:
        print("No valid setups found during execution scan.")
        return
    
    setup = setups[0]
    print(f"Executing: {setup['call']['symbol']} and {setup['put']['symbol']}")
    
    # 2. Check balance
    balances = client.get_wallet_balances()
    usd_balance = 0
    for b in (balances if isinstance(balances, list) else [balances]):
        if b.get("asset_symbol") == "USD":
            usd_balance = float(b.get("available_balance", 0))
            break
    
    print(f"Current USD Balance: ${usd_balance:.2f}")
    
    # 3. Size the trade. We'll use the minimum possible (1 contract) since balance is low
    # The strategy.execute_entry usually calculates size based on risk, 
    # but we will force size=1 for this manual execution attempt.
    
    from uuid import uuid4
    trade_id = str(uuid4())[:12]
    
    print(f"Placing orders for 1 contract each (Minimum Size)...")
    
    try:
        # Place CALL (Sell)
        # Delta API might use 'id' instead of 'product_id' in some objects
        call_pid = setup["call"].get("id") or setup["call"].get("product_id")
        call_price = float(setup["call"]["quotes"].get("best_bid", setup["call"]["mark_price"]))
        print(f"Selling {setup['call']['symbol']} at {call_price}...")
        call_order = client.place_order(
            product_id=call_pid,
            size=1,
            side="sell",
            order_type="limit_order",
            limit_price=str(call_price),
            time_in_force="gtc"
        )
        print(f"  Call Order ID: {call_order.get('id')}")
        
        # Place PUT (Sell)
        put_pid = setup["put"].get("id") or setup["put"].get("product_id")
        put_price = float(setup["put"]["quotes"].get("best_bid", setup["put"]["mark_price"]))
        print(f"Selling {setup['put']['symbol']} at {put_price}...")
        put_order = client.place_order(
            product_id=put_pid,
            size=1,
            side="sell",
            order_type="limit_order",
            limit_price=str(put_price),
            time_in_force="gtc"
        )
        print(f"  Put Order ID: {put_order.get('id')}")
        
        print("\nSUCCESS: Trade Execution Completed successfully (Orders submitted).")
        print("Check your dashboard to monitor the position.")
        
    except Exception as e:
        print(f"\nERROR: Execution failed: {str(e).encode('ascii', 'ignore').decode()}")

if __name__ == "__main__":
    main()
