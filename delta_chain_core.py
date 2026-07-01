"""
delta_chain_core.py
--------------------
Core engine for building a Nifty-style options chain (Call | Strike | Put)
for crypto options on Delta Exchange, across ALL live expiries.

Features implemented:
    1. Pulls live option products + tickers (free/public market data, no API key needed)
    2. Persists OI + price snapshots to SQLite so we can compute:
         - OI Chg % (vs previous snapshot)
         - Buildup tag: Long Buildup / Short Buildup / Long Unwinding / Short Covering
    3. Ranks strikes by OI within each (expiry, side) bucket -> OI1 / OI2 / OI3 badges
    4. Groups everything per-expiry so a UI can render one chain per expiry tab

NOTE ON API FIELD NAMES:
    Delta's public endpoints (checked against docs as of training, not live-verified
    here since api.delta.exchange / api.india.delta.exchange are not reachable from
    this sandbox) are:
        GET {BASE}/v2/products?contract_types=call_options,put_options&states=live
        GET {BASE}/v2/tickers?contract_types=call_options,put_options
    Ticker payloads typically expose: symbol, mark_price, spot_price, oi (open
    interest in contracts), oi_value_usd, oi_change_6h / oi_change_24h (may or may
    not be present depending on API version), turnover, volume.

    Because I can't hit the live endpoint from here to pin exact keys, run
    `python delta_chain_core.py --debug-fields` ONCE on your GCP box first.
    It prints the raw keys of one ticker object so you can confirm/adjust
    the FIELD MAP below in 30 seconds. Everything else is written to be
    resilient to minor key differences (uses get_first() with fallbacks).
"""

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

BASE_URL = "https://api.india.delta.exchange"   # swap to https://api.delta.exchange for global acct
UNDERLYING = "BTC"                               # change to ETH etc if needed
DB_PATH = "oi_snapshots.db"
TOP_N_OI = 3                                     # OI1 / OI2 / OI3 badges

# Fallback-tolerant field map: first key found wins
FIELD_MAP = {
    "oi": ["oi", "open_interest"],
    "oi_value": ["oi_value_usd", "oi_value"],
    "mark_price": ["mark_price", "close", "last_price"],
    "spot_price": ["spot_price", "underlying_price"],
    "volume": ["volume", "turnover"],
}


def get_first(d: dict, keys: list, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


# ----------------------------------------------------------------------------
# DATA CLASSES
# ----------------------------------------------------------------------------

@dataclass
class LegSnapshot:
    expiry: str          # YYYY-MM-DD
    strike: float
    side: str             # "call" | "put"
    symbol: str
    oi: float
    price: float
    volume: float
    ts: float             # unix timestamp of this snapshot


# ----------------------------------------------------------------------------
# STORAGE
# ----------------------------------------------------------------------------

def init_db(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oi_snapshots (
            ts REAL,
            expiry TEXT,
            strike REAL,
            side TEXT,
            symbol TEXT,
            oi REAL,
            price REAL,
            volume REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lookup ON oi_snapshots(expiry, strike, side, ts)"
    )
    conn.commit()
    return conn


def save_snapshot(conn, legs: list[LegSnapshot]):
    conn.executemany(
        """INSERT INTO oi_snapshots (ts, expiry, strike, side, symbol, oi, price, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(l.ts, l.expiry, l.strike, l.side, l.symbol, l.oi, l.price, l.volume) for l in legs],
    )
    conn.commit()


def get_previous_snapshot(conn, expiry: str, strike: float, side: str, before_ts: float) -> Optional[dict]:
    """Most recent snapshot strictly before `before_ts` for this leg."""
    row = conn.execute(
        """SELECT ts, oi, price FROM oi_snapshots
           WHERE expiry=? AND strike=? AND side=? AND ts<?
           ORDER BY ts DESC LIMIT 1""",
        (expiry, strike, side, before_ts),
    ).fetchone()
    if row:
        return {"ts": row[0], "oi": row[1], "price": row[2]}
    return None


# ----------------------------------------------------------------------------
# FETCH
# ----------------------------------------------------------------------------

def fetch_products(underlying: str = UNDERLYING) -> list[dict]:
    """Get live option product metadata: symbol -> strike / expiry / side."""
    url = f"{BASE_URL}/v2/products"
    params = {"contract_types": "call_options,put_options", "states": "live"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("result", [])

    products = []
    for p in data:
        underlying_asset = (
            p.get("underlying_asset", {}).get("symbol")
            if isinstance(p.get("underlying_asset"), dict)
            else p.get("underlying_asset")
        )
        if underlying_asset != underlying:
            continue
        contract_type = p.get("contract_type", "")
        side = "call" if "call" in contract_type else "put" if "put" in contract_type else None
        if side is None:
            continue
        settlement_time = p.get("settlement_time")  # ISO string
        expiry = settlement_time[:10] if settlement_time else "unknown"
        strike = p.get("strike_price")
        products.append(
            {
                "symbol": p.get("symbol"),
                "expiry": expiry,
                "strike": float(strike) if strike is not None else None,
                "side": side,
            }
        )
    return products


def fetch_tickers(underlying: str = UNDERLYING) -> dict:
    """Get live ticker (OI/price/volume) keyed by symbol."""
    url = f"{BASE_URL}/v2/tickers"
    params = {"contract_types": "call_options,put_options"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("result", [])
    return {t.get("symbol"): t for t in data}


def debug_print_fields(underlying: str = UNDERLYING):
    """One-off helper: run this first on your server to confirm real field names."""
    tickers = fetch_tickers(underlying)
    if not tickers:
        print("No tickers returned - check BASE_URL / underlying / network.")
        return
    sample_symbol, sample = next(iter(tickers.items()))
    print(f"Sample symbol: {sample_symbol}")
    print(json.dumps(sample, indent=2))
    print("\n--- Adjust FIELD_MAP in delta_chain_core.py if the OI/price keys differ ---")


# ----------------------------------------------------------------------------
# SNAPSHOT BUILD + BUILDUP CLASSIFICATION
# ----------------------------------------------------------------------------

def classify_buildup(oi_chg_pct: Optional[float], price_chg_pct: Optional[float]) -> str:
    """Standard 4-way options buildup classification."""
    if oi_chg_pct is None or price_chg_pct is None:
        return "Insufficient Data"
    # small dead-zone so noise doesn't flip labels
    oi_up = oi_chg_pct > 0.5
    oi_down = oi_chg_pct < -0.5
    price_up = price_chg_pct > 0.1
    price_down = price_chg_pct < -0.1

    if oi_up and price_up:
        return "Long Buildup"
    if oi_up and price_down:
        return "Short Buildup"
    if oi_down and price_down:
        return "Long Unwinding"
    if oi_down and price_up:
        return "Short Covering"
    return "Neutral"


def build_current_snapshot(underlying: str = UNDERLYING) -> list[LegSnapshot]:
    products = fetch_products(underlying)
    tickers = fetch_tickers(underlying)
    ts = time.time()

    legs = []
    for p in products:
        if p["strike"] is None or p["symbol"] not in tickers:
            continue
        t = tickers[p["symbol"]]
        oi = get_first(t, FIELD_MAP["oi"], 0) or 0
        price = get_first(t, FIELD_MAP["mark_price"], 0) or 0
        volume = get_first(t, FIELD_MAP["volume"], 0) or 0
        legs.append(
            LegSnapshot(
                expiry=p["expiry"],
                strike=p["strike"],
                side=p["side"],
                symbol=p["symbol"],
                oi=float(oi),
                price=float(price),
                volume=float(volume),
                ts=ts,
            )
        )
    return legs


def build_option_chain(underlying: str = UNDERLYING, db_path: str = DB_PATH) -> dict:
    """
    Main entry point. Returns a dict keyed by expiry:
        {
          "2026-07-11": {
              "strikes": [
                 {
                   "strike": 66000,
                   "call": {oi, oi_chg_pct, price, price_chg_pct, buildup, oi_rank},
                   "put":  {...}
                 }, ...
              ],
              "max_oi_call_strike": ...,
              "max_oi_put_strike": ...,
          }, ...
        }
    Also persists this snapshot to SQLite for the NEXT run's delta calc.
    """
    conn = init_db(db_path)
    current_legs = build_current_snapshot(underlying)

    # organize current legs by (expiry, side) -> list
    by_expiry_side: dict[tuple, list[LegSnapshot]] = {}
    for leg in current_legs:
        by_expiry_side.setdefault((leg.expiry, leg.side), []).append(leg)

    result: dict[str, dict] = {}

    for (expiry, side), legs in by_expiry_side.items():
        result.setdefault(expiry, {"strikes_map": {}})

        # compute chg% and buildup per leg vs previous snapshot
        enriched = []
        for leg in legs:
            prev = get_previous_snapshot(conn, expiry, leg.strike, side, leg.ts)
            if prev and prev["oi"]:
                oi_chg_pct = (leg.oi - prev["oi"]) / prev["oi"] * 100
            else:
                oi_chg_pct = None
            if prev and prev["price"]:
                price_chg_pct = (leg.price - prev["price"]) / prev["price"] * 100
            else:
                price_chg_pct = None

            buildup = classify_buildup(oi_chg_pct, price_chg_pct)
            enriched.append(
                {
                    "strike": leg.strike,
                    "oi": leg.oi,
                    "oi_chg_pct": round(oi_chg_pct, 2) if oi_chg_pct is not None else None,
                    "price": leg.price,
                    "price_chg_pct": round(price_chg_pct, 2) if price_chg_pct is not None else None,
                    "volume": leg.volume,
                    "buildup": buildup,
                }
            )

        # rank by OI descending -> OI1..OI3 badges
        ranked = sorted(enriched, key=lambda x: x["oi"], reverse=True)
        for i, item in enumerate(ranked[:TOP_N_OI]):
            item["oi_rank"] = i + 1
        for item in ranked[TOP_N_OI:]:
            item["oi_rank"] = None

        for item in enriched:
            strike_key = item["strike"]
            result[expiry]["strikes_map"].setdefault(strike_key, {"strike": strike_key})
            result[expiry]["strikes_map"][strike_key][side] = item

    # finalize: convert strikes_map -> sorted list, attach max-OI strike per side
    for expiry, payload in result.items():
        strikes = sorted(payload["strikes_map"].values(), key=lambda x: x["strike"])
        payload["strikes"] = strikes
        del payload["strikes_map"]

        call_oi_sorted = sorted(
            [s for s in strikes if "call" in s], key=lambda s: s["call"]["oi"], reverse=True
        )
        put_oi_sorted = sorted(
            [s for s in strikes if "put" in s], key=lambda s: s["put"]["oi"], reverse=True
        )
        payload["max_oi_call_strike"] = call_oi_sorted[0]["strike"] if call_oi_sorted else None
        payload["max_oi_put_strike"] = put_oi_sorted[0]["strike"] if put_oi_sorted else None

    # persist this run so the NEXT run can diff against it
    save_snapshot(conn, current_legs)
    conn.close()

    return result


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug-fields", action="store_true", help="Print raw ticker keys and exit")
    parser.add_argument("--underlying", default=UNDERLYING)
    parser.add_argument("--json-out", default=None, help="Optional path to dump full chain as JSON")
    args = parser.parse_args()

    if args.debug_fields:
        debug_print_fields(args.underlying)
    else:
        chain = build_option_chain(args.underlying)
        print(f"Fetched chain for {len(chain)} expiries at {datetime.now(timezone.utc).isoformat()}")
        for expiry, payload in chain.items():
            print(f"  {expiry}: {len(payload['strikes'])} strikes | "
                  f"max OI call={payload['max_oi_call_strike']} put={payload['max_oi_put_strike']}")
        if args.json_out:
            with open(args.json_out, "w") as f:
                json.dump(chain, f, indent=2)
            print(f"Full chain written to {args.json_out}")
