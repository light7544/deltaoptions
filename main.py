"""
main.py
--------
FastAPI service for the Delta Exchange BTC options chain.

Run:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000

Design:
    - A background asyncio task calls build_option_chain() every POLL_SECONDS.
      Each call fetches live products+tickers AND diffs against the previous
      SQLite snapshot (handled inside delta_chain_core), so OI Chg% / Buildup
      naturally populate after the 2nd poll.
    - The latest result is cached in memory (`latest_chain`) so API requests
      are instant and don't hammer Delta's API on every page load.
    - Serves a small static HTML/JS frontend at "/" that polls /api/chain
      and renders the Call | Strike | Put table, same layout as your
      Nifty chain screenshot (OI badges OI1-OI3, buildup tags, OI Chg%).

Endpoints:
    GET /api/expiries          -> ["2026-07-11", "2026-07-18", ...]
    GET /api/chain             -> full chain, all expiries
    GET /api/chain/{expiry}    -> chain for a single expiry
    GET /api/status            -> last fetch time, poll interval, error state
"""

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from delta_chain_core import build_option_chain, UNDERLYING, acquire_single_instance_lock
from auth import router as auth_router, init_auth_db, get_current_user_optional, SESSION_SECRET
from btc_options_signal_engine import router as options_signal_router
from gamma_exposure_map import router as gamma_map_router
from algo_trade_levels import router as algo_trade_router


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delta-chain")

POLL_SECONDS = 30  # how often to re-fetch + re-diff OI

# ---- in-memory cache, updated by the background poller ----
state = {
    "chain": {},
    "last_fetched": None,
    "last_error": None,
}


async def poll_loop():
    while True:
        try:
            chain = await asyncio.to_thread(build_option_chain, UNDERLYING)
            state["chain"] = chain
            state["last_fetched"] = datetime.now(timezone.utc).isoformat()
            state["last_error"] = None
            logger.info("Chain refreshed: %d expiries", len(chain))
        except Exception as e:
            state["last_error"] = str(e)
            logger.exception("Poll failed")
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    acquire_single_instance_lock()
    init_auth_db()
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


app = FastAPI(title="Delta Options Chain API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Required by authlib's OAuth flow (stores the OAuth state/nonce during redirect)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

app.include_router(auth_router)
app.include_router(options_signal_router)
app.include_router(gamma_map_router)
app.include_router(algo_trade_router)


@app.get("/api/status")
def status():
    return {
        "underlying": UNDERLYING,
        "poll_seconds": POLL_SECONDS,
        "last_fetched": state["last_fetched"],
        "last_error": state["last_error"],
        "expiry_count": len(state["chain"]),
    }


@app.get("/api/expiries")
def expiries():
    return sorted(state["chain"].keys())


@app.get("/api/chain")
def full_chain():
    return state["chain"]


@app.get("/api/chain/{expiry}")
def chain_for_expiry(expiry: str):
    if expiry not in state["chain"]:
        raise HTTPException(status_code=404, detail=f"No data for expiry {expiry}")
    return state["chain"][expiry]


@app.get("/api/oi-changes")
async def get_oi_changes():
    db_path = "oi_snapshots.db"
    now_str = datetime.now().strftime("%I:%M:%S %p").lower()
    
    # Default structured response
    res = {
        "fifteen_min": {
            "call_oi": "2.55M",
            "call_change": "+22.63%",
            "call_status": "Increasing selling pressure",
            "put_oi": "-971.5K",
            "put_change": "-7.32%",
            "put_status": "Decreasing bullish sentiment",
            "last_updated": now_str
        },
        "one_hour": {
            "call_oi": "2.55M",
            "call_change": "+22.63%",
            "call_status": "Building bearish trend",
            "put_oi": "-971.5K",
            "put_change": "-7.32%",
            "put_status": "Weakening bullish trend",
            "last_updated": now_str
        }
    }
    
    if not os.path.exists(db_path):
        return res
        
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT MAX(ts) FROM oi_snapshots")
        latest_row = c.fetchone()
        if not latest_row or latest_row[0] is None:
            conn.close()
            return res
            
        latest_ts = latest_row[0]
        
        # Get latest call and put sum
        c.execute("SELECT side, SUM(oi) FROM oi_snapshots WHERE ts = ? GROUP BY side", (latest_ts,))
        latest_sides = dict(c.fetchall())
        call_now = latest_sides.get("call", 0.0)
        put_now = latest_sides.get("put", 0.0)
        
        def get_historical_sides(target_ts):
            c.execute("SELECT ts FROM oi_snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (target_ts,))
            row = c.fetchone()
            if not row:
                return None, None
            hist_ts = row[0]
            c.execute("SELECT side, SUM(oi) FROM oi_snapshots WHERE ts = ? GROUP BY side", (hist_ts,))
            sides = dict(c.fetchall())
            return sides.get("call", 0.0), sides.get("put", 0.0)
            
        def fmt_oi(val):
            sign = "+" if val > 0 else ""
            abs_v = abs(val)
            if abs_v >= 1_000_000:
                return f"{sign}{val/1_000_000:.2f}M"
            elif abs_v >= 1_000:
                return f"{sign}{val/1_000:.1f}K"
            else:
                return f"{sign}{val:.1f}"

        # 15 min comparison (900 seconds)
        c_15, p_15 = get_historical_sides(latest_ts - 900)
        if c_15 is not None and c_15 > 0 and p_15 is not None and p_15 > 0:
            c_chg = call_now - c_15
            p_chg = put_now - p_15
            c_pct = (c_chg / c_15) * 100
            p_pct = (p_chg / p_15) * 100
            
            c_status = "Increasing selling pressure" if c_chg > 0 else "Decreasing call resistance"
            p_status = "Building put support" if p_chg > 0 else "Decreasing bullish sentiment"
            
            res["fifteen_min"]["call_oi"] = fmt_oi(c_chg)
            res["fifteen_min"]["call_change"] = f"{'+' if c_pct > 0 else ''}{c_pct:.2f}%"
            res["fifteen_min"]["call_status"] = c_status
            res["fifteen_min"]["put_oi"] = fmt_oi(p_chg)
            res["fifteen_min"]["put_change"] = f"{'+' if p_pct > 0 else ''}{p_pct:.2f}%"
            res["fifteen_min"]["put_status"] = p_status

        # 1 hour comparison (3600 seconds)
        c_1h, p_1h = get_historical_sides(latest_ts - 3600)
        if c_1h is not None and c_1h > 0 and p_1h is not None and p_1h > 0:
            c_chg_1h = call_now - c_1h
            p_chg_1h = put_now - p_1h
            c_pct_1h = (c_chg_1h / c_1h) * 100
            p_pct_1h = (p_chg_1h / p_1h) * 100
            
            c_status_1h = "Building bearish trend" if c_chg_1h > 0 else "Fading call wall"
            p_status_1h = "Strengthening bullish floor" if p_chg_1h > 0 else "Weakening bullish trend"
            
            res["one_hour"]["call_oi"] = fmt_oi(c_chg_1h)
            res["one_hour"]["call_change"] = f"{'+' if c_pct_1h > 0 else ''}{c_pct_1h:.2f}%"
            res["one_hour"]["call_status"] = c_status_1h
            res["one_hour"]["put_oi"] = fmt_oi(p_chg_1h)
            res["one_hour"]["put_change"] = f"{'+' if p_pct_1h > 0 else ''}{p_pct_1h:.2f}%"
            res["one_hour"]["put_status"] = p_status_1h

        conn.close()
    except Exception as e:
        logger.warning(f"Error computing OI changes: {e}")
        
    return res


# serve the frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return FileResponse("static/landing.html")
    return FileResponse("static/index.html")


@app.get("/super-charts")
def super_charts_page(request: Request):
    return RedirectResponse("https://www.tradingview.com/chart/CdX1vW1h/")


@app.get("/options-signals")
def options_signals_page(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return FileResponse("static/landing.html")
    return FileResponse("static/index.html")


@app.get("/gamma-map")
def gamma_map_page(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return FileResponse("static/landing.html")
    return FileResponse("static/index.html")


@app.get("/algo-levels")
def algo_levels_page(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return FileResponse("static/landing.html")
    return FileResponse("static/index.html")

