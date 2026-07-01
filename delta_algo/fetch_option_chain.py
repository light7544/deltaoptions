"""
fetch_option_chain.py
=====================
Connect to Delta Exchange API and pull live option chain.
Based exactly on the official Delta Exchange API documentation.

Usage:
    python fetch_option_chain.py
    python fetch_option_chain.py --underlying ETH
    python fetch_option_chain.py --live
"""

import hashlib
import hmac
import json
import requests
import time
import argparse
import sys
import io
from datetime import datetime, date
from typing import Optional

# Ensure Unicode characters print correctly on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# ── CONFIG ───────────────────────────────────────────────────
BASE_URL_PRODUCTION = "https://api.india.delta.exchange"
BASE_URL_TESTNET    = "https://cdn-ind.testnet.deltaex.org"


# ── AUTH — exact from Delta docs ─────────────────────────────
def generate_signature(api_secret: str, message: str) -> str:
    message = bytes(message, 'utf-8')
    secret  = bytes(api_secret, 'utf-8')
    hash    = hmac.new(secret, message, hashlib.sha256)
    return hash.hexdigest()


def get_public_headers() -> dict:
    return {
        "User-Agent":   "python-rest-client",
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


def get_auth_headers(api_key: str, api_secret: str,
                     method: str, path: str,
                     query_string: str = "", payload: str = "") -> dict:
    timestamp      = str(int(time.time()))
    signature_data = method + timestamp + path + query_string + payload
    signature      = generate_signature(api_secret, signature_data)
    return {
        "api-key":      api_key,
        "timestamp":    timestamp,
        "signature":    signature,
        "User-Agent":   "python-rest-client",
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


# ── STEP 1: GET SPOT PRICE ────────────────────────────────────
# Docs: GET /v2/tickers/{symbol}
def get_spot_price(base_url: str, underlying: str) -> float:
    url      = f"{base_url}/v2/tickers/{underlying}USD"
    response = requests.get(url, headers=get_public_headers(), timeout=(5, 30))
    if response.status_code != 200:
        return 0.0
    data   = response.json()
    result = data.get("result", {})
    spot   = float(result.get("spot_price", 0) or 0)
    mark   = float(result.get("mark_price", 0) or 0)
    return spot if spot > 0 else mark


# ── STEP 2: GET ALL OPTION PRODUCTS ──────────────────────────
# Docs: GET /v2/products?contract_types=call_options,put_options&states=live
def get_option_products(base_url: str, underlying: str) -> list:
    url    = f"{base_url}/v2/products"
    params = {
        "contract_types": "call_options,put_options",
        "states":         "live",
        "page_size":      100,
    }

    print(f"\n{'='*60}")
    print(f"STEP 1 — Fetching option products for {underlying}")
    print(f"Endpoint : GET /v2/products")
    print(f"{'='*60}")

    response = requests.get(
        url, params=params,
        headers=get_public_headers(),
        timeout=(5, 30)
    )

    print(f"HTTP Status : {response.status_code}")

    if response.status_code != 200:
        print(f"Error: {response.text}")
        return []

    data = response.json()

    if not data.get("success", False):
        print(f"API Error: {data}")
        return []

    all_products = data.get("result", [])
    print(f"Total option products : {len(all_products)}")

    # Filter by underlying — symbol format: C-BTC-90000-310125
    filtered = [
        p for p in all_products
        if len(p.get("symbol", "").split("-")) >= 2
        and p.get("symbol", "").split("-")[1].upper() == underlying.upper()
    ]

    print(f"Products for {underlying} : {len(filtered)}")
    return filtered


# ── STEP 3: PARSE EXPIRY DATES ────────────────────────────────
def get_expiry_dates(products: list) -> list:
    """
    Parse expiry dates from symbol format: C-BTC-90000-310125 (ddMMyy)
    Convert to DD-MM-YYYY for tickers API
    """
    expiries = {}
    for p in products:
        sym   = p.get("symbol", "")
        parts = sym.split("-")
        if len(parts) < 4:
            continue
        date_str = parts[-1]
        try:
            d        = int(date_str[:2])
            mo       = int(date_str[2:4])
            yr       = 2000 + int(date_str[4:])
            exp_date = date(yr, mo, d)
            dte      = (exp_date - date.today()).days
            if dte >= 0:
                expiries[date_str] = {
                    "date_str":   date_str,
                    "display":    f"{d:02d}/{mo:02d}/{yr}",
                    "api_format": f"{d:02d}-{mo:02d}-{yr}",
                    "dte":        dte,
                }
        except:
            continue

    sorted_expiries = sorted(expiries.values(), key=lambda x: x["dte"])

    print(f"\nFound {len(sorted_expiries)} expiry dates:")
    for e in sorted_expiries[:8]:
        print(f"  {e['display']}  (DTE: {e['dte']} days)")

    return sorted_expiries


# ── STEP 4: FETCH OPTION CHAIN ────────────────────────────────
# Docs: GET /v2/tickers?contract_types=call_options,put_options
#                      &underlying_asset_symbols=BTC
#                      &expiry_date=DD-MM-YYYY
def get_option_chain(base_url: str, underlying: str, expiry_api_fmt: str) -> list:
    url    = f"{base_url}/v2/tickers"
    params = {
        "contract_types":           "call_options,put_options",
        "underlying_asset_symbols": underlying,
        "expiry_date":              expiry_api_fmt,
    }

    print(f"\n{'='*60}")
    print(f"STEP 2 — Fetching option chain")
    print(f"Endpoint : GET /v2/tickers")
    print(f"Params   : {params}")
    print(f"{'='*60}")

    response = requests.get(
        url, params=params,
        headers=get_public_headers(),
        timeout=(5, 30)
    )

    print(f"HTTP Status : {response.status_code}")

    if response.status_code != 200:
        print(f"Error: {response.text}")
        return []

    data = response.json()

    if not data.get("success", False):
        print(f"API Error: {data}")
        return []

    chain = data.get("result", [])
    print(f"Options returned : {len(chain)}")
    return chain


# ── STEP 5: DISPLAY CHAIN ────────────────────────────────────
def display_option_chain(chain: list, spot: float,
                          underlying: str, expiry_display: str) -> dict:
    calls = {}
    puts  = {}

    for opt in chain:
        sym   = opt.get("symbol", "")
        parts = sym.split("-")
        if len(parts) < 3:
            continue
        try:
            strike = float(parts[2])
        except:
            continue

        ct     = opt.get("contract_type", "")
        quotes = opt.get("quotes", {}) or {}
        greeks = opt.get("greeks", {}) or {}

        entry = {
            "symbol":     sym,
            "product_id": opt.get("product_id", 0),
            "bid":        float(quotes.get("best_bid",  0) or 0),
            "ask":        float(quotes.get("best_ask",  0) or 0),
            "mark_price": float(opt.get("mark_price",  0) or 0),
            "mark_vol":   float(opt.get("mark_vol",    0) or 0),
            "delta":      float(greeks.get("delta",    0) or 0),
            "gamma":      float(greeks.get("gamma",    0) or 0),
            "theta":      float(greeks.get("theta",    0) or 0),
            "vega":       float(greeks.get("vega",     0) or 0),
            "rho":        float(greeks.get("rho",      0) or 0),
            "oi":         float(opt.get("oi", 0) or 0),
            "oi_change":  float(opt.get("oi_change_usd_6h", 0) or 0),
        }

        if "call" in ct:
            calls[strike] = entry
        elif "put" in ct:
            puts[strike] = entry

    all_strikes = sorted(set(list(calls.keys()) + list(puts.keys())))

    if not all_strikes:
        print("No strikes found in chain.")
        return {}

    # ATM strike
    atm = min(all_strikes, key=lambda s: abs(s - spot)) if spot > 0 else None

    # Print header
    print(f"\n{'='*130}")
    print(f"  OPTION CHAIN — {underlying} | Expiry: {expiry_display} | Spot: {spot:,.2f}")
    print(f"{'='*130}")
    print(
        f"  {'Strike':>10} | "
        f"{'C-Bid':>9} {'C-Ask':>9} {'C-IV%':>7} {'C-Δ':>7} {'C-Θ':>8} | "
        f"{'':^7} | "
        f"{'P-Bid':>9} {'P-Ask':>9} {'P-IV%':>7} {'P-Δ':>7} {'P-Θ':>8}"
    )
    print(f"  {'-'*126}")

    for strike in all_strikes:
        c = calls.get(strike, {})
        p = puts.get(strike,  {})

        # Markers
        atm_tag     = " ATM" if strike == atm else ""
        call_tag    = " ←0.15δ" if 0.10 <= abs(c.get("delta", 0)) <= 0.20 \
                      and spot > 0 and strike > spot else ""
        put_tag     = " ←0.15δ" if 0.10 <= abs(p.get("delta", 0)) <= 0.20 \
                      and spot > 0 and strike < spot else ""

        print(
            f"  {strike:>10,.0f} | "
            f"{c.get('bid',0):>9.4f} "
            f"{c.get('ask',0):>9.4f} "
            f"{c.get('mark_vol',0):>6.1f}% "
            f"{c.get('delta',0):>7.3f} "
            f"{c.get('theta',0):>8.4f}{call_tag:7} | "
            f"{atm_tag:^7} | "
            f"{p.get('bid',0):>9.4f} "
            f"{p.get('ask',0):>9.4f} "
            f"{p.get('mark_vol',0):>6.1f}% "
            f"{p.get('delta',0):>7.3f} "
            f"{p.get('theta',0):>8.4f}{put_tag}"
        )

    print(f"  {'='*126}")
    print(f"\n  Total: {len(calls)} calls | {len(puts)} puts | {len(all_strikes)} strikes\n")

    # Best strangle candidates
    if spot > 0:
        print(f"  🎯 BEST STRANGLE CANDIDATES (±0.15 delta):")

        otm_calls = {s: c for s, c in calls.items()
                     if s > spot and c.get("delta", 0) > 0}
        otm_puts  = {s: p for s, p in puts.items()
                     if s < spot and p.get("delta", 0) < 0}

        best_call = None
        best_put  = None

        if otm_calls:
            best_cs = min(otm_calls, key=lambda s: abs(otm_calls[s]["delta"] - 0.15))
            best_call = otm_calls[best_cs]
            print(f"  SELL CALL → {best_call['symbol']:30} "
                  f"Strike: {best_cs:>10,.0f} | "
                  f"Bid: {best_call['bid']:>8.4f} | "
                  f"Delta: {best_call['delta']:>6.3f} | "
                  f"IV: {best_call['mark_vol']:>5.1f}%")

        if otm_puts:
            best_ps = min(otm_puts, key=lambda s: abs(abs(otm_puts[s]["delta"]) - 0.15))
            best_put = otm_puts[best_ps]
            print(f"  SELL PUT  → {best_put['symbol']:30} "
                  f"Strike: {best_ps:>10,.0f} | "
                  f"Bid: {best_put['bid']:>8.4f} | "
                  f"Delta: {best_put['delta']:>6.3f} | "
                  f"IV: {best_put['mark_vol']:>5.1f}%")

        if best_call and best_put:
            net = best_call["bid"] + best_put["bid"]
            print(f"\n  NET CREDIT      : {net:.4f} {underlying}")
            print(f"  Profit target   : {net * 0.35:.4f} (35%)")
            print(f"  Stop loss level : {net * 1.5:.4f} (1.5x per leg)")

    return {"calls": calls, "puts": puts, "strikes": all_strikes}


# ── MAIN ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Delta Exchange — Fetch Option Chain")
    parser.add_argument("--underlying", default="BTC", choices=["BTC", "ETH", "SOL"])
    parser.add_argument("--live",       action="store_true", help="Production API")
    parser.add_argument("--expiry",     default=None,        help="DD-MM-YYYY")
    parser.add_argument("--save",       action="store_true", help="Save to JSON")
    args = parser.parse_args()

    base_url = BASE_URL_PRODUCTION if args.live else BASE_URL_TESTNET
    env      = "PRODUCTION" if args.live else "TESTNET"

    print(f"\n{'='*60}")
    print(f"  Delta Exchange Option Chain")
    print(f"  Environment : {env}")
    print(f"  URL         : {base_url}")
    print(f"  Underlying  : {args.underlying}")
    print(f"{'='*60}")

    # 1. Spot price
    spot = get_spot_price(base_url, args.underlying)
    print(f"\n  {args.underlying} Spot Price : {spot:,.2f}" if spot > 0
          else f"\n  Could not fetch spot price")

    # 2. Products
    products = get_option_products(base_url, args.underlying)
    if not products:
        print(f"\n  No products found on {env}.")
        print(f"  Try: python fetch_option_chain.py --live")
        return

    # 3. Expiries
    expiries = get_expiry_dates(products)
    if not expiries:
        print("  No future expiries found.")
        return

    # 4. Select expiry
    if args.expiry:
        matches = [e for e in expiries if e["api_format"] == args.expiry]
        if not matches:
            print(f"  Expiry {args.expiry} not found. Available:")
            for e in expiries:
                print(f"    {e['api_format']}  (DTE: {e['dte']})")
            return
        selected = matches[0]
    else:
        weekly = [e for e in expiries if 2 <= e["dte"] <= 8]
        selected = weekly[0] if weekly else expiries[0]
        label = "weekly" if weekly else "nearest"
        print(f"\n  Auto-selected {label} expiry: "
              f"{selected['display']} (DTE: {selected['dte']})")

    # 5. Fetch chain
    chain = get_option_chain(base_url, args.underlying, selected["api_format"])
    if not chain:
        print(f"\n  No chain data for this expiry.")
        print(f"  Available expiries to try:")
        for e in expiries[:5]:
            print(f"    python fetch_option_chain.py --live --expiry {e['api_format']}")
        return

    # 6. Display
    result = display_option_chain(chain, spot, args.underlying, selected["display"])

    # 7. Save
    if args.save and result:
        import os
        os.makedirs("data", exist_ok=True)
        fname = f"data/chain_{args.underlying}_{selected['date_str']}_{int(time.time())}.json"
        with open(fname, "w") as f:
            json.dump({
                "underlying": args.underlying,
                "spot":       spot,
                "expiry":     selected["display"],
                "dte":        selected["dte"],
                "fetched_at": datetime.utcnow().isoformat(),
                "calls":      {str(k): v for k, v in result["calls"].items()},
                "puts":       {str(k): v for k, v in result["puts"].items()},
            }, f, indent=2, default=str)
        print(f"\n  Saved to: {fname}")

    print(f"\n  Done.\n")


if __name__ == "__main__":
    main()
