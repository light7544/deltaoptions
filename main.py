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
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from delta_chain_core import build_option_chain, UNDERLYING
from auth import router as auth_router, init_auth_db, get_current_user_optional, SESSION_SECRET

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


# serve the frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return RedirectResponse(url="/static/login.html")
    return FileResponse("static/index.html")
