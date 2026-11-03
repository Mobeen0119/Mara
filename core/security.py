import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
SESSION_DAYS = 30
MAX_SESSIONS_PER_USER = 10


# --------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def _is_legacy(stored: str) -> bool:
    return len(stored) == 64 and all(c in "0123456789abcdef" for c in stored)


def verify_password(password: str, stored: str):
    """Returns (ok, needs_upgrade). Legacy unsalted SHA-256 hashes still verify
    once, then the caller re-hashes them with scrypt."""
    if not stored:
        return False, False
    if _is_legacy(stored):
        ok = hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)
        return ok, ok
    try:
        _, n, r, p, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(dk_b64)
        got = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(want))
        return hmac.compare_digest(got, want), False
    except Exception:
        return False, False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def burn_password_time(password: str):
    """Spend the same time as a real verify so unknown emails are not detectable by timing."""
    verify_password(password, _DUMMY_HASH)


# ---------------------------------------------------------------- sessions
def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def create_session(conn, user_id) -> str:
    token = new_token()
    expires = _utcnow() + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (?,?,?)",
        (user_id, token_hash(token), _iso(expires)),
    )
    # keep the newest N, drop expired
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(_utcnow()),))
    conn.execute(
        "DELETE FROM sessions WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT ?)",
        (user_id, user_id, MAX_SESSIONS_PER_USER),
    )
    conn.commit()
    return token


def resolve_session(conn, token: str):
    row = conn.execute(
        "SELECT s.id AS sid, s.expires_at, u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=?", (token_hash(token),),
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < _iso(_utcnow()):
        conn.execute("DELETE FROM sessions WHERE id=?", (row["sid"],))
        conn.commit()
        return None
    # sliding expiry, written at most once an hour
    conn.execute(
        "UPDATE sessions SET last_seen_at=datetime('now'), expires_at=? "
        "WHERE id=? AND last_seen_at < datetime('now','-1 hour')",
        (_iso(_utcnow() + timedelta(days=SESSION_DAYS)), row["sid"]),
    )
    conn.commit()
    d = dict(row)
    d.pop("expires_at", None)
    return d


def revoke_session(conn, token: str):
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
    conn.commit()


def revoke_all_sessions(conn, user_id):
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()


class UnsafeURL(ValueError):
    pass


def _allow_private():
    return os.environ.get("ELOISE_ALLOW_PRIVATE_LLM", "").strip().lower() in ("1", "true", "yes", "on")


def validate_outbound_url(raw: str, *, resolve=True) -> str:
    """An LLM endpoint is a URL the server will POST an API key to. Only allow
    https to public hosts; loopback/private addresses (LM Studio, vLLM...) need
    ELOISE_ALLOW_PRIVATE_LLM=1. Cloud-metadata addresses are never allowed."""
    raw = (raw or "").strip()
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL("endpoint must be a full http(s) URL")
    if p.username or p.password:
        raise UnsafeURL("endpoint must not contain credentials")
    host = p.hostname
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80)) if resolve else []
    except socket.gaierror:
        # An unresolvable name cannot reach anything; the request itself will fail
        # with a clear connection error. Literal IPs are still checked below.
        infos = []
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not infos:
        try:
            addrs.append(ipaddress.ip_address(host))
        except ValueError:
            pass
    for a in addrs:
        if a.is_link_local or a.is_multicast or a.is_unspecified or str(a) == "169.254.169.254":
            raise UnsafeURL("that address is not allowed")
        if (a.is_private or a.is_loopback) and not _allow_private():
            raise UnsafeURL(
                "private/loopback endpoints are disabled (set ELOISE_ALLOW_PRIVATE_LLM=1 to allow a local server)"
            )
    if p.scheme == "http" and not (addrs and all(a.is_private or a.is_loopback for a in addrs)):
        raise UnsafeURL("endpoint must use https")
    return raw
