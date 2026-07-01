#!/usr/bin/env python3
"""
run_live.py — Master Live Trading Script
=========================================
Usage:
    python run_live.py
    python run_live.py --config config/config.yaml
    python run_live.py --emergency-stop   (cancel all + close all)
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    parser = argparse.ArgumentParser(description="Delta Exchange Options Algo — Live Trading")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config file")
    parser.add_argument("--emergency-stop", action="store_true", help="Cancel all orders + close all positions")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not place orders")
    args = parser.parse_args()

    # Basic validation
    if not os.path.exists(args.config):
        print(f"❌ Config file not found: {args.config}")
        print("   Copy config/config.yaml and fill in your API keys.")
        sys.exit(1)

    from utils.config_loader import load_config
    try:
        cfg = load_config(args.config)
    except ValueError as e:
        print(f"❌ Config error: {e}")
        sys.exit(1)

    from utils.logger import setup_logger
    setup_logger("delta_algo", cfg.get("logging", {}))

    if args.emergency_stop:
        print("🚨 EMERGENCY STOP: Cancelling all orders and closing all positions...")
        from api.client import DeltaClient
        client = DeltaClient.from_config(cfg)
        try: client.cancel_all_orders()
        except Exception as e: print(f"Cancel orders error: {e}")
        try: client.close_all_positions()
        except Exception as e: print(f"Close positions error: {e}")
        print("Done.")
        return

    if args.dry_run:
        print("🔍 DRY RUN mode — scanning only, no orders will be placed")
        # Monkey-patch client to block order placement
        from api import client as client_mod
        orig = client_mod.DeltaClient.place_order
        def fake_order(*a, **kw): print(f"  [DRY RUN] place_order blocked: {kw}"); return {"id":0,"state":"dry_run"}
        client_mod.DeltaClient.place_order = fake_order

    from live.live_trader import LiveTrader
    print(f"🚀 Starting Delta Exchange Options Algo")
    print(f"   Config:      {args.config}")
    print(f"   Environment: {cfg['exchange']['environment']}")
    print(f"   Strategy:    {cfg['strategy']['name']}")
    print(f"   Underlyings: {cfg['strategy']['active_underlyings']}")
    print()

    if cfg["exchange"]["environment"] == "production":
        confirm = input("⚠️  PRODUCTION MODE. Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("Aborted."); return

    trader = LiveTrader(args.config)
    trader.start()

if __name__ == "__main__":
    main()
