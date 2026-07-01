"""
utils/helpers.py
─────────────────────────────────────────────────────────────────
Utility functions: config loading, logging setup, time helpers,
Black-Scholes Greeks, IV percentile calculation.
"""

import logging
import logging.handlers
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import yaml


# ══════════════════════════════════════════════════════════════════════
# CONFIG LOADER
# ══════════════════════════════════════════════════════════════════════

def load_config(path: str = "config/config.yaml") -> Dict[str, Any]:
    """Load and return YAML config, overriding with env vars if set."""
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    # Allow env-var overrides for secrets (safe for deployment)
    if os.environ.get("DELTA_API_KEY"):
        cfg["api"]["api_key"] = os.environ["DELTA_API_KEY"]
    if os.environ.get("DELTA_API_SECRET"):
        cfg["api"]["api_secret"] = os.environ["DELTA_API_SECRET"]
    if os.environ.get("DELTA_BASE_URL"):
        cfg["api"]["base_url"] = os.environ["DELTA_BASE_URL"]

    return cfg


# ══════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════

def setup_logging(config: Dict) -> logging.Logger:
    """Configure rotating file + console logging."""
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO"))
    log_dir = log_cfg.get("log_dir", "logs/")
    log_file = log_cfg.get("log_file", "algo.log")
    max_bytes = log_cfg.get("max_bytes", 10_485_760)
    backup_count = log_cfg.get("backup_count", 5)
    console = log_cfg.get("console_output", True)

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_file)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # File handler (rotating)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    logger = logging.getLogger("algo")
    logger.info(f"Logging initialized → {log_path}")
    return logger


# ══════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ══════════════════════════════════════════════════════════════════════

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_ts() -> int:
    """Current Unix timestamp in seconds."""
    return int(time.time())

def days_to_expiry(expiry_str: str) -> float:
    """
    Calculate DTE from symbol expiry string like '310125' (DDMMYY).
    Returns float days.
    """
    try:
        dt = datetime.strptime(expiry_str, "%d%m%y")
        dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = (dt - now).total_seconds() / 86400
        return max(diff, 0)
    except Exception:
        return 0.0

def parse_expiry_from_symbol(symbol: str) -> Optional[str]:
    """
    Extract expiry part from option symbol.
    e.g. C-BTC-90000-310125 → '310125'
    """
    parts = symbol.split("-")
    if len(parts) >= 4:
        return parts[-1]
    return None

def expiry_to_date_str(expiry_code: str) -> str:
    """Convert '310125' to '31-01-2025' (DD-MM-YYYY for Delta API)."""
    try:
        dt = datetime.strptime(expiry_code, "%d%m%y")
        return dt.strftime("%d-%m-%Y")
    except Exception:
        return ""

def is_within_trading_window(start_hhmm: str, end_hhmm: str) -> bool:
    """Check if current UTC time is within [start, end] trading window."""
    now = datetime.now(timezone.utc)
    current = now.hour * 60 + now.minute
    s_h, s_m = map(int, start_hhmm.split(":"))
    e_h, e_m = map(int, end_hhmm.split(":"))
    start = s_h * 60 + s_m
    end = e_h * 60 + e_m
    return start <= current <= end

def make_client_order_id(prefix: str = "opt") -> str:
    """Generate unique client_order_id (max 32 chars)."""
    ts = str(int(time.time() * 1000))[-10:]
    return f"{prefix}_{ts}"[:32]


# ══════════════════════════════════════════════════════════════════════
# BLACK-SCHOLES GREEKS
# ══════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def bs_delta(
    spot: float,
    strike: float,
    tte: float,       # time to expiry in years
    iv: float,        # annualized implied volatility (decimal)
    r: float = 0.0,   # risk-free rate
    option_type: str = "call",
) -> float:
    """Compute Black-Scholes delta."""
    if tte <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * iv**2) * tte) / (iv * math.sqrt(tte))
        if option_type.lower() == "call":
            return _norm_cdf(d1)
        else:
            return _norm_cdf(d1) - 1.0
    except Exception:
        return 0.0

def bs_price(
    spot: float,
    strike: float,
    tte: float,
    iv: float,
    r: float = 0.0,
    option_type: str = "call",
) -> float:
    """Black-Scholes option price."""
    if tte <= 0 or iv <= 0:
        return max(0, spot - strike) if option_type == "call" else max(0, strike - spot)
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * iv**2) * tte) / (iv * math.sqrt(tte))
        d2 = d1 - iv * math.sqrt(tte)
        if option_type.lower() == "call":
            return spot * _norm_cdf(d1) - strike * math.exp(-r * tte) * _norm_cdf(d2)
        else:
            return strike * math.exp(-r * tte) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════
# IV PERCENTILE
# ══════════════════════════════════════════════════════════════════════

def compute_iv_percentile(current_iv: float, historical_ivs: List[float]) -> float:
    """
    Compute IV percentile rank.
    Returns 0-100 indicating where current_iv sits vs history.
    """
    if not historical_ivs:
        return 50.0
    arr = np.array(historical_ivs)
    below = np.sum(arr < current_iv)
    return round(float(below) / len(arr) * 100, 1)

def compute_iv_percentile_from_ohlc(
    current_iv: float,
    candles: List[Dict],
    iv_field: str = "close",
) -> float:
    """Compute IV rank from OHLC candle data using close prices as IV proxy."""
    if not candles:
        return 50.0
    closes = [c[iv_field] for c in candles if iv_field in c]
    return compute_iv_percentile(current_iv, closes)


# ══════════════════════════════════════════════════════════════════════
# TREND / ATR
# ══════════════════════════════════════════════════════════════════════

def compute_sma(prices: List[float], period: int) -> Optional[float]:
    """Simple moving average."""
    if len(prices) < period:
        return None
    return float(np.mean(prices[-period:]))

def compute_atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> Optional[float]:
    """Average True Range."""
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return float(np.mean(trs[-period:]))

def is_high_volatility_regime(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    atr_period: int = 14,
    multiplier: float = 2.0,
) -> bool:
    """Return True if current ATR > multiplier * average ATR (high vol regime)."""
    if len(closes) < atr_period * 3:
        return False
    atr_now = compute_atr(highs, lows, closes, atr_period)
    atr_avg = compute_atr(highs, lows, closes[: -atr_period], atr_period)
    if atr_now is None or atr_avg is None or atr_avg == 0:
        return False
    return atr_now > multiplier * atr_avg

def get_trend_direction(closes: List[float], ma_period: int = 20) -> str:
    """
    Returns 'up', 'down', or 'neutral'.
    Up: current price > MA. Down: below MA.
    """
    if len(closes) < ma_period + 1:
        return "neutral"
    ma = compute_sma(closes, ma_period)
    if ma is None:
        return "neutral"
    current = closes[-1]
    diff_pct = (current - ma) / ma
    if diff_pct > 0.01:
        return "up"
    elif diff_pct < -0.01:
        return "down"
    return "neutral"


# ══════════════════════════════════════════════════════════════════════
# POSITION SIZING
# ══════════════════════════════════════════════════════════════════════

def calculate_position_size(
    available_balance: float,
    max_risk_pct: float,
    premium_per_contract: float,
    stop_loss_multiplier: float = 1.5,
    max_contracts: int = 5,
) -> int:
    """
    Calculate number of contracts to trade.
    
    Risk = (stop_loss_multiplier - 1) * premium * contracts
    Max risk = available_balance * max_risk_pct
    """
    if premium_per_contract <= 0 or available_balance <= 0:
        return 0
    max_loss_usd = available_balance * max_risk_pct
    risk_per_contract = (stop_loss_multiplier - 1) * premium_per_contract
    if risk_per_contract <= 0:
        return 0
    contracts = int(max_loss_usd / risk_per_contract)
    return min(max(contracts, 0), max_contracts)


# ══════════════════════════════════════════════════════════════════════
# OPTION CHAIN PARSING
# ══════════════════════════════════════════════════════════════════════

def parse_option_chain(tickers: List[Dict]) -> Dict:
    """
    Parse raw ticker list into structured option chain.
    Returns: {'calls': [...], 'puts': [...]}
    Each item has: symbol, product_id, strike, delta, iv, bid, ask, mid, dte
    """
    calls = []
    puts = []

    for t in tickers:
        symbol = t.get("symbol", "")
        contract_type = t.get("contract_type", "")

        if contract_type not in ("call_options", "put_options"):
            continue

        greeks = t.get("greeks", {}) or {}
        quotes = t.get("quotes", {}) or {}

        try:
            delta = float(greeks.get("delta", 0) or 0)
        except (ValueError, TypeError):
            delta = 0.0

        try:
            bid = float(quotes.get("best_bid", 0) or 0)
            ask = float(quotes.get("best_ask", 0) or 0)
        except (ValueError, TypeError):
            bid = ask = 0.0

        try:
            iv_str = quotes.get("bid_iv") or quotes.get("ask_iv") or t.get("mark_vol", 0)
            iv = float(iv_str or 0)
        except (ValueError, TypeError):
            iv = 0.0

        try:
            strike = float(t.get("strike_price", 0) or 0)
        except (ValueError, TypeError):
            strike = 0.0

        mid = (bid + ask) / 2 if bid and ask else 0.0
        expiry_code = parse_expiry_from_symbol(symbol)
        dte = days_to_expiry(expiry_code) if expiry_code else 0.0

        entry = {
            "symbol": symbol,
            "product_id": t.get("product_id"),
            "strike": strike,
            "delta": delta,
            "iv": iv,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "dte": dte,
            "mark_price": float(t.get("mark_price", 0) or 0),
            "spot_price": float(t.get("spot_price", 0) or 0),
        }

        if contract_type == "call_options":
            calls.append(entry)
        else:
            puts.append(entry)

    return {
        "calls": sorted(calls, key=lambda x: x["strike"]),
        "puts": sorted(puts, key=lambda x: x["strike"]),
    }

def select_strike_by_delta(
    options: List[Dict],
    target_delta: float,
    delta_tolerance: float = 0.03,
) -> Optional[Dict]:
    """
    Select the option closest to target_delta within tolerance.
    For calls: delta is positive. For puts: delta is negative.
    """
    target_abs = abs(target_delta)
    candidates = [
        o for o in options
        if abs(abs(o["delta"]) - target_abs) <= delta_tolerance
        and o["bid"] > 0
        and o["ask"] > 0
    ]
    if not candidates:
        # Relax and find nearest
        candidates = [o for o in options if o["bid"] > 0]
        if not candidates:
            return None
    return min(candidates, key=lambda o: abs(abs(o["delta"]) - target_abs))
