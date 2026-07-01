"""
Options Math Utilities
======================
Black-Scholes pricing, delta calculation, IV percentile,
ATR regime detection — all pure numpy/pandas, no external libs needed.
"""
import math, numpy as np, pandas as pd
from typing import Optional

# ── Black-Scholes ────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_price(S, K, T, r, sigma, option_type="call") -> float:
    """Black-Scholes option price. T in years."""
    if T <= 0: return max(0, (S-K) if option_type=="call" else (K-S))
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r*T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r*T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def bs_delta(S, K, T, r, sigma, option_type="call") -> float:
    """Black-Scholes delta."""
    if T <= 0: return (1.0 if S>K else 0.0) if option_type=="call" else (-1.0 if S<K else 0.0)
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma * math.sqrt(T))
    if option_type == "call": return _norm_cdf(d1)
    else: return _norm_cdf(d1) - 1.0

def implied_volatility(market_price, S, K, T, r, option_type="call",
                       tol=1e-6, max_iter=100) -> Optional[float]:
    """Newton-Raphson IV solver."""
    if T <= 0 or market_price <= 0: return None
    sigma = 0.3  # initial guess
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        # vega
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma * math.sqrt(T))
        vega = S * math.exp(-0.5*d1**2) / math.sqrt(2*math.pi) * math.sqrt(T)
        if abs(vega) < 1e-10: break
        sigma_new = sigma - (price - market_price) / vega
        if sigma_new <= 0: sigma_new = 1e-6
        if abs(sigma_new - sigma) < tol: return sigma_new
        sigma = sigma_new
    return sigma if 0 < sigma < 10 else None

# ── IV Percentile ────────────────────────────────────────────

def iv_percentile(iv_series: pd.Series, current_iv: float) -> float:
    """
    Returns IV percentile rank (0–100).
    current_iv is where today's IV sits relative to historical distribution.
    """
    if len(iv_series) == 0: return 50.0
    pct = (iv_series < current_iv).sum() / len(iv_series) * 100
    return float(pct)

# ── ATR Regime Detection ─────────────────────────────────────

def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period=14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def is_high_volatility_regime(atr_series: pd.Series, multiple: float = 2.5) -> bool:
    """Returns True if current ATR is spike (multiple x normal)."""
    if len(atr_series) < 20: return False
    current_atr = atr_series.iloc[-1]
    baseline_atr = atr_series.iloc[-20:].mean()
    return current_atr > multiple * baseline_atr

def moving_average(close: pd.Series, period: int = 20) -> pd.Series:
    return close.rolling(period).mean()

def trend_direction(close: pd.Series, ma_period=20) -> str:
    """Returns 'bullish', 'bearish', or 'neutral'."""
    if len(close) < ma_period: return "neutral"
    ma = moving_average(close, ma_period)
    price = close.iloc[-1]
    ma_val = ma.iloc[-1]
    if pd.isna(ma_val): return "neutral"
    pct_diff = (price - ma_val) / ma_val
    if pct_diff > 0.01: return "bullish"
    if pct_diff < -0.01: return "bearish"
    return "neutral"

# ── Strike Selection ─────────────────────────────────────────

def find_strike_by_delta(
    option_chain: list,
    target_delta: float,
    option_type: str,   # "call" or "put"
    tolerance: float = 0.03,
) -> Optional[dict]:
    """
    Find the option in chain closest to target_delta.
    Calls have positive delta, puts negative — pass absolute values.
    """
    best = None; best_diff = float("inf")
    for opt in option_chain:
        greeks = opt.get("greeks", {})
        if not greeks: continue
        delta_val = abs(float(greeks.get("delta", 0)))
        diff = abs(delta_val - target_delta)
        contract_type = opt.get("contract_type", "")
        if option_type == "call" and "call" not in contract_type: continue
        if option_type == "put" and "put" not in contract_type: continue
        if diff < best_diff:
            best_diff = diff; best = opt
    if best and best_diff <= tolerance: return best
    return best  # return closest even if outside tolerance

def days_to_expiry(symbol: str) -> int:
    """
    Parse DTE from Delta option symbol.
    Format: C-BTC-90000-310125 → expiry 31/01/2025
    """
    import re
    from datetime import date
    m = re.search(r"-(\d{6})$", symbol)
    if not m: return 999
    date_str = m.group(1)  # DDMMYY
    try:
        d = int(date_str[:2]); mo = int(date_str[2:4]); yr = 2000 + int(date_str[4:])
        exp = date(yr, mo, d)
        return max(0, (exp - date.today()).days)
    except: return 999

def mid_price(quotes: dict) -> Optional[float]:
    """Calculate mid price from bid/ask quotes."""
    bid = quotes.get("best_bid"); ask = quotes.get("best_ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return None

def bid_ask_spread_pct(quotes: dict) -> float:
    """Bid-ask spread as percentage of mid price."""
    bid = quotes.get("best_bid"); ask = quotes.get("best_ask")
    if not bid or not ask: return 1.0
    mid = (float(bid) + float(ask)) / 2
    if mid == 0: return 1.0
    return (float(ask) - float(bid)) / mid
