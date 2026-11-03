"""Tiny in-process sliding-window rate limiter (single-worker deployment, as
documented in the README). Keyed by arbitrary strings, e.g. 'login:<ip>:<email>'."""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_lock = threading.Lock()
_hits = defaultdict(deque)


def _prune(q, now, window):
    while q and now - q[0] > window:
        q.popleft()


def hit(key, limit, window, *, consume=True):
    """Record a hit and return True if it is allowed. consume=False only checks."""
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        _prune(q, now, window)
        if len(q) >= limit:
            return False
        if consume:
            q.append(now)
        return True


def count(key, window):
    now = time.monotonic()
    with _lock:
        q = _hits.get(key)
        if not q:
            return 0
        _prune(q, now, window)
        return len(q)


def reset(key):
    with _lock:
        _hits.pop(key, None)


def clear_all():  # tests
    with _lock:
        _hits.clear()


def client_ip(request: Request):
    import os
    if os.environ.get("ELOISE_TRUST_PROXY") == "1":
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(key, limit, window, detail="too many requests, slow down"):
    if not hit(key, limit, window):
        raise HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(int(window))})
