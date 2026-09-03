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


@router.post("/signup")
def signup(body: SignupRequest):
    name = _sanitize_name(body.name)
    conn = get_connection()
    exists = conn.execute("SELECT id FROM users WHERE email=?", (body.email,)).fetchone()
    if exists:
        raise HTTPException(status_code=409, detail="email already registered")
    user = _new_user(name, body.email, _hash(body.password), is_guest=0, verified=1)
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
    email = None
    password_hash = _hash(secrets.token_urlsafe(16))
    user = _new_user(name, email, password_hash, is_guest=1, verified=1)
    return {"token": user["token"], "name": user["name"], "is_guest": True}


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