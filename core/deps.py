from fastapi import Depends, Header, HTTPException

from core.database import get_connection
from core.security import resolve_session


def bearer_token(authorization: str = "") -> str:
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def require_user(authorization: str = Header(default="")) -> dict:
    token = bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    user = resolve_session(get_connection(), token)
    if not user:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return user
