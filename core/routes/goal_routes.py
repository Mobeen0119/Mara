import json
import logging
import threading
import time
from datetime import date, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException
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
    StakeCreateRequest,
)

logger = logging.getLogger("eloise")

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


def _goal_payload(g, conn=None):
    g = dict(g) if not isinstance(g, dict) else g
    today = date.today().isoformat()
    try:
        dl = generation.days_remaining(g["deadline"], today)
    except Exception:
        dl = 0
    pressure = None
    if conn is not None:
        try:
            actions = [
                dict(a) for a in conn.execute(
                    "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (g["id"],)
                ).fetchall()
            ]
            blockers = conn.execute(
                "SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)
            ).fetchone()["c"]
            missed = sum(1 for a in actions if a["status"] == "missed")
            pressure = generation.pressure_rating(g, actions, open_blockers=blockers, missed_days=missed)
        except Exception:
            pressure = None
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
        "pressure": pressure,
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
    logger.warning("plan bg: goal %s thread started", goal_id)
    try:
        goal = _goal_or_404(user, goal_id)
    except Exception as exc:
        logger.warning("plan bg: goal %s lookup failed: %s", goal_id, exc)
        return
    lock = _plan_locks.setdefault(goal_id, threading.Lock())
    acquired = lock.acquire(blocking=False)
    if not acquired:
        try:
            conn = get_connection()
            conn.execute("UPDATE goals SET plan_status='active' WHERE id=?", (goal_id,))
            conn.commit()
        except Exception:
            pass
        return
    try:
        time.sleep(0.5)
        conn = get_connection()
        blocked = generation.user_blocked_windows(user)
        # NO hardcoded fallback schedule. Only a real LLM plan is written. We try the
        # model; if it is down or times out, we KEEP whatever plan already exists and
        # retry in a minute. A hardcoded fill-in is never shown to the user.
        try:
            better = generation.generate_plan_or_none(goal, user, db=conn, extra_blocked=blocked)
        except Exception as exc:
            logger.warning("plan bg: goal %s LLM attempt failed: %r", goal_id, exc)
            better = None
        if better:
            _write_plan(conn, goal, better, blocked_min=blocked,
                        provider=generation.last_provider())
            logger.warning("plan bg: goal %s wrote %s LLM entries", goal_id, len(better))
        else:
            existing = conn.execute("SELECT COUNT(*) c FROM actions WHERE goal_id=?", (goal_id,)).fetchone()[0]
            if existing:
                logger.warning("plan bg: goal %s LLM down, keeping %s existing entries", goal_id, existing)
                conn.execute(
                    "UPDATE goals SET plan_status='active', plan_summary=? WHERE id=?",
                    ("kept previous schedule (model unavailable, retrying)", goal_id),
                )
            else:
                logger.warning("plan bg: goal %s LLM down, no plan yet", goal_id)
                conn.execute("UPDATE goals SET plan_status='active' WHERE id=?", (goal_id,))
            conn.commit()
            _schedule_plan_retry(user, goal_id)
    except Exception as exc:
        logger.warning("plan bg: goal %s failed: %r", goal_id, exc)
        try:
            conn = get_connection()
            conn.execute("UPDATE goals SET plan_status='active' WHERE id=?", (goal_id,))
            conn.commit()
        except Exception:
            pass
    finally:
        lock.release()


def _schedule_plan_retry(user, goal_id, delay=60):
    """Try the plan again in ~a minute, as the user wants: 'if there is some issue
    while creating a new one, try in a minute and keep the old one until then'."""
    def _retry():
        time.sleep(delay)
        try:
            _regenerate_plan_bg(user, goal_id)
        except Exception:
            pass
    threading.Thread(target=_retry, daemon=True).start()


def _write_plan(conn, goal, entries, blocked_min=None, provider=None):
    """Insert the chosen entries and update plan_status/summary. Keeps up to three
    blocks per date (so a day is actually managed, not a single slot). Then re-slot
    so no two goals own the same time block on a date."""
    by_date = {}
    for e in entries:
        d = e["date"]
        by_date.setdefault(d, []).append(e)
    final = []
    for d, day_entries in sorted(by_date.items()):
        # keep the earliest 3 distinct blocks for that date
        day_entries.sort(key=lambda e: (_to_min(e.get("start_time")) or 0))
        final.extend(day_entries[:3])
    # Cross-goal conflict resolution: pull this user's OTHER goals' blocks and this
    # user's global blocked windows, shift any overlapping entry to the next free slot.
    final = _resolve_cross_goal_slots(conn, goal, final, blocked_min=blocked_min)
    conn.execute("DELETE FROM actions WHERE goal_id=?", (goal["id"],))
    _insert_actions(conn, goal, final)
    summary = f"{len(final)} tasks across {len(set(e['date'] for e in final))} days"
    if provider in ("ollama", "openrouter"):
        summary += f" · via {provider}"
    conn.execute(
        "UPDATE goals SET plan_status='active', plan_summary=? WHERE id=?",
        (summary, goal["id"]),
    )
    conn.commit()
    return final


def _to_min(t):
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _resolve_cross_goal_slots(conn, goal, entries, blocked_min=None):
    """For each date in `entries`, collect time blocks already scheduled by OTHER goals
    for this user, plus the user's global "always busy" windows. If an entry overlaps,
    shift it to the earliest free 90-min window (9:00, 12:00, 15:00, 18:00 anchors).
    Returns re-slotted entries."""
    if not entries:
        return entries
    user_id = goal["user_id"]
    dates = sorted({e["date"] for e in entries})
    placeholders = ",".join("?" * len(dates))
    busy = {}
    rows = conn.execute(
        f"SELECT date, start_time, end_time FROM actions "
        f"WHERE user_id=? AND goal_id!=? AND date IN ({placeholders}) AND status!='done'",
        (user_id, goal["id"], *dates),
    ).fetchall()
    for r in rows:
        s, e = _to_min(r["start_time"]), _to_min(r["end_time"])
        if s is None or e is None:
            continue
        busy.setdefault(r["date"], []).append((s, e))
    # the user's global windows (e.g. gym 5-7pm) apply to EVERY day in the plan
    if blocked_min:
        for d in dates:
            busy.setdefault(d, []).extend(blocked_min)
    if not busy:
        return entries
    anchors = [(9, 0), (12, 0), (15, 0), (18, 0)]

    def _merge(b):
        b = sorted(b)
        out = []
        for (s, e) in b:
            if out and s < out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        return out

    def _first_free(taken_set, from_min):
        """Earliest 90-min slot starting >= from_min that avoids every busy window.
        Searches the whole day (not just anchors) so it slides past any busy block."""
        obstacles = _merge(list(taken_set))
        cur = from_min
        for (bs, be) in obstacles:
            if be <= cur:
                continue
            if bs >= cur + 90:
                break
            cur = be
        if cur + 90 > 22 * 60:
            return None
        return (cur, cur + 90)

    out = []
    for e in entries:
        d = e["date"]
        s, e2 = _to_min(e.get("start_time")), _to_min(e.get("end_time"))
        taken = busy.get(d, [])
        overlap = s is not None and e2 is not None and any(
            s < be and bs < e2 for (bs, be) in taken
        )
        if not overlap:
            out.append(e)
            continue
        # Prefer the earliest anchor that fits; otherwise slide the whole day for any
        # free 90-min window after the anchor. If the day is genuinely full, drop the
        # block instead of double-booking — a skipped slot beats a liar's schedule.
        placed = None
        surf = _merge(list(taken))
        candidate_starts = [ah * 60 + am for (ah, am) in anchors]
        if s is not None and s not in candidate_starts:
            candidate_starts.append(s)
        for cs in sorted(filter(lambda x: x < 22 * 60, candidate_starts)):
            ce = min(cs + 90, 22 * 60)
            if ce - cs < 90:
                continue
            if any(cs < be and bs < ce for (bs, be) in surf):
                continue
            placed = (cs, ce)
            break
        if placed is None:
            # whole-day slide: find first free gap at/after the anchor, then anywhere
            for cs in sorted(set([a * 60 for a in (9, 12, 15, 18)]) | {s or 540}):
                if cs >= 22 * 60:
                    continue
                f = _first_free(taken, cs)
                if f and (placed is None or f[0] < placed[0]):
                    placed = f
            if placed is None:
                continue  # day is full of your own life; don't fake a slot
        if placed:
            cs, ce = placed
            taken.append((cs, ce))
            busy[d] = taken
            e = {**e, "start_time": f"{cs // 60:02d}:{cs % 60:02d}", "end_time": f"{ce // 60:02d}:{ce % 60:02d}"}
        out.append(e)
    return out


@router.post("/goals")
def create_goal(body: GoalCreateRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    title = body.title.strip()[:120]
    if generation._is_truly_harmful(title):
        raise HTTPException(
            status_code=400,
            detail="That goal doesn't go on this board — Eloise plans real goals, not incest or coercion. A date, a hookup with protection, a memorable night for a partner: all in. That: no.",
        )
    display = title if len(title) <= 60 else title[:57] + "..."
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
    # return questionnaire questions so the frontend can ask them
    questions = generation.goal_questionnaire(title)
    return {"id": goal["id"], "async": True, "questions": questions}


@router.post("/goals/{goal_id}/details")
def save_goal_details(goal_id: int, user: dict = Depends(require_user), answers: dict = Body(default={})):
    """Save questionnaire answers and regenerate the schedule with them."""
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    conn.execute("UPDATE goals SET details=? WHERE id=?", (json.dumps(answers), goal_id))
    conn.commit()
    # regenerate schedule with the new details
    threading.Thread(target=_regenerate_plan_bg, args=(user, goal_id), daemon=True).start()
    return {"ok": True}


@router.get("/goals")
def list_goals(user: dict = Depends(require_user)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM goals WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    goals = []
    for g in rows:
        payload = _goal_payload(g, conn=conn)
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
    payload = _goal_payload(goal, conn=conn)
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
        vals.append(d if len(d) <= 60 else d[:57] + "...")
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
        return _goal_payload(goal, conn=conn)
    vals.append(goal_id)
    conn.execute(f"UPDATE goals SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    return _goal_payload(_goal_or_404(user, goal_id), conn=conn)


@router.post("/goals/{goal_id}/plan")
def regen_plan(goal_id: int, user: dict = Depends(require_user), body: RegenPlanRequest = None):
    """Redraw a goal's schedule — returns INSTANTLY. The real LLM draw happens in a
    background thread so Redraw never blocks the UI for a minute+ of model time.
    Whatever plan exists stays up while the model works (never filler): on success the
    new schedule replaces it; on failure the OLD schedule stays and a retry is queued.
    The goal is flagged plan_status='generating' so the UI can keep showing the current
    schedule and render the result when it settles."""
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    existing = [dict(a) for a in conn.execute(
        "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (goal_id,)
    ).fetchall()]
    conn.execute("UPDATE goals SET plan_status='generating' WHERE id=?", (goal_id,))
    conn.commit()
    threading.Thread(target=_regenerate_plan_bg, args=(user, goal_id), daemon=True).start()
    return {"ok": True, "plan": existing, "source": "redraw-bg"}


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
        conn.commit()
        return {"status": "succeeded"}
    else:
        # Cancellation or delay — require a real reason
        reason = (body.reason or "").strip()
        if not reason:
            raise HTTPException(status_code=400, detail="cancellation requires a reason")
        conn.execute(
            "UPDATE goals SET status='cancelled', plan_status=?, display_title=? WHERE id=?",
            (f"cancelled: {reason}", goal["display_title"], goal_id),
        )
        conn.execute("DELETE FROM actions WHERE goal_id=?", (goal_id,))
        conn.commit()
        roast = (
            f"File closed. Reason: \"{reason}\". "
            "If this is real, fine. If you're just quitting, the next goal won't be easier."
        )
        return {"status": "cancelled", "roast": roast}


@router.post("/actions/{action_id}/toggle")
def toggle_action(action_id: int, user: dict = Depends(require_user)):
    conn = get_connection()
    row = conn.execute(
        "SELECT a.id, a.status, g.id AS goal_id FROM actions a JOIN goals g ON g.id=a.goal_id "
        "WHERE a.id=? AND a.user_id=?",
        (action_id, user["id"]),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="action not found")
    new = "done" if row["status"] != "done" else "pending"
    conn.execute("UPDATE actions SET status=? WHERE id=?", (new, action_id))
    conn.commit()
    return {"id": action_id, "status": new}


@router.post("/goals/{goal_id}/stake")
def place_stake(goal_id: int, body: StakeCreateRequest, user: dict = Depends(require_user)):
    """Pin a loss on the line: if this file misses its deadline, the punishment comes due."""
    _goal_or_404(user, goal_id)
    conn = get_connection()
    existing = conn.execute(
        "SELECT * FROM stakes WHERE user_id=? AND goal_id=? AND status='active'", (user["id"], goal_id)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE stakes SET punishment=? WHERE id=?", (body.punishment.strip(), existing["id"])
        )
        conn.commit()
        return {"ok": True, "stake": {"id": existing["id"], "punishment": body.punishment.strip(), "status": "active"}}
    cur = conn.execute(
        "INSERT INTO stakes (user_id, goal_id, punishment, status) VALUES (?,?,?,'active')",
        (user["id"], goal_id, body.punishment.strip()),
    )
    conn.commit()
    return {"ok": True, "stake": {"id": cur.lastrowid, "punishment": body.punishment.strip(), "status": "active"}}


@router.delete("/goals/{goal_id}/stake")
def remove_stake(goal_id: int, user: dict = Depends(require_user)):
    _goal_or_404(user, goal_id)
    conn = get_connection()
    conn.execute(
        "DELETE FROM stakes WHERE user_id=? AND goal_id=? AND status='active'", (user["id"], goal_id)
    )
    conn.commit()
    return {"ok": True}


@router.get("/momentum")
def momentum(user: dict = Depends(require_user)):
    """Streak + burn-rate + a 0-100 momentum meter derived from work actually done."""
    conn = get_connection()
    stats = generation.momentum_stats(user["id"], db=conn)
    # attach current stakes so the board can show what's on the line
    stakes = [
        dict(s) for s in conn.execute(
            "SELECT s.id, s.goal_id, s.punishment, s.status, g.display_title "
            "FROM stakes s JOIN goals g ON g.id=s.goal_id "
            "WHERE s.user_id=? AND s.status='active'", (user["id"],)
        ).fetchall()
    ]
    stats["stakes"] = stakes
    return stats


@router.post("/goals/{goal_id}/stake/enforce")
def enforce_stake(goal_id: int, user: dict = Depends(require_user)):
    """Eloise calls the debt in and writes the enforcement into the ledger."""
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    stake = conn.execute(
        "SELECT * FROM stakes WHERE user_id=? AND goal_id=? AND status='active'", (user["id"], goal_id)
    ).fetchone()
    if not stake:
        raise HTTPException(status_code=404, detail="no stake on this file")
    from datetime import datetime
    conn.execute(
        "UPDATE stakes SET status='enforced', enforced_at=? WHERE id=?",
        (datetime.utcnow().isoformat(), stake["id"]),
    )
    conn.commit()
    roast = (
        f"The bet's as good as dead and so is the debt. You owe the punishment: "
        f"{stake['punishment']}. I'll be watching the ledger."
    )
    return {"ok": True, "roast": roast, "stake": {"id": stake["id"], "punishment": stake["punishment"], "status": "enforced"}}


@router.post("/onboarding/seed")
def seed_demo(user: dict = Depends(require_user)):
    """First-run: seed two live demo goals so the board has shape the moment you sit down."""
    conn = get_connection()
    cnt = conn.execute("SELECT COUNT(*) c FROM goals WHERE user_id=?", (user["id"],)).fetchone()["c"]
    if cnt > 0:
        conn.execute("UPDATE users SET onboarding_done=1 WHERE id=?", (user["id"],))
        conn.commit()
        return {"seeded": False, "seeded_goals": [], "already": True}
    today = date.today()
    from datetime import timedelta as _td
    demos = [
        {
            "title": "Ship the v3 launch",
            "deadline": (today + _td(days=5)).isoformat(),
            "constraints": ["gym 5-7pm"],
        },
        {
            "title": "Read the floor report",
            "deadline": (today + _td(days=2)).isoformat(),
            "constraints": [],
        },
    ]
    seeded = []
    for d in demos:
        display = d["title"] if len(d["title"]) <= 60 else d["title"][:57] + "..."
        cur = conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, plan_status) "
            "VALUES (?,?,?,?,?,?,?)",
            (user["id"], d["title"], d["deadline"], "09:00", json.dumps(d["constraints"]), display, "pending"),
        )
        seeded.append(cur.lastrowid)
    conn.execute("UPDATE users SET onboarding_done=1 WHERE id=?", (user["id"],))
    conn.commit()
    for gid in seeded:
        threading.Thread(target=_regenerate_plan_bg, args=(user, gid), daemon=True).start()
    return {"seeded": True, "seeded_goals": seeded, "already": False}


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