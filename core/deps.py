from fastapi import Header, HTTPException

from core.database import get_connection


def require_user(authorization: str = Header(default="")) -> dict:
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="invalid token")
    return dict(row)