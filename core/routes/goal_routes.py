import json
import threading
import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from core.database import get_connection
from core.deps import require_user
from core import generation
from core.models import (
    CompleteGoalRequest,
    ConstraintAddRequest,
    DeleteGoalRequest,
    GoalCreateRequest,
    GoalUpdateRequest,
    RegenPlanRequest,
    ReminderSetRequest,
)

router = APIRouter(prefix="/api", tags=["goals"])

_plan_locks = {}


def _goal_or_404(user, goal_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="goal not found")
    return dict(row)


def _goal_payload(g):
    today = date.today().isoformat()
    try:
        dl = generation.days_remaining(g["deadline"], today)
    except Exception:
        dl = 0
    return {
        "id": g["id"],
        "title": g["title"],
        "display_title": g["display_title"],
        "deadline": g["deadline"],
        "days_left": dl,
        "status": g["status"],
        "reminder_time": g["reminder_time"],
        "constraints": json.loads(g["constraints"] or "[]"),
        "plan_status": g["plan_status"],
        "plan_summary": g["plan_summary"],
    }


def _insert_actions(conn, goal, entries):
    for idx, e in enumerate(entries):
        conn.execute(
            "INSERT INTO actions (goal_id, user_id, date, title, description, start_time, end_time, duration_min, status, order_idx) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (goal["id"], goal["user_id"], e["date"], e["title"], "", e.get("start_time", ""),
             e.get("end_time", ""), 60, e.get("status", "pending"), idx),
        )


def _regenerate_plan_bg(user, goal_id):
    goal = _goal_or_404(user, goal_id)
    lock = _plan_locks.setdefault(goal_id, threading.Lock())
    acquired = lock.acquire(blocking=False)
    if not acquired:
        return
    try:
        time.sleep(1)
        conn = get_connection()
        conn.execute("DELETE FROM actions WHERE goal_id=?", (goal_id,))
        conn.commit()
        user_row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        entries = generation.generate_plan(goal, dict(user_row), db=conn)
        sel = {}
        for e in entries:
            d = e["date"]
            hh = int(e["start_time"].split(":")[0]) if e.get("start_time") else 9
            if d not in sel or hh < sel[d]["hh"]:
                sel[d] = {"hh": hh, "entry": e}
        final = [v["entry"] for v in sel.values()]
        _insert_actions(conn, goal, final)
        conn.commit()
        conn.execute(
            "UPDATE goals SET plan_status='active', plan_summary=? WHERE id=?",
            (f"{len(final)} blocks across {len(set(e['date'] for e in final))} days", goal_id),
        )
        conn.commit()
    finally:
        lock.release()


@router.post("/goals")
def create_goal(body: GoalCreateRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    title = body.title.strip()[:120]
    display = title if len(title) <= 24 else title[:21] + "..."
    constraints = json.dumps([c.strip() for c in body.constraints if c.strip()])
    cur = conn.execute(
        "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title) "
        "VALUES (?,?,?,?,?,?)",
        (user["id"], title, body.deadline, body.reminder_time, constraints, display),
    )
    conn.commit()
    goal = dict(conn.execute("SELECT * FROM goals WHERE id=?", (cur.lastrowid,)).fetchone())
    # kick off plan generation in the background thread so response is immediate
    threading.Thread(target=_regenerate_plan_bg, args=(user, goal["id"]), daemon=True).start()
    return {"id": goal["id"], "async": True}


@router.get("/goals")
def list_goals(user: dict = Depends(require_user)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM goals WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    goals = []
    for g in rows:
        payload = _goal_payload(g)
        open_iv = conn.execute(
            "SELECT type FROM interventions WHERE goal_id=? AND user_id=? AND acknowledged=0",
            (g["id"], user["id"]),
        ).fetchone()
        payload["open_intervention_type"] = (open_iv["type"] if open_iv else None)
        goals.append(payload)
    return goals


@router.get("/goals/{goal_id}")
def get_goal(goal_id: int, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    actions = [
        dict(a) for a in conn.execute(
            "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (goal_id,)
        ).fetchall()
    ]
    blockers = [
        dict(b) for b in conn.execute(
            "SELECT * FROM blockers WHERE goal_id=? AND status='open'", (goal_id,)
        ).fetchall()
    ]
    chat_history = json.loads(goal["chat_history"] or "[]")
    progress = generation.progress_stats(goal, actions)
    payload = _goal_payload(goal)
    payload["plan"] = actions
    payload["blockers"] = blockers
    payload["chat_history"] = chat_history
    payload["progress"] = progress
    return payload


@router.put("/goals/{goal_id}")
def update_goal(goal_id: int, body: GoalUpdateRequest, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    fields = []
    vals = []
    if body.title is not None:
        fields.append("title=?")
        vals.append(body.title.strip()[:120])
        fields.append("display_title=?")
        d = body.title.strip()[:120]
        vals.append(d if len(d) <= 24 else d[:21] + "...")
    if body.deadline is not None:
        fields.append("deadline=?")
        vals.append(body.deadline)
    if body.reminder_time is not None:
        fields.append("reminder_time=?")
        vals.append(body.reminder_time)
    if body.constraints is not None:
        fields.append("constraints=?")
        vals.append(json.dumps([c.strip() for c in body.constraints if c.strip()]))
    if not fields:
        return _goal_payload(goal)
    vals.append(goal_id)
    conn.execute(f"UPDATE goals SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    return _goal_payload(_goal_or_404(user, goal_id))


@router.post("/goals/{goal_id}/plan")
def regen_plan(goal_id: int, body: RegenPlanRequest, user: dict = Depends(require_user)):
    _goal_or_404(user, goal_id)
    threading.Thread(target=_regenerate_plan_bg, args=(user, goal_id), daemon=True).start()
    return {"ok": True, "async": True}


# Reads progress (deprecated/compat path, primary is in GET goal)
@router.get("/goals/{goal_id}/progress")
def get_progress(goal_id: int, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    actions = [
        dict(a) for a in conn.execute(
            "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (goal_id,)
        ).fetchall()
    ]
    return generation.progress_stats(goal, actions)


@router.post("/goals/{goal_id}/complete")
def complete_goal(goal_id: int, body: CompleteGoalRequest, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    if body.claimed_success:
        conn.execute("UPDATE goals SET status='succeeded', manually_succeeded=1 WHERE id=?", (goal_id,))
        conn.execute("UPDATE actions SET status='done' WHERE goal_id=? AND status='pending'", (goal_id,))
    else:
        conn.execute("UPDATE goals SET status='active' WHERE id=?", (goal_id,))
    conn.commit()
    return {"status": conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()["status"]}


@router.post("/goals/{goal_id}/delete")
def delete_goal(goal_id: int, body: DeleteGoalRequest, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    conn.execute("DELETE FROM actions WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM blockers WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM chat_messages WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM plans WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
    conn.commit()
    roasts = [
        "Burned. The board breathes easier without it.",
        "Deleted. It never really committed and now it's gone.",
        "Gone. One fewer thing to dodge.",
    ]
    import random
    return {"ok": True, "roast": random.choice(roasts)}


@router.post("/goals/{goal_id}/constraints")
def add_constraint(goal_id: int, body: ConstraintAddRequest, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    cons = json.loads(goal["constraints"] or "[]")
    cons.append(body.text.strip())
    conn.execute("UPDATE goals SET constraints=? WHERE id=?", (json.dumps(cons), goal_id))
    conn.commit()
    return {"constraints": cons}


@router.post("/goals/{goal_id}/reminder")
def set_reminder(goal_id: int, body: ReminderSetRequest, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    conn.execute("UPDATE goals SET reminder_time=? WHERE id=?", (body.reminder_time, goal_id))
    conn.commit()
    return {"ok": True}


@router.post("/goals/{goal_id}/acknowledge")
def acknowledge_goal(goal_id: int, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    conn.execute("DELETE FROM interventions WHERE goal_id=? AND user_id=?", (goal_id, user["id"]))
    conn.commit()
    return {"ok": True}


# expose the background regen helper for the check-in router
def regenerate_plan_bg(user, goal_id):
    _regenerate_plan_bg(user, goal_id)