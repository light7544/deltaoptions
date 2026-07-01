"""
Backtesting Engine
==================
Simulates the short strangle strategy on historical option chain data.
Since Delta doesn't provide historical options chain, we synthesize
option prices from OHLC data using Black-Scholes.
"""
import logging, os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

from utils.options_math import (bs_price, bs_delta, calculate_atr,
    is_high_volatility_regime, iv_percentile, days_to_expiry, trend_direction)

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Simulates short strangle selling on BTC/ETH using synthetic options.
    Pulls historical OHLC from Delta API, synthesizes option chains.
    """

    def __init__(self, cfg: dict, client=None):
        self.cfg = cfg
        self.client = client
        b = cfg["backtest"]
        self.start_date     = b["start_date"]
        self.end_date       = b["end_date"]
        self.initial_capital= b["initial_capital"]
        self.commission_rate= b["commission_per_contract"]
        self.slippage_pct   = b["slippage_pct"]
        self.data_dir       = b["data_dir"]
        s = cfg["strategy"]
        self.target_delta   = s["target_delta"]
        self.profit_target  = cfg["exit"]["profit_target_pct"]
        self.sl_multiple    = cfg["exit"]["stop_loss_multiple"]
        self.min_dte        = cfg["instruments"]["min_dte"]
        self.max_dte        = cfg["instruments"]["max_dte"]
        self.iv_pct_min     = s["iv_percentile_min"]
        self.risk_per_trade = cfg["risk"]["max_risk_per_trade_pct"]
        self.force_close_dte= cfg["exit"]["max_dte_to_force_close"]
        os.makedirs(self.data_dir, exist_ok=True)

        # Results tracking
        self.equity_curve: list = []
        self.trades: list = []
        self.capital = float(self.initial_capital)
        self.peak_capital = float(self.initial_capital)

    # ── Data Loading ─────────────────────────────────────────

    def load_ohlc(self, underlying: str, resolution="1d") -> pd.DataFrame:
        """Load OHLC — from cache or Delta API."""
        cache_file = os.path.join(self.data_dir, f"{underlying}_{resolution}.csv")
        if os.path.exists(cache_file):
            logger.info(f"Loading cached OHLC: {cache_file}")
            df = pd.read_csv(cache_file)
            df["time"] = pd.to_datetime(df["time"], unit="s") if df["time"].dtype != "O" else pd.to_datetime(df["time"])
            return df

        if self.client is None:
            logger.warning(f"No client, generating synthetic data for {underlying}")
            return self._generate_synthetic_ohlc(underlying)

        try:
            sym = f"{underlying}USD"
            start_ts = int(datetime.strptime(self.start_date, "%Y-%m-%d").timestamp())
            end_ts   = int(datetime.strptime(self.end_date,   "%Y-%m-%d").timestamp())
            candles  = self.client.get_ohlc(sym, resolution, start_ts, end_ts)
            df = pd.DataFrame(candles)
            df.columns = [c.lower() for c in df.columns]
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df.to_csv(cache_file, index=False)
            logger.info(f"Saved OHLC to {cache_file}: {len(df)} rows")
            return df
        except Exception as e:
            logger.warning(f"OHLC fetch failed: {e}. Using synthetic data.")
            return self._generate_synthetic_ohlc(underlying)

    def _generate_synthetic_ohlc(self, underlying: str) -> pd.DataFrame:
        """Generate realistic synthetic BTC/ETH price data for testing."""
        np.random.seed(42)
        start = pd.to_datetime(self.start_date); end = pd.to_datetime(self.end_date)
        dates = pd.date_range(start, end, freq="D")
        n = len(dates)
        s0 = 40000 if underlying == "BTC" else 2500
        returns = np.random.normal(0.0005, 0.025, n)
        prices = s0 * np.exp(np.cumsum(returns))
        df = pd.DataFrame({"time": dates,"open":prices,"close":prices*np.exp(np.random.normal(0,0.005,n)),
                           "high":prices*np.exp(np.abs(np.random.normal(0,0.015,n))),
                           "low":prices*np.exp(-np.abs(np.random.normal(0,0.015,n))),
                           "volume":np.random.uniform(1e8,1e9,n)})
        for col in ["open","high","low","close"]:
            df[col] = df[col].clip(lower=100)
        return df

    # ── Option Chain Simulation ──────────────────────────────

    def _get_iv(self, df: pd.DataFrame, idx: int, period=20) -> float:
        """Calculate historical vol as IV proxy."""
        start = max(0, idx-period)
        sub = df.iloc[start:idx+1]
        if len(sub) < 5: return 0.60
        log_rets = np.log(sub["close"] / sub["close"].shift(1)).dropna()
        hv = float(log_rets.std() * np.sqrt(365))
        return max(0.20, min(hv * 1.3, 2.0))  # IV usually > HV

    def _find_target_strike(self, spot, T, r, sigma, option_type, target_delta) -> float:
        """Binary search for strike at given delta."""
        lo, hi = spot * 0.5, spot * 2.0
        for _ in range(50):
            mid = (lo + hi) / 2
            d = bs_delta(spot, mid, T, r, sigma, option_type)
            if option_type == "call":
                if d > target_delta: lo = mid
                else: hi = mid
            else:
                if abs(d) > target_delta: hi = mid
                else: lo = mid
            if abs(hi - lo) < 0.01: break
        return (lo + hi) / 2

    # ── Main Backtest Loop ───────────────────────────────────

    def run(self, underlying: str = "BTC") -> dict:
        logger.info(f"Starting backtest: {underlying} | {self.start_date} → {self.end_date}")
        df = self.load_ohlc(underlying)
        df = df.sort_values("time").reset_index(drop=True)

        # Filter date range
        df = df[(df["time"] >= self.start_date) & (df["time"] <= self.end_date)].reset_index(drop=True)
        if len(df) < 30:
            logger.error("Insufficient data for backtest"); return {}

        r = 0.05  # risk-free rate
        open_trades = []
        atr_s = calculate_atr(df["high"], df["low"], df["close"])

        for i in range(20, len(df)):
            row = df.iloc[i]
            today = row["time"]
            spot  = float(row["close"])
            self.equity_curve.append({"date": str(today.date()), "equity": round(self.capital, 2)})
            self.peak_capital = max(self.peak_capital, self.capital)

            # 1. Check exits for open trades
            remaining = []
            for trade in open_trades:
                t_days = (today - trade["entry_date"]).days
                T = max(0, (trade["expiry_date"] - today).total_seconds() / (365.25*86400))
                dte = int(T * 365.25)

                # Reprice current option values
                iv = self._get_iv(df, i)
                call_curr = bs_price(spot, trade["call_strike"], T, r, iv, "call")
                put_curr  = bs_price(spot, trade["put_strike"],  T, r, iv, "put")

                # Apply slippage on exit
                call_curr *= (1 + self.slippage_pct)
                put_curr  *= (1 + self.slippage_pct)

                entry_credit = trade["call_entry"] + trade["put_entry"]
                current_cost = call_curr + put_curr
                pnl_pct = (entry_credit - current_cost) / entry_credit if entry_credit > 0 else 0

                exit_reason = None
                if dte <= self.force_close_dte:
                    exit_reason = "dte_expiry"
                elif pnl_pct >= self.profit_target:
                    exit_reason = "profit_target"
                elif call_curr >= trade["call_entry"] * self.sl_multiple:
                    exit_reason = "call_stop"
                elif put_curr >= trade["put_entry"] * self.sl_multiple:
                    exit_reason = "put_stop"

                if exit_reason:
                    pnl_usd = (entry_credit - current_cost) * trade["size"]
                    comm = current_cost * trade["size"] * self.commission_rate * 2
                    net_pnl = pnl_usd - comm
                    self.capital += net_pnl
                    trade.update({"exit_date":today,"exit_reason":exit_reason,"pnl":net_pnl,"pnl_pct":pnl_pct})
                    self.trades.append(trade)
                    logger.debug(f"EXIT {today.date()} {exit_reason} PnL={net_pnl:.2f} ({pnl_pct:.1%})")
                else:
                    remaining.append(trade)
            open_trades = remaining

            # 2. Check entry conditions
            if len(open_trades) >= self.cfg["risk"]["max_open_positions"]:
                continue

            iv = self._get_iv(df, i)
            iv_hist_series = pd.Series([self._get_iv(df, j) for j in range(max(0,i-30), i)])
            iv_pct = iv_percentile(iv_hist_series, iv)

            if iv_pct < self.iv_pct_min:
                continue

            if is_high_volatility_regime(atr_s.iloc[:i+1], self.cfg["strategy"]["max_atr_multiple"]):
                continue

            # Select expiry ~7 days out
            expiry_date = today + timedelta(days=7)
            T = 7/365.25

            call_strike = self._find_target_strike(spot, T, r, iv, "call", self.target_delta)
            put_strike  = self._find_target_strike(spot, T, r, iv, "put",  self.target_delta)

            call_entry = bs_price(spot, call_strike, T, r, iv, "call") * (1 - self.slippage_pct)
            put_entry  = bs_price(spot, put_strike,  T, r, iv, "put")  * (1 - self.slippage_pct)
            net_credit = call_entry + put_entry

            if net_credit < self.cfg["entry"]["min_premium_usd"] / spot * 100:
                continue

            # Size
            max_loss_per = net_credit * self.sl_multiple
            risk_budget  = self.capital * self.risk_per_trade
            size = max(1, int(risk_budget / max(max_loss_per, 0.001)))

            # Commission
            comm = net_credit * size * self.commission_rate * 2
            net_credit_after = net_credit * size - comm

            open_trades.append({
                "underlying": underlying, "entry_date": today,
                "expiry_date": expiry_date, "spot_at_entry": spot,
                "call_strike": call_strike, "put_strike": put_strike,
                "call_entry": call_entry, "put_entry": put_entry,
                "net_credit": net_credit, "size": size, "iv": iv, "iv_percentile": iv_pct,
            })
            logger.debug(f"ENTRY {today.date()} spot={spot:.0f} credit={net_credit:.4f} size={size}")

        # Close remaining trades
        for trade in open_trades:
            last_row = df.iloc[-1]; spot = float(last_row["close"])
            iv = self._get_iv(df, len(df)-1)
            T = max(0.001, (trade["expiry_date"] - df.iloc[-1]["time"]).total_seconds()/(365.25*86400))
            call_curr = bs_price(spot, trade["call_strike"], T, r, iv, "call")
            put_curr  = bs_price(spot, trade["put_strike"],  T, r, iv, "put")
            pnl_usd = (trade["call_entry"] + trade["put_entry"] - call_curr - put_curr) * trade["size"]
            self.capital += pnl_usd
            trade.update({"exit_date": df.iloc[-1]["time"], "exit_reason":"end_of_data","pnl":pnl_usd})
            self.trades.append(trade)

        return self._compute_metrics(underlying)

    # ── Performance Metrics ──────────────────────────────────

    def _compute_metrics(self, underlying: str) -> dict:
        if not self.trades:
            logger.warning("No trades generated in backtest")
            return {"error": "no_trades"}

        df_t = pd.DataFrame(self.trades)
        pnls = df_t["pnl"].fillna(0)
        wins = (pnls > 0).sum(); losses = (pnls <= 0).sum()
        win_rate = wins / len(pnls) if len(pnls) > 0 else 0

        # Equity curve metrics
        df_e = pd.DataFrame(self.equity_curve)
        df_e["equity"] = df_e["equity"].astype(float)
        peak = df_e["equity"].cummax()
        drawdown = (df_e["equity"] - peak) / peak
        max_dd = float(drawdown.min())

        # Sharpe
        daily_returns = df_e["equity"].pct_change().dropna()
        sharpe = float(daily_returns.mean() / daily_returns.std() * np.sqrt(365)) if daily_returns.std() > 0 else 0

        total_return = (self.capital - self.initial_capital) / self.initial_capital

        metrics = {
            "underlying":        underlying,
            "start_date":        self.start_date,
            "end_date":          self.end_date,
            "initial_capital":   self.initial_capital,
            "final_capital":     round(self.capital, 2),
            "total_return_pct":  round(total_return * 100, 2),
            "total_trades":      len(self.trades),
            "win_rate_pct":      round(win_rate * 100, 2),
            "wins":              int(wins),
            "losses":            int(losses),
            "avg_win":           round(float(pnls[pnls>0].mean()), 4) if wins > 0 else 0,
            "avg_loss":          round(float(pnls[pnls<=0].mean()), 4) if losses > 0 else 0,
            "profit_factor":     round(float(pnls[pnls>0].sum() / abs(pnls[pnls<=0].sum())), 3) if losses > 0 else 99,
            "max_drawdown_pct":  round(max_dd * 100, 2),
            "sharpe_ratio":      round(sharpe, 3),
            "exit_reasons":      df_t["exit_reason"].value_counts().to_dict() if "exit_reason" in df_t else {},
        }
        logger.info(f"\n{'='*50}")
        logger.info(f"BACKTEST RESULTS: {underlying}")
        for k, v in metrics.items():
            logger.info(f"  {k:30s}: {v}")
        logger.info(f"{'='*50}")
        return metrics

    def save_results(self, metrics: dict, output_dir: str = "reports"):
        """Save equity curve + trades to CSV files."""
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        und = metrics.get("underlying","BTC")

        equity_file = os.path.join(output_dir, f"equity_{und}_{ts}.csv")
        trades_file = os.path.join(output_dir, f"trades_{und}_{ts}.csv")
        summary_file = os.path.join(output_dir, f"summary_{und}_{ts}.txt")

        pd.DataFrame(self.equity_curve).to_csv(equity_file, index=False)
        pd.DataFrame(self.trades).to_csv(trades_file, index=False)
        with open(summary_file, "w") as f:
            for k, v in metrics.items():
                f.write(f"{k}: {v}\n")

        logger.info(f"Results saved to {output_dir}/")
        return equity_file, trades_file

    def plot_results(self, output_dir: str = "reports"):
        """Generate performance plots."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(2, 2, figsize=(14, 10))
            fig.suptitle("Options Selling Strategy — Backtest Results", fontsize=14)

            df_e = pd.DataFrame(self.equity_curve)
            df_t = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()

            # Equity curve
            ax = axes[0, 0]
            ax.plot(df_e["date"], df_e["equity"], color="steelblue", linewidth=1.5)
            ax.axhline(y=self.initial_capital, color="gray", linestyle="--", alpha=0.5)
            ax.set_title("Equity Curve"); ax.set_xlabel("Date"); ax.set_ylabel("Capital ($)")
            ax.tick_params(axis='x', rotation=45)

            # Drawdown
            ax = axes[0, 1]
            eq = df_e["equity"].astype(float)
            dd = (eq - eq.cummax()) / eq.cummax() * 100
            ax.fill_between(range(len(dd)), dd, 0, color="red", alpha=0.4)
            ax.set_title("Drawdown (%)"); ax.set_xlabel("Day"); ax.set_ylabel("Drawdown %")

            # PnL distribution
            ax = axes[1, 0]
            if not df_t.empty and "pnl" in df_t.columns:
                pnls = df_t["pnl"].dropna()
                ax.hist(pnls, bins=30, color="steelblue", edgecolor="white", alpha=0.8)
                ax.axvline(0, color="red", linestyle="--")
                ax.set_title("Trade PnL Distribution"); ax.set_xlabel("PnL ($)")

            # Exit reasons
            ax = axes[1, 1]
            if not df_t.empty and "exit_reason" in df_t.columns:
                reasons = df_t["exit_reason"].value_counts()
                ax.bar(reasons.index, reasons.values, color=["green","orange","red","purple"])
                ax.set_title("Exit Reasons"); ax.tick_params(axis='x', rotation=30)

            plt.tight_layout()
            os.makedirs(output_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            plot_file = os.path.join(output_dir, f"backtest_plot_{ts}.png")
            plt.savefig(plot_file, dpi=120, bbox_inches="tight")
            plt.close()
            logger.info(f"Plot saved: {plot_file}")
            return plot_file
        except ImportError:
            logger.warning("matplotlib not installed — skipping plots")
            return None
        except Exception as e:
            logger.error(f"Plot error: {e}")
            return None
