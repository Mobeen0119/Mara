import hashlib
import html
import json
import secrets
import smtplib
from email.mime.text import MIMEText
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from core.database import get_connection
from core.deps import require_user
from core.models import GuestLoginRequest, LoginRequest, SignupRequest
from core.persona import fallback_greeting

router = APIRouter(prefix="/api", tags=["auth"])


def _hash(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def _sanitize_name(raw):
    return html.escape((raw or "").strip())[:80]


def _new_user(name, email, password_hash, is_guest=0, verified=1):
    conn = get_connection()
    token = secrets.token_urlsafe(32)
    cur = conn.execute(
        "INSERT INTO users (name, email, password_hash, is_guest, verified, token, checkin_time) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, email, password_hash, is_guest, verified, token, "08:00"),
    )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()


def _confirm_email(user):
    """Best-effort confirmation mail when the account has an email. Never blocks sign-in."""
    if not user["email"]:
        return False
    from core.mailer import smtp_configured, send_email
    if not smtp_configured():
        return False
    body = (
        f"{user['name']}, your seat at Eloise is confirmed.\n\n"
        "Your schedule lands here when the day's plan is ready, and at your check-in.\n"
        "Take a seat. The clock's already running."
    )
    return send_email(user["email"], "Eloise - seat confirmed", body)


@router.post("/signup")
def signup(body: SignupRequest):
    name = _sanitize_name(body.name)
    conn = get_connection()
    exists = conn.execute("SELECT id FROM users WHERE email=?", (body.email,)).fetchone()
    if exists:
        raise HTTPException(status_code=409, detail="email already registered")
    user = _new_user(name, body.email, _hash(body.password), is_guest=0, verified=1)
    _confirm_email(user)
    return {"token": user["token"], "name": user["name"], "email": user["email"]}


@router.post("/login")
def login(body: LoginRequest):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE email=?", (body.email,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="bad credentials")
    if row["password_hash"] != _hash(body.password):
        raise HTTPException(status_code=401, detail="bad credentials")
    return {"token": row["token"], "name": row["name"], "email": row["email"]}


@router.post("/guest")
def guest(body: GuestLoginRequest):
    name = _sanitize_name(body.name)
    email = str(body.email) if body.email else None
    password_hash = _hash(secrets.token_urlsafe(16))
    conn = get_connection()
    if email:
        taken = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if taken:
            # email already on the books — keep it for the schedule, own the row, no crash
            user = dict(conn.execute("SELECT * FROM users WHERE id=?", (taken["id"],)).fetchone())
            return {
                "token": user["token"], "name": user.get("name") or name, "is_guest": True,
                "email": user["email"], "has_email": True,
            }
    user = _new_user(name, email, password_hash, is_guest=1, verified=1)
    _confirm_email(user)
    return {
        "token": user["token"], "name": user["name"], "is_guest": True,
        "email": user["email"], "has_email": bool(user["email"]),
    }


@router.get("/me")
def me(user: dict = Depends(require_user)):
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "is_guest": user["is_guest"],
        "checkin_time": user["checkin_time"] or "08:00",
        "last_checkin_at": user["last_checkin_at"],
    }


@router.get("/openings")
def openings(user: dict = Depends(require_user)):
    from core.generation import generate_opening_message
    conn = get_connection()
    goals = conn.execute("SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall()
    summary = "; ".join(g["display_title"] for g in goals)
    text, source = generate_opening_message(user["name"], summary, db=conn)
    return {"message": text, "source": source}


@router.post("/logout")
def logout(user: dict = Depends(require_user)):
    return {"ok": True}