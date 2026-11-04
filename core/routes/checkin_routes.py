import logging
import threading
from datetime import date

from fastapi import APIRouter, Depends

from core import generation, ratelimit
from core.clock import now_for
from core.database import get_connection
from core.deps import require_user
from core.models import CheckinRespondRequest
from core.routes.goal_routes import submit_plan

logger = logging.getLogger("eloise")
router = APIRouter(prefix="/api", tags=["checkin"])

# A "not done" check-in redraws only the most urgent goals, through the shared
# bounded plan pool (not one new thread per goal).
_MAX_REGEN_GOALS = 2


def _hhmm_to_min(text, default=8 * 60):
    try:
        h, m = (text or "").split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default


@router.get("/checkin")
def checkin_status(user: dict = Depends(require_user)):
    """Due once per LOCAL day, from the user's check-in time, when they have live goals."""
    conn = get_connection()
    now = now_for(user)
    has_goal = conn.execute(
        "SELECT 1 FROM goals WHERE user_id=? AND status='active' LIMIT 1", (user["id"],)
    ).fetchone()
    last = user.get("last_checkin_at")
    last_day = (last or "")[:10]
    today = now.date().isoformat()
    at = _hhmm_to_min(user.get("checkin_time"))
    now_min = now.hour * 60 + now.minute
    if not has_goal:
        return {"due": False, "reason": "no active goals", "last_checkin_at": last}
    due = last_day < today and now_min >= at
    return {
        "due": bool(due),
        "last_checkin_at": last,
        "checkin_time": user.get("checkin_time") or "08:00",
        "reason": None if due else ("already checked in today" if last_day == today else "not time yet"),
    }


@router.post("/checkin/prompt")
def checkin_prompt(user: dict = Depends(require_user)):
    ratelimit.enforce(f"llm:{user['id']}", 30, 60)
    conn = get_connection()
    goals = conn.execute(
        "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()
    summary = [g["display_title"] for g in goals]
    text, source = generation.generate_check_in_prompt(user["name"], summary, db=conn)
    return {"message": text, "source": source}


@router.post("/checkin/respond")
def checkin_respond(body: CheckinRespondRequest, user: dict = Depends(require_user)):
    ratelimit.enforce(f"llm:{user['id']}", 30, 60)
    conn = get_connection()
    now = now_for(user).isoformat(sep=" ", timespec="seconds")
    resp, source = generation.generate_check_in_response(user["name"], body.done, db=conn)
    conn.execute(
        "INSERT INTO check_ins (user_id, result) VALUES (?,?)",
        (user["id"], "done" if body.done else "not_done"),
    )
    conn.execute(
        "UPDATE users SET last_checkin_at=?, last_checkin_result=? WHERE id=?",
        (now, "done" if body.done else "not_done", user["id"]),
    )
    conn.commit()
    if not body.done:
        for r in conn.execute(
            "SELECT id FROM goals WHERE user_id=? AND status='active' ORDER BY deadline ASC LIMIT ?",
            (user["id"], _MAX_REGEN_GOALS),
        ).fetchall():
            submit_plan(user, r["id"])
    return {"message": resp, "source": source, "done": body.done}
