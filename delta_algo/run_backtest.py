#!/usr/bin/env python3
"""
run_backtest.py — Backtesting Script
=====================================
Usage:
    python run_backtest.py
    python run_backtest.py --underlying BTC --start 2024-01-01 --end 2024-12-31
    python run_backtest.py --synthetic   (use synthetic data, no API needed)
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    parser = argparse.ArgumentParser(description="Delta Exchange Options Algo — Backtest")
    parser.add_argument("--config",     default="config/config.yaml")
    parser.add_argument("--underlying", default="BTC", choices=["BTC","ETH"])
    parser.add_argument("--start",      default=None)
    parser.add_argument("--end",        default=None)
    parser.add_argument("--synthetic",  action="store_true", help="Use synthetic data (no API)")
    parser.add_argument("--no-plots",   action="store_true")
    args = parser.parse_args()

    from utils.config_loader import load_config
    from utils.logger import setup_logger

    # Allow running without real API keys for backtest
    cfg = None
    try:
        cfg = load_config(args.config)
    except ValueError:
        # Load raw without validation for backtest
        import yaml
        with open(args.config) as f:
            cfg = yaml.safe_load(f)
        print("⚠️  Running backtest with placeholder credentials (synthetic data mode)")
        args.synthetic = True

    setup_logger("delta_algo", cfg.get("logging", {}))

    if args.start: cfg["backtest"]["start_date"] = args.start
    if args.end:   cfg["backtest"]["end_date"]   = args.end

    client = None
    if not args.synthetic:
        try:
            from api.client import DeltaClient
            client = DeltaClient.from_config(cfg)
            if not client.ping():
                print("⚠️  API not reachable, using synthetic data"); client = None
        except Exception as e:
            print(f"⚠️  API error ({e}), using synthetic data"); client = None

    from backtest.engine import BacktestEngine
    engine = BacktestEngine(cfg, client)

    print(f"\n{'='*60}")
    print(f"BACKTEST: {args.underlying} | {cfg['backtest']['start_date']} → {cfg['backtest']['end_date']}")
    print(f"Strategy: {cfg['strategy']['name']}")
    print(f"Capital:  ${cfg['backtest']['initial_capital']:,}")
    print(f"{'='*60}\n")

    metrics = engine.run(underlying=args.underlying)

    if not metrics or "error" in metrics:
        print(f"❌ Backtest failed: {metrics}"); return

    print(f"\n{'='*60}")
    print(f"RESULTS SUMMARY:")
    print(f"  Total Return:   {metrics['total_return_pct']}%")
    print(f"  Win Rate:       {metrics['win_rate_pct']}%")
    print(f"  Sharpe Ratio:   {metrics['sharpe_ratio']}")
    print(f"  Max Drawdown:   {metrics['max_drawdown_pct']}%")
    print(f"  Profit Factor:  {metrics['profit_factor']}")
    print(f"  Total Trades:   {metrics['total_trades']}")
    print(f"  Final Capital:  ${metrics['final_capital']:,.2f}")
    print(f"{'='*60}\n")

    equity_f, trades_f = engine.save_results(metrics)
    print(f"📁 Results saved: {equity_f}")
    print(f"📁 Trades saved:  {trades_f}")

    if not args.no_plots:
        plot_f = engine.plot_results()
        if plot_f: print(f"📊 Plot saved:    {plot_f}")

if __name__ == "__main__":
    main()
