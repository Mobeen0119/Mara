import html
import os
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from core import ratelimit
from core.clock import tz_name_for, valid_tz
from core.database import get_connection
from core.deps import bearer_token, require_user
from core.models import GuestLoginRequest, LoginRequest, SignupRequest, TimezoneRequest
from core.security import _utcnow
from core.security import (
    burn_password_time, create_session, hash_password, revoke_all_sessions,
    revoke_session, token_hash, verify_password,
)

router = APIRouter(prefix="/api", tags=["auth"])

_CTRL = re.compile(r"[\x00-\x1f\x7f]")

LOGIN_FAILS_PER_EMAIL = 5          # per (ip, email) per window
LOGIN_FAILS_PER_IP = 30            # per ip per window
LOGIN_WINDOW = 15 * 60
GUESTS_PER_IP_PER_HOUR = 10
SIGNUPS_PER_IP_PER_HOUR = 10


def _clean_name(raw):
    return _CTRL.sub("", (raw or "")).strip()[:80]


def _admin_email():
    return (os.environ.get("ELOISE_ADMIN_EMAIL") or "").strip().lower()


def _public_url(request: Request):
    return (os.environ.get("ELOISE_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def _smtp_ready():
    from core.mailer import smtp_configured
    return smtp_configured()


def _new_user(conn, name, email, password_hash, *, is_guest=0, verified=1, is_admin=0):
    cur = conn.execute(
        "INSERT INTO users (name, email, password_hash, is_guest, verified, checkin_time, is_admin) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, email, password_hash, is_guest, verified, "08:00", is_admin),
    )
    conn.commit()
    return cur.lastrowid


def _send_verification(conn, request, user_id, name, email):
    """Email a single-use link. Never blocks sign-in; returns True if mail went out."""
    if not _smtp_ready():
        return False
    raw = secrets.token_urlsafe(32)
    expires = (_utcnow() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM email_tokens WHERE user_id=? AND purpose='verify'", (user_id,))
    conn.execute(
        "INSERT INTO email_tokens (user_id, token_hash, purpose, expires_at) VALUES (?,?, 'verify', ?)",
        (user_id, token_hash(raw), expires),
    )
    conn.commit()
    link = f"{_public_url(request)}/api/verify-email?token={raw}"
    from core.mailer import send_email
    body = (
        f"{name}, confirm this address so Eloise can mail your schedule:\n\n{link}\n\n"
        "The link works once and expires in 48 hours. If this wasn't you, ignore it."
    )
    return send_email(email, "Eloise - confirm your email", body)


@router.post("/signup")
def signup(body: SignupRequest, request: Request):
    ip = ratelimit.client_ip(request)
    ratelimit.enforce(f"signup:{ip}", SIGNUPS_PER_IP_PER_HOUR, 3600, "too many signups from this address")
    name = _clean_name(body.name)
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    email = str(body.email).strip().lower()
    conn = get_connection()
    if conn.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
        raise HTTPException(status_code=409, detail="email already registered")
    # First real account (or the configured admin email) administers the app:
    # LLM key/endpoint are app-wide, so they must not be editable by anyone.
    no_admin = not conn.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    is_admin = 1 if (no_admin or (_admin_email() and email == _admin_email())) else 0
    mail_possible = _smtp_ready()
    uid = _new_user(conn, name, email, hash_password(body.password),
                    verified=0 if mail_possible else 1, is_admin=is_admin)
    _send_verification(conn, request, uid, name, email)
    token = create_session(conn, uid)
    return {"token": token, "name": name, "email": email, "is_admin": bool(is_admin),
            "verified": not mail_possible}


@router.post("/login")
def login(body: LoginRequest, request: Request):
    ip = ratelimit.client_ip(request)
    email = str(body.email).strip().lower()
    k_email, k_ip = f"loginfail:{ip}:{email}", f"loginfail:{ip}"
    if (ratelimit.count(k_email, LOGIN_WINDOW) >= LOGIN_FAILS_PER_EMAIL
            or ratelimit.count(k_ip, LOGIN_WINDOW) >= LOGIN_FAILS_PER_IP):
        raise HTTPException(status_code=429, detail="too many failed attempts, try again in 15 minutes",
                            headers={"Retry-After": str(LOGIN_WINDOW)})
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE lower(email)=? AND is_guest=0", (email,)).fetchone()
    if not row:
        burn_password_time(body.password)
        ok, upgrade = False, False
    else:
        ok, upgrade = verify_password(body.password, row["password_hash"])
    if not ok:
        ratelimit.hit(k_email, 10 ** 6, LOGIN_WINDOW)
        ratelimit.hit(k_ip, 10 ** 6, LOGIN_WINDOW)
        raise HTTPException(status_code=401, detail="bad credentials")
    if upgrade:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), row["id"]))
        conn.commit()
    ratelimit.reset(k_email)
    token = create_session(conn, row["id"])
    return {"token": token, "name": row["name"], "email": row["email"],
            "is_admin": bool(row["is_admin"]), "verified": bool(row["verified"])}


@router.post("/guest")
def guest(body: GuestLoginRequest, request: Request):
    """A throwaway workspace. Always a brand-new account with no email: a guest
    can never be handed an existing user's session."""
    ip = ratelimit.client_ip(request)
    ratelimit.enforce(f"guest:{ip}", GUESTS_PER_IP_PER_HOUR, 3600, "too many guest sessions from this address")
    name = _clean_name(body.name) or "guest"
    conn = get_connection()
    uid = _new_user(conn, name, None, hash_password(secrets.token_urlsafe(24)), is_guest=1, verified=1)
    token = create_session(conn, uid)
    return {"token": token, "name": name, "is_guest": True, "email": None, "has_email": False,
            "is_admin": False}


@router.get("/me")
def me(user: dict = Depends(require_user)):
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "is_guest": user["is_guest"],
        "is_admin": bool(user.get("is_admin")),
        "verified": bool(user.get("verified")),
        "checkin_time": user["checkin_time"] or "08:00",
        "last_checkin_at": user["last_checkin_at"],
        "location": (user.get("location") or "").strip() or "Pakistan",
        "timezone": tz_name_for(user),
    }


@router.put("/settings/timezone")
def set_timezone(body: TimezoneRequest, user: dict = Depends(require_user)):
    if not valid_tz(body.timezone):
        raise HTTPException(status_code=400, detail="unknown timezone (use an IANA name like Asia/Karachi)")
    conn = get_connection()
    conn.execute("UPDATE users SET timezone=? WHERE id=?", (body.timezone, user["id"]))
    conn.commit()
    return {"ok": True, "timezone": body.timezone}


@router.get("/openings")
def openings(user: dict = Depends(require_user)):
    from core.generation import generate_opening_message
    ratelimit.enforce(f"llm:{user['id']}", 30, 60)
    conn = get_connection()
    goals = conn.execute("SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall()
    summary = "; ".join(g["display_title"] for g in goals)
    text, source = generate_opening_message(user["name"], summary, db=conn)
    return {"message": text, "source": source}


@router.post("/logout")
def logout(authorization: str = Header(default=""), user: dict = Depends(require_user)):
    revoke_session(get_connection(), bearer_token(authorization))
    return {"ok": True}


@router.post("/logout-all")
def logout_all(user: dict = Depends(require_user)):
    revoke_all_sessions(get_connection(), user["id"])
    return {"ok": True}


_PAGE = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Eloise</title><body style='font:16px system-ui;background:#14110f;color:#eee;display:grid;"
    "place-items:center;height:100vh;margin:0'><div style='text-align:center'><h1>{h}</h1><p>{p}</p>"
    "<p><a style='color:#ff8b7a' href='/'>Open Eloise</a></p></div>"
)


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(token: str = ""):
    """Single-use link from the confirmation email. The old version let a user
    verify themselves with their own session token."""
    conn = get_connection()
    now = _utcnow().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT * FROM email_tokens WHERE token_hash=? AND purpose='verify' AND used=0 AND expires_at>=?",
        (token_hash(token or ""), now),
    ).fetchone() if token else None
    if not row:
        return HTMLResponse(_PAGE.format(h="Link expired", p="Ask for a fresh confirmation email in Settings."), status_code=400)
    conn.execute("UPDATE email_tokens SET used=1 WHERE id=?", (row["id"],))
    conn.execute("UPDATE users SET verified=1 WHERE id=?", (row["user_id"],))
    conn.commit()
    return HTMLResponse(_PAGE.format(h="Email confirmed", p="Your schedule mail is armed."))


@router.post("/verify-email/resend")
def resend_verification(request: Request, user: dict = Depends(require_user)):
    ratelimit.enforce(f"resend:{user['id']}", 3, 3600, "too many confirmation emails")
    if not user.get("email"):
        raise HTTPException(status_code=400, detail="no email on this account")
    if user.get("verified"):
        return {"ok": True, "already": True}
    sent = _send_verification(get_connection(), request, user["id"], user["name"], user["email"])
    return {"ok": sent, "reason": None if sent else "SMTP is not configured on this server"}
