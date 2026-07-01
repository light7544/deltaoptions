import os, sys
from utils.config_loader import load_config
from api.client import DeltaClient
from risk.risk_manager import RiskManager
from utils.trade_logger import TradeLogger
from strategies.short_strangle import ShortStrangleStrategy

def main():
    cfg_path = "config/config.yaml"
    if not os.path.exists(cfg_path):
        print("Config not found")
        return

    cfg = load_config(cfg_path)
    # Ensure production environment if .env says so, but we'll just use the client's internal logic
    client = DeltaClient.from_config(cfg)
    risk = RiskManager(cfg)
    tlog = TradeLogger("logs/temp_trades.csv")
    
    strategy = ShortStrangleStrategy(client, cfg, risk, tlog)
    
    print("--- SCANNING FOR SIGNALS (DRY RUN) ---")
    for symbol in ["BTC", "ETH"]:
        print(f"Scanning {symbol}...")
        setups = strategy.scan_entries(symbol)
        if not setups:
            print(f"  No valid setups found for {symbol} at this time.")
        for s in setups:
            print(f"  [SIGNAL] {symbol} DTE={s['dte']} Net Credit=${s['net_credit']:.2f} IV%={s['iv_percentile']:.1f}%")
            print(f"    Call: {s['call']['symbol']} (Delta: {s['call']['greeks']['delta']})")
            print(f"    Put:  {s['put']['symbol']} (Delta: {s['put']['greeks']['delta']})")

if __name__ == "__main__":
    main()
