import os
import sys
import logging
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.client import DeltaClient
from utils.config_loader import load_config

# Setup Logging
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] RISK_ENGINE: %(message)s',
    handlers=[
        logging.FileHandler("logs/risk_management.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("risk_engine")

def calculate_sl_tp(entry_premium, tp_pct=0.35, sl_multiplier=1.8):
    tp_price = entry_premium * (1 - tp_pct)
    sl_price = entry_premium * sl_multiplier
    return round(tp_price, 2), round(sl_price, 2)

def place_protective_orders(client, product_id, size, entry_premium, symbol):
    abs_size = abs(float(size))
    tp_price, sl_price = calculate_sl_tp(float(entry_premium))
    
    logger.info(f"Setting protection for {symbol} (ID: {product_id}):")
    
    # 0. Check for existing orders to avoid duplicates
    try:
        active_orders = client.get_active_orders(product_ids=str(product_id))
        if active_orders:
            logger.info(f"Existing orders found for {symbol}. Skipping placement.")
            return
    except Exception as e:
        logger.warning(f"Could not fetch existing orders for {symbol}: {e}")

    try:
        # 1. Take Profit (Limit Buy)
        logger.info(f"Placing TP Limit Buy at {tp_price}...")
        tp_res = client.place_order(
            product_id=product_id,
            size=str(abs_size),
            side="buy",
            order_type="limit_order", 
            limit_price=str(tp_price),
            post_only=False,
            reduce_only=True
        )
        logger.info(f"TP Status: {tp_res.get('status', 'OK')} | ID: {tp_res.get('id', 'N/A')}")
    except Exception as e:
        logger.error(f"Failed to place TP for {symbol}: {e}")

    try:
        # 2. Stop Loss (Stop Limit Buy)
        logger.info(f"Placing SL Stop-Limit Buy at {sl_price}...")
        sl_res = client.place_order(
            product_id=product_id,
            size=str(abs_size),
            side="buy",
            order_type="limit_order",
            stop_order_type="stop_loss_order", 
            stop_price=str(sl_price),
            limit_price=str(sl_price * 1.1),
            post_only=False,
            reduce_only=True
        )
        logger.info(f"SL Status: {sl_res.get('status', 'OK')} | ID: {sl_res.get('id', 'N/A')}")
        
    except Exception as e:
        logger.error(f"Failed to place SL for {symbol}: {str(e)}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Delta Exchange Risk Management — Auto SL/TP")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not place orders")
    args = parser.parse_args()

    load_dotenv()
    cfg = load_config()
    client = DeltaClient.from_config(cfg)
    
    logger.info("--- Initiating Automated LIVE SL/TP Placement ---")
    if args.dry_run:
        logger.info("[DRY RUN] No orders will be placed.")

    try:
        # Fetch actual positions from the exchange
        raw_positions = client.get_all_positions()
        
        # Filter for short positions (size < 0)
        active_shorts = []
        all_positions = []
        for p in (raw_positions if isinstance(raw_positions, list) else [raw_positions]):
            size = float(p.get("size", 0))
            symbol = p.get("product_symbol")
            entry = float(p.get("entry_price", 0))
            pid = p.get("product_id")
            all_positions.append({"symbol": symbol, "size": size, "entry": entry})
            if size < 0:
                active_shorts.append({
                    "symbol": symbol,
                    "id": pid,
                    "size": size,
                    "entry": entry
                })
        
        if all_positions:
            logger.info("Current Positions:")
            for p in all_positions:
                logger.info(f"  {p['symbol']}: Size {p['size']} | Entry {p['entry']}")

        if not active_shorts:
            logger.info("No active short positions found to protect.")
            return

        logger.info(f"Found {len(active_shorts)} active short positions.")

        for pos in active_shorts:
            if args.dry_run:
                tp_price, sl_price = calculate_sl_tp(pos["entry"])
                logger.info(f"[DRY RUN] Would place protection for {pos['symbol']}: TP={tp_price}, SL={sl_price}")
            else:
                place_protective_orders(client, pos["id"], pos["size"], pos["entry"], pos["symbol"])
                
    except Exception as e:
        logger.error(f"Error fetching or processing positions: {e}")

if __name__ == "__main__":
    main()
