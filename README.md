# Delta Exchange Options Chain (FastAPI)

Nifty-chain-style live options chain for BTC on Delta Exchange, across every live expiry:
Call OI | OI Chg% | Buildup | Volume | Price — Strike — Price | Volume | Buildup | OI Chg% | Put OI,
with OI1/OI2/OI3 badges on the top-3 OI strikes per side.

## Files
- `delta_chain_core.py` — fetches products+tickers, stores OI/price snapshots in SQLite,
  computes OI Chg% and buildup tag, ranks top-3 OI per side/expiry. Framework-agnostic.
- `main.py` — FastAPI app. Background task refetches + re-diffs every 30s (in-memory cache),
  exposes `/api/chain`, `/api/chain/{expiry}`, `/api/expiries`, `/api/status`.
- `static/index.html` — single-page dashboard (vanilla JS, no build step) that polls the API
  and renders the table + expiry tabs.

## Setup

```bash
cd delta_option_chain
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 1. Confirm live field names FIRST (important)
I couldn't hit Delta's API from my sandbox to verify exact ticker field names, so before
trusting the numbers, run:

```bash
python delta_chain_core.py --debug-fields
```

This prints one raw ticker object. Check that OI / mark price / volume keys match what's in
`FIELD_MAP` at the top of `delta_chain_core.py` — tweak that dict if Delta's response uses
different key names than assumed (`oi`, `mark_price`, `volume`).

### 2. Run standalone (no server) to sanity check
```bash
python delta_chain_core.py --json-out sample_chain.json
```
Run it twice ~a minute apart — the first run has no OI Chg%/buildup (no prior snapshot to
diff against), the second run onward will.

### 3. Run the API + UI
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
Open `http://<your-gcp-ip>:8000/` — dashboard polls the backend every 15s; backend itself
refetches Delta + re-diffs every 30s (`POLL_SECONDS` in `main.py`).

## Buildup classification (standard 4-way)
| OI | Price | Label |
|----|-------|-------|
| Up | Up | Long Buildup |
| Up | Down | Short Buildup |
| Down | Down | Long Unwinding |
| Down | Up | Short Covering |

Small dead-zones (±0.5% OI, ±0.1% price) avoid label flip-flopping on noise — tune in
`classify_buildup()` if you want it tighter/looser.

## Config knobs
- `UNDERLYING` in `delta_chain_core.py` — switch BTC → ETH etc.
- `BASE_URL` — `api.india.delta.exchange` vs `api.delta.exchange`, depending on which account/region you use.
- `POLL_SECONDS` in `main.py` — backend refetch cadence.
- `TOP_N_OI` in `delta_chain_core.py` — currently top 3, bump if you want OI1-OI6 like your Nifty screenshot.

## Auth (Google + Phone OTP)

The dashboard (`/`) now redirects to `/static/login.html` unless you have a valid session.

**Files:** `auth.py` (all auth logic), `static/login.html`, `static/signup.html`, `static/auth-style.css`.

### Google login setup
1. Go to https://console.cloud.google.com/apis/credentials → Create OAuth Client ID (Web application).
2. Authorized redirect URI: `http://<your-domain-or-ip>:8000/auth/google/callback`
3. Set env vars before running:
   ```bash
   export GOOGLE_CLIENT_ID="..."
   export GOOGLE_CLIENT_SECRET="..."
   ```
Without these set, the Google button will hit `/auth/google/login` and return a clear 500 telling you the env var is missing — it won't fail silently.

### Phone OTP setup
OTP generation, expiry (5 min), and verification are fully implemented and tested end-to-end.
The only missing piece is an actual SMS provider — right now `send_otp_sms()` in `auth.py`
just logs the OTP server-side, and the API response includes a `dev_otp` field when
`ENV=dev` (default) so you can test the whole login flow without SMS today.

To go live:
1. Pick a provider — MSG91 or Twilio are the common choices for Indian numbers.
2. Fill in `send_otp_sms()` in `auth.py` (a ready-to-uncomment MSG91 example is in the docstring there).
3. Set `ENV=production` so the `dev_otp` field stops being returned in API responses.

### Session
- JWT stored in an httpONLY cookie (`session_token`), 7-day expiry by default (`SESSION_TTL_HOURS` in `auth.py`).
- Set a real `SESSION_SECRET` env var in production — it currently falls back to a dev default.
- `GET /auth/me` returns the current logged-in user; `POST /auth/logout` clears the cookie.
- Users are stored in `auth.db` (SQLite, separate from `oi_snapshots.db`).

### Still just a redirect gate on `/`
The `/api/chain*` endpoints are currently NOT auth-gated (dashboard page is, API isn't) —
if you want the raw API locked down too, add `Depends(get_current_user)` to those route
functions in `main.py`.

## Deploy notes for your GCP e2-small box
- Run behind your existing reverse proxy / same box as your Delta MCP server — no conflict,
  this doesn't need auth keys since product/ticker endpoints are public market data.
- `oi_snapshots.db` (SQLite) grows over time — fine for a while, but consider a cron to
  prune rows older than N days once you've validated this is stable.
- If you want this exposed to Claude via MCP later (so I can query live chain state
  directly instead of you screenshotting), the FastAPI endpoints here are already
  clean JSON — wrapping them as MCP tools next to your existing Delta MCP server is a
  small step, happy to do that whenever you want.
