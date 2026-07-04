"""
auth.py
--------
Auth for the Options Chain app: Google OAuth + Phone OTP.

SETUP NEEDED (both have TODOs marked below):
  1. Google OAuth:
     - Create OAuth 2.0 Client ID at https://console.cloud.google.com/apis/credentials
     - Authorized redirect URI: http://<your-domain-or-ip>:8000/auth/google/callback
     - Set env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
  2. Phone OTP / SMS:
     - No SMS provider is wired yet. OTPs are generated + verified correctly, but the
       "send" step currently just logs the OTP server-side (dev mode) so you can test
       end-to-end today. Plug MSG91 / Twilio / any provider into `send_otp_sms()`.
  3. Set a real SESSION_SECRET env var in production (falls back to a dev default).

Run: pip install -r requirements.txt   (adds authlib, pyjwt, python-multipart)
"""

import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
import jwt
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

# Load environment variables from .env file if present
load_dotenv()

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-change-me")
JWT_ALGO = "HS256"
SESSION_COOKIE = "session_token"
SESSION_TTL_HOURS = 24 * 7

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")       # TODO: fill in
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")  # TODO: fill in

OTP_TTL_SECONDS = 300      # 5 minutes
OTP_LENGTH = 6
DB_PATH = "auth.db"

# ----------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            email TEXT UNIQUE,
            name TEXT,
            google_sub TEXT UNIQUE,
            created_at REAL
        )"""
    )
    conn.commit()
    conn.close()


def get_or_create_user_by_phone(phone: str, name: Optional[str] = None) -> sqlite3.Row:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if row:
        if name and not row["name"]:
            conn.execute("UPDATE users SET name=? WHERE phone=?", (name, phone))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        conn.close()
        return row
    conn.execute(
        "INSERT INTO users (phone, name, created_at) VALUES (?, ?, ?)", (phone, name, time.time())
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    conn.close()
    return row


def get_or_create_user_by_google(sub: str, email: str, name: str) -> sqlite3.Row:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
    if row:
        conn.close()
        return row
    conn.execute(
        "INSERT INTO users (google_sub, email, name, created_at) VALUES (?, ?, ?, ?)",
        (sub, email, name, time.time()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
    conn.close()
    return row


# ----------------------------------------------------------------------------
# OTP STORE (in-memory; swap for Redis if you run multiple workers)
# ----------------------------------------------------------------------------

_otp_store: dict[str, dict] = {}  # phone -> {"otp": "123456", "expires": ts, "attempts": 0}


def generate_otp(phone: str) -> str:
    otp = str(random.randint(10 ** (OTP_LENGTH - 1), 10 ** OTP_LENGTH - 1))
    _otp_store[phone] = {"otp": otp, "expires": time.time() + OTP_TTL_SECONDS, "attempts": 0}
    return otp


def send_otp_sms(phone: str, otp: str):
    """
    TODO: plug your SMS provider here. Example for MSG91 (common for Indian numbers):

        import requests
        requests.post(
            "https://control.msg91.com/api/v5/otp",
            headers={"authkey": os.environ["MSG91_AUTH_KEY"]},
            json={"mobile": phone, "otp": otp, "template_id": os.environ["MSG91_TEMPLATE_ID"]},
        )

    Until that's wired, we just log it so you can test the flow.
    """
    print(f"[DEV OTP] phone={phone} otp={otp} (would be sent via SMS in production)")


def verify_otp(phone: str, otp: str) -> bool:
    entry = _otp_store.get(phone)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        del _otp_store[phone]
        return False
    entry["attempts"] += 1
    if entry["attempts"] > 5:
        del _otp_store[phone]
        return False
    if entry["otp"] == otp:
        del _otp_store[phone]
        return True
    return False


# ----------------------------------------------------------------------------
# JWT SESSION
# ----------------------------------------------------------------------------

def create_session_token(user_id: int) -> str:
    payload = {
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS),
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm=JWT_ALGO)


def decode_session_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, SESSION_SECRET, algorithms=[JWT_ALGO])
        return payload.get("user_id")
    except jwt.PyJWTError:
        return None


def get_current_user(request: Request) -> sqlite3.Row:
    token = request.cookies.get(SESSION_COOKIE)
    user_id = decode_session_token(token) if token else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return row


def get_current_user_optional(request: Request) -> Optional[sqlite3.Row]:
    try:
        return get_current_user(request)
    except HTTPException:
        return None


# ----------------------------------------------------------------------------
# GOOGLE OAUTH CLIENT
# ----------------------------------------------------------------------------

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# ----------------------------------------------------------------------------
# ROUTES
# ----------------------------------------------------------------------------

router = APIRouter()


class PhoneRequest(BaseModel):
    phone: str  # E.164 format e.g. +919876543210


class OtpVerifyRequest(BaseModel):
    phone: str
    otp: str
    name: Optional[str] = None


@router.post("/auth/phone/send-otp")
def send_otp(body: PhoneRequest):
    if not body.phone.startswith("+") or len(body.phone) < 8:
        raise HTTPException(status_code=400, detail="Use E.164 format, e.g. +919876543210")
    otp = generate_otp(body.phone)
    send_otp_sms(body.phone, otp)
    resp = {"status": "sent", "phone": body.phone}
    if os.environ.get("ENV", "dev") == "dev":
        resp["dev_otp"] = otp  # REMOVE this key before deploying to prod
    return resp


@router.post("/auth/phone/verify-otp")
def verify_otp_route(body: OtpVerifyRequest, response: Response):
    if not verify_otp(body.phone, body.otp):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    user = get_or_create_user_by_phone(body.phone, body.name)
    token = create_session_token(user["id"])
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, max_age=SESSION_TTL_HOURS * 3600, samesite="lax"
    )
    return {"status": "ok", "user": {"id": user["id"], "phone": user["phone"]}}


@router.get("/auth/google/login")
async def google_login(request: Request):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_CLIENT_ID not set. Add it as an env var (see auth.py header).",
        )
    redirect_uri = request.url_for("google_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback")
async def google_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    sub = userinfo.get("sub")
    email = userinfo.get("email")
    name = userinfo.get("name")
    if not sub:
        raise HTTPException(status_code=400, detail="Google login failed")
    user = get_or_create_user_by_google(sub, email, name)
    session_token = create_session_token(user["id"])
    resp = RedirectResponse(url="/")
    resp.set_cookie(
        SESSION_COOKIE, session_token, httponly=True,
        max_age=SESSION_TTL_HOURS * 3600, samesite="lax",
    )
    return resp


@router.get("/auth/me")
def me(user: sqlite3.Row = Depends(get_current_user)):
    return {"id": user["id"], "phone": user["phone"], "email": user["email"], "name": user["name"]}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged_out"}
