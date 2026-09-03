import threading
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_connection
from core.deps import require_user
from core import generation
from core.routes.goal_routes import regenerate_plan_bg
from core.models import CheckinRespondRequest

router = APIRouter(prefix="/api", tags=["checkin"])


def _regenerate_active_goals(user):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()
    for r in rows:
        try:
            regenerate_plan_bg(user, r["id"])
        except Exception:
            pass


@router.get("/checkin")
def checkin_status(user: dict = Depends(require_user)):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    last = row["last_checkin_at"]
    if not last:
        return {"due": False, "next_checkin_at": None, "reason": "first check-in not armed yet"}
    from datetime import datetime, timedelta
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        last_dt = datetime.now()
    due = (datetime.now() - last_dt) >= timedelta(hours=24)
    return {"due": due, "last_checkin_at": last}


@router.post("/checkin/prompt")
def checkin_prompt(user: dict = Depends(require_user)):
    conn = get_connection()
    goals = conn.execute(
        "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()
    summary = [g["display_title"] for g in goals]
    text, source = generation.generate_check_in_prompt(user["name"], summary, db=conn)
    return {"message": text, "source": source}


@router.post("/checkin/respond")
def checkin_respond(body: CheckinRespondRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    from datetime import datetime
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
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
        threading.Thread(target=_regenerate_active_goals, args=(user,), daemon=True).start()
    return {"message": resp, "source": source, "done": body.done}