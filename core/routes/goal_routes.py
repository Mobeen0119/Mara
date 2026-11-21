import json
import logging
import os
import re as _re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from core import planner, ratelimit
from core.security import _utcnow
from core.database import get_connection
from core.deps import require_user
from core import balance, generation, planner
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


def _get_today(user=None):
    """'Today' in the user's own timezone (honours ELOISE_TEST_TODAY for tests)."""
    from core.clock import today_for

    return today_for(user)


router = APIRouter(prefix="/api", tags=["goals"])

_plan_locks = {}
_user_plan_locks = {}  # one draw at a time per user, so goal B sees goal A's finished blocks
_user_locks_guard = threading.Lock()

# One bounded pool for every background plan draw (the old code spawned a fresh
# Thread per request, unbounded). A goal that is already queued/running is not
# queued twice; a request that arrives mid-draw sets a flag so the draw runs
# once more afterwards with the newest inputs, instead of being silently dropped.
_PLAN_WORKERS = int(os.environ.get("ELOISE_PLAN_WORKERS", "3") or 3)
_pool = None
_queue_lock = threading.Lock()
_queued = set()
_rerun = {}


def _get_pool():
    global _pool
    with _queue_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_PLAN_WORKERS, thread_name_prefix="plan")
        return _pool


def submit_plan(user, goal_id, prefer_cloud=False):
    with _queue_lock:
        if goal_id in _queued:
            _rerun[goal_id] = (user, prefer_cloud or _rerun.get(goal_id, (None, False))[1])
            return False
        _queued.add(goal_id)
    try:
        _get_pool().submit(_run_plan_job, user, goal_id, prefer_cloud)
    except RuntimeError:  # interpreter is exiting
        with _queue_lock:
            _queued.discard(goal_id)
        return False
    return True


def _run_plan_job(user, goal_id, prefer_cloud):
    try:
        _regenerate_plan_bg(user, goal_id, prefer_cloud=prefer_cloud)
    except Exception as exc:
        logger.warning("plan job: goal %s crashed: %r", goal_id, exc)
    finally:
        again = None
        with _queue_lock:
            _queued.discard(goal_id)
            again = _rerun.pop(goal_id, None)
        if again:
            submit_plan(again[0], goal_id, prefer_cloud=again[1])


def shutdown_workers():
    global _pool
    with _queue_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


# Retry bookkeeping per goal. A failed model call used to spawn "another
# thread in 60s" forever — a single bad CPU spike could stack unbounded
# dueling threads. Now each goal gets at most _PLAN_RETRY_MAX scheduled
# retries with exponential backoff, and only ONE pending retry can exist for a
# goal at a time.
_PLAN_RETRY_MAX = 3
_plan_retry_state = {}  # goal_id -> {"attempts": int, "scheduled": bool}
_retry_state_lock = threading.Lock()


def _replan_siblings(user, goal_id):
    rebalance_user(user, skip=goal_id)


def rebalance_user(user, skip=None):
    """Re-split the user's time across ALL active goals. The board is conflict-free the moment
    this returns (tasks are moved/trimmed in place); only goals whose slots changed a lot get
    new titles from the model."""
    try:
        conn = get_connection()
        redraw = balance.rebalance(conn, user)
    except Exception as exc:
        logger.warning("rebalance failed for user %s: %r", user.get("id"), exc)
        return
    for gid in sorted(redraw):
        if gid != skip:
            submit_plan(user, gid)


def _rank(g):
    from core import triage

    return (-triage.importance_of(g), g["deadline"] if not g.get("open_ended") else "9999-12-31", g["id"])


def _outranking_ids(conn, user_id, goal):
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, deadline, open_ended, title, details FROM goals WHERE user_id=? AND status='active' AND id!=?",
            (user_id, goal["id"]),
        ).fetchall()
    ]
    me = _rank(goal)
    return {r["id"] for r in rows if _rank(r) < me}


def _goal_or_404(user, goal_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="goal not found")
    return dict(row)


def _goal_payload(g, conn=None, user=None):
    g = dict(g) if not isinstance(g, dict) else g
    today_d = _get_today(user)
    today = today_d.isoformat()
    try:
        dl = generation.days_remaining(g["deadline"], today)
    except Exception:
        dl = 0
    pressure = None
    if conn is not None:
        try:
            actions = [
                dict(a)
                for a in conn.execute(
                    "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (g["id"],)
                ).fetchall()
            ]
            blockers = conn.execute(
                "SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)
            ).fetchone()["c"]
            missed = sum(1 for a in actions if a["status"] == "missed")
            pressure = generation.pressure_rating(
                g, actions, open_blockers=blockers, missed_days=missed, today=today_d
            )
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


def _dur(e):
    s, f = _to_min(e.get("start_time")), _to_min(e.get("end_time"))
    return (f - s) if (s is not None and f is not None and f > s) else 60


def _insert_actions(conn, goal, entries):
    base = conn.execute(
        "SELECT COALESCE(MAX(order_idx), -1) + 1 FROM actions WHERE goal_id=?", (goal["id"],)
    ).fetchone()[0]
    for idx, e in enumerate(entries, start=base):
        conn.execute(
            "INSERT INTO actions (goal_id, user_id, date, title, description, start_time, end_time, duration_min, status, order_idx) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                goal["id"],
                goal["user_id"],
                e["date"],
                e["title"],
                "",
                e.get("start_time", ""),
                e.get("end_time", ""),
                _dur(e),
                e.get("status", "pending"),
                idx,
            ),
        )


def active_goal_blocked_windows(conn, user):
    """Busy windows shared by every goal: the one merged routine's workout time plus
    every other always-busy window (class, work...) typed anywhere, newest wins."""
    from core import routine

    return routine.blocked_windows(routine.merged(conn, user))


def _routine_windows(goal, conn=None, user=None):
    """Sleep and meals as (start, end) windows, overnight spans split at midnight so
    the conflict resolver never slides a block into them."""
    from core import routine

    if conn is not None and user is not None:
        wins = routine.human_windows(routine.merged(conn, user))
    else:
        try:
            details = json.loads(goal.get("details") or "{}")
        except Exception:
            details = {}
        wins = generation.parse_routine_windows(details) or generation._DEFAULT_HUMAN_WINDOWS
    out = []
    for s, e in wins:
        if e <= s:
            out.append((s, 24 * 60))
            out.append((0, e))
        else:
            out.append((s, e))
    return out


OPEN_HORIZON_DAYS = 28
OPEN_ROLL_DAYS = 14

PLAN_STEPS = [
    "Read your routine and other goals",
    "Placed sleep, meals and fixed windows",
    "Checked free time against your calendar",
    "Asked the model to write the days",
    "Fitted the draft into real slots",
    "Saved the schedule",
]
_plan_progress = {}


def _mark(goal_id, idx):
    _plan_progress[goal_id] = idx


@router.get("/goals/{goal_id}/plan-progress")
def plan_progress(goal_id: int, user: dict = Depends(require_user)):
    _goal_or_404(user, goal_id)
    return {"steps": PLAN_STEPS, "at": _plan_progress.get(goal_id, -1)}


def _grow_size_hint(conn, goal_id, ratio):
    """The model said a quarter+ of this goal's slots were too short for the work: ask the allocator
    for ~25% longer slots next solve. Bounded at 1.5 so it can never run away; lowering needs no hint."""
    if ratio < 0.25:
        return
    row = conn.execute("SELECT details FROM goals WHERE id=?", (goal_id,)).fetchone()
    d = json.loads(row["details"] or "{}") if row else {}
    cur = float(d.get("size_hint") or 1.0)
    if cur >= 1.5:
        return
    d["size_hint"] = round(min(1.5, cur + 0.25), 2)
    conn.execute("UPDATE goals SET details=? WHERE id=?", (json.dumps(d), goal_id))
    conn.commit()


def _regenerate_plan_bg(user, goal_id, prefer_cloud=False):
    logger.warning("plan bg: goal %s thread started", goal_id)
    try:
        goal = _goal_or_404(user, goal_id)
    except Exception as exc:
        logger.warning("plan bg: goal %s lookup failed: %s", goal_id, exc)
        return
    with _user_locks_guard:
        ulock = _user_plan_locks.setdefault(user["id"], threading.Lock())
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
    ulock.acquire()
    try:
        conn = get_connection()
        fresh = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        if fresh:
            user = dict(fresh)  # blocked windows / timezone may have changed since queued
        # ELOISE IS THE SINGLE CONTROL UNIT: the user does not declare a
        # routine per goal. If they blocked gym 5-7am in ANY goal, every goal
        # knows. So each draw respects the union of the user's global blocked
        # windows AND every ACTIVE goal's own constraint windows
        # (gym/uni/sleep/whatever), parsed once here.
        _mark(goal_id, 0)
        from core import routine as _routine

        rt = _routine.merged(conn, user)
        generation.set_routine(rt)
        blocked = _routine.blocked_windows(rt) + _routine_windows(goal, conn, user)
        from core import triage as _triage

        goal["details"] = json.dumps(
            {**json.loads(goal.get("details") or "{}"), "importance": _triage.rate_importance(conn, goal)}
        )
        _d0 = json.loads(goal.get("details") or "{}")
        _cached = _d0.get("brief") if _d0.get("brief_for") == goal["title"] else None
        _brief = _cached or generation.goal_brief(goal["title"], conn, goal["deadline"])
        if _brief and not _cached:
            _fresh = json.loads(
                conn.execute("SELECT details FROM goals WHERE id=?", (goal_id,)).fetchone()["details"] or "{}"
            )
            conn.execute(
                "UPDATE goals SET details=? WHERE id=?",
                (json.dumps({**_fresh, "brief": _brief, "brief_for": goal["title"]}), goal_id),
            )
            conn.commit()
        generation.set_brief(_brief)
        kind = generation.effort_profile(goal, 30)["kind"]
        wins = [tuple(rt["workout"])] if rt["workout"] else []
        if kind == "habit" and not wins:
            wake = rt["sleep"][1] % 1440
            ws = wake + 60 if rt.get("peak") == "morning" else 18 * 60
            wins = [(ws, ws + 60)]
            rt = dict(
                rt,
                assumed=list(rt.get("assumed", []))
                + [f"you work out {generation.fmt_min(ws)}-{generation.fmt_min(ws + 60)}"],
            )
            generation.set_routine(rt)
        generation.set_workout_windows(wins if kind == "habit" else [])
        _mark(goal_id, 1)
        if kind == "habit" and wins:
            blocked = [b for b in blocked if tuple(b) not in set(wins)]
        # The slot grid must avoid the user's REAL calendar, not just declared
        # busy windows: other goals already occupy specific time on specific
        # days, and an LLM slot that lands on a booked block gets dropped
        # wholesale by conflict resolution (the whole plan silently collapsing
        # to "0 tasks").
        _mark(goal_id, 2)
        today_iso = _get_today(user).isoformat()
        # ONE joint solve for every active goal: this goal's slots are carved out of the same
        # day as everyone else's, so nothing can overlap. Existing tasks are moved onto it
        # right now (no model call) so the board is conflict-free before any title is drawn.
        alloc, _rep, _specs, _t, _now_min = balance.compute(conn, user, rt)
        generation.set_allocation(alloc)
        balance.reslot(conn, user, alloc, _t, _now_min)
        busy_by_date = {}
        dated = _routine.dated_busy(rt, today_iso, goal.get("deadline") or today_iso)
        # NO hardcoded fallback schedule. Only a real LLM plan is written. We
        # try the model; if it is down or times out, we KEEP whatever plan
        # already exists and retry in a minute. A hardcoded fill-in is never
        # shown to the user.
        written = None
        _mark(goal_id, 3)
        try:
            better = generation.generate_plan_or_none(
                goal,
                user,
                db=conn,
                extra_blocked=blocked,
                prefer_cloud=prefer_cloud,
                busy_by_date=busy_by_date,
            )
        except Exception as exc:
            logger.warning("plan bg: goal %s LLM attempt failed: %r", goal_id, exc)
            better = None
        partial = False
        if better and not generation.last_plan_complete():
            # a slice failed or many titles were rejected: writing this would ERASE days the
            # old plan still covers. Keep the old plan and retry; only a first draw may be partial.
            had = conn.execute(
                "SELECT COUNT(*) c FROM actions WHERE goal_id=? AND status='pending'", (goal_id,)
            ).fetchone()["c"]
            if had:
                logger.warning("plan bg: goal %s draw incomplete, keeping %s existing tasks", goal_id, had)
                better, partial = None, True
        if better:
            resolve_blocked = blocked
            if generation.effort_profile(goal, 30)["kind"] == "event":
                # dinner is part of the event evening, so meal windows don't push it around
                resolve_blocked = [
                    b
                    for b in blocked
                    if not ((b[1] - b[0]) % 1440 <= 90 and 12 * 60 <= b[0] % 1440 < 22 * 60)
                ]
            _mark(goal_id, 4)
            written = _write_plan(
                conn,
                goal,
                better,
                blocked_min=resolve_blocked,
                provider=generation.last_provider(),
                dated_busy=dated,
                keep_in_place=(kind == "habit"),
            )
            if written:
                _mark(goal_id, 5)
                _reset_plan_retries(goal_id)
                logger.warning("plan bg: goal %s wrote %s LLM entries", goal_id, len(written))
                _first_day_mail(conn, user, goal_id)
                try:
                    _grow_size_hint(conn, goal_id, generation.too_small_ratio())
                except Exception as exc:
                    logger.warning("size hint failed: %r", exc)
                try:
                    for gid in sorted(balance.rebalance(conn, user, rt) - {goal_id}):
                        submit_plan(user, gid)
                except Exception as exc:
                    logger.warning("rebalance after draw failed: %r", exc)
        if not written:
            # LLM produced nothing usable, or the model never ran. Distinguish
            # the two HONESTLY: if the failure was the user's own windows
            # leaving zero usable slots (the fallback grid came back empty),
            # report THAT instead of blaming the model — there is no point
            # retrying when the real problem is the blocked windows. Every
            # other failure keeps the previous plan and retries.
            blocked_msg = generation.plan_blocked_reason()
            if blocked_msg:
                try:
                    rows, free = _routine.fixed_windows_report(conn, user)
                    top = sorted(rows, key=lambda r: -((r[3] - r[2]) % 1440))[:3]
                    named = "; ".join(f"'{r[0]}' ({r[1]})" for r in top)
                    blocked_msg = (
                        f"Only {free // 60}h{free % 60:02d}m of your day is free after sleep, meals and fixed windows."
                        + (f" Biggest blockers: {named}." if named else "")
                        + " Remove or shorten one and I'll redraw."
                    )
                except Exception as exc:
                    logger.warning("blocked diagnosis failed: %r", exc)
                logger.warning("plan bg: goal %s has no usable free windows, marking blocked", goal_id)
                conn.execute(
                    "UPDATE goals SET plan_status='blocked', plan_summary=? WHERE id=?",
                    (blocked_msg, goal_id),
                )
                conn.commit()
                return
            existing = conn.execute("SELECT COUNT(*) c FROM actions WHERE goal_id=?", (goal_id,)).fetchone()[
                0
            ]
            err = generation.last_llm_error()
            tail = f": {err}" if err else ""
            summary = (
                "kept previous schedule (the new draw came back incomplete, retrying)"
                if partial
                else f"kept previous schedule (model error{tail}, retrying)"
            )
            if existing:
                logger.warning("plan bg: goal %s LLM down, keeping %s existing entries", goal_id, existing)
            else:
                logger.warning("plan bg: goal %s LLM down, no schedule yet — will retry", goal_id)
                summary = f"no plan drawn yet (model error{tail}), retrying"
            conn.execute(
                "UPDATE goals SET plan_status='active', plan_summary=? WHERE id=?",
                (summary, goal_id),
            )
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
        generation.set_allocation(None)
        ulock.release()
        lock.release()


def _action_violates_goal_windows(goal):
    """True if a pending task lands inside one of the goal's OWN blocked windows
    (e.g. a plan drawn before the constraint existed, or by the old code that ignored
    per-goal windows). Those plans are self-contradictory and must be redrawn."""
    try:
        goal = dict(goal)
        cons = json.loads(goal.get("constraints") or "[]")
    except Exception:
        return False
    windows = []
    habit = generation.effort_profile(goal, 30)["kind"] == "habit"
    for c in cons:
        w = generation.parse_time_window(c)
        if w and not (habit and generation.is_workout_label(c)):  # a habit goal USES its gym window
            windows.append(w)
    if not windows:
        return False
    c = get_connection()
    for a in c.execute(
        "SELECT start_time, end_time FROM actions WHERE goal_id=? AND status!='done'",
        (goal["id"],),
    ).fetchall():
        a0, a1 = _to_min(a["start_time"]), _to_min(a["end_time"])
        if a0 is None or a1 is None:
            continue
        for w0, w1 in windows:
            if a0 < w1 and a1 > w0:
                return True
    return False


def heal_stale_plans():
    """Startup repair for plans that never materialized:
    - goals stranded in 'generating' (a redraw thread died mid-draw, e.g. on a process
      kill — the 0.5s lead-in sleep means it can die before ever writing its status),
    - goals 'active' but with ZERO tasks (an old empty-write badged them 'active' with a
      confidently wrong '0 tasks across 0 days' summary and nothing ever re-drew them).
    Both get reset to 'active' and, if they still have no tasks, a fresh LOCAL-first
    generation is kicked at boot so the board and chat have something real to work with."""
    today = (_get_today() - timedelta(days=1)).isoformat()
    try:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT g.id, g.user_id, g.plan_status
            FROM goals g
            WHERE g.plan_status NOT IN ('cancelled', 'succeeded', 'blocked')
              AND g.plan_status NOT LIKE 'cancelled:%'
              AND g.plan_status NOT LIKE 'succeeded:%'
              AND g.status NOT IN ('cancelled', 'succeeded')
              AND g.deadline >= ?
              AND (g.plan_status = 'generating'
                   OR NOT EXISTS (SELECT 1 FROM actions a WHERE a.goal_id = g.id))
            """,
            (today,),
        ).fetchall()
    except Exception as exc:
        logger.warning("plan bg: heal scan failed: %r", exc)
        return
    for g in rows:
        gid, uid = g["id"], g["user_id"]
        try:
            conn.execute(
                "UPDATE goals SET plan_status='active' WHERE id=? AND plan_status='generating'",
                (gid,),
            )
            n = conn.execute("SELECT COUNT(*) c FROM actions WHERE goal_id=?", (gid,)).fetchone()[0]
            conn.commit()
            if n == 0:
                user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                if user:
                    logger.warning(
                        "plan bg: goal %s has no tasks — regenerating (was %s)", gid, g["plan_status"]
                    )
                    submit_plan(dict(user), gid)
        except Exception as exc:
            logger.warning("plan bg: heal goal %s failed: %r", gid, exc)
    # Hardcoded-title purge: the grid placeholder '— day N part 1' used to
    # leak into the DB via the old substitution path. Those goals contain only
    # canned filler titles that violate the user's hard rule. Delete ONLY the
    # junk rows (concrete tasks survive) and let the no-hardcoded regen path
    # try once (keeps the goal honest and empty-if-unfilled and retries if the
    # model still fails).
    try:
        act_rows = conn.execute(
            """
            SELECT a.id, a.goal_id, a.title FROM actions a
            JOIN goals g ON g.id = a.goal_id
            WHERE a.status = 'pending'
              AND g.status NOT IN ('cancelled', 'succeeded')
              AND g.plan_status NOT LIKE 'cancelled:%'
              AND g.plan_status NOT LIKE 'succeeded:%'
            """,
        ).fetchall()
    except Exception as exc:
        logger.warning("plan bg: purge scan failed: %r", exc)
        act_rows = []
    junk_by_goal = {}
    for r in act_rows:
        if _re.search(r"\bday\s+\d+\s+part\s+\d+", r["title"] or "", _re.I):
            junk_by_goal.setdefault(int(r["goal_id"]), []).append(int(r["id"]))
    for gid, ids in junk_by_goal.items():
        try:
            conn.execute(f"DELETE FROM actions WHERE id IN ({','.join('?' * len(ids))})", ids)
            conn.commit()
            user = conn.execute(
                "SELECT * FROM users WHERE id=(SELECT user_id FROM goals WHERE id=?)", (gid,)
            ).fetchone()
            if user:
                logger.warning("plan bg: goal %s purged placeholder titles — regenerating", gid)
                submit_plan(dict(user), gid)
        except Exception as exc:
            logger.warning("plan bg: purge placeholders goal %s failed: %r", gid, exc)
    # Self-contradictory plans: a goal whose pending tasks sit INSIDE its own
    # blocked windows (drawn before the constraint existed, or by code that
    # ignored per-goal windows). Redraw so the schedule honors the goal's
    # stated constraints.
    try:
        goal_rows = conn.execute(
            """
            SELECT g.id, g.user_id, g.constraints
            FROM goals g
            WHERE g.plan_status NOT IN ('cancelled', 'succeeded', 'blocked')
              AND g.plan_status NOT LIKE 'cancelled:%'
              AND g.plan_status NOT LIKE 'succeeded:%'
              AND g.status NOT IN ('cancelled', 'succeeded')
              AND g.deadline >= ?
            """,
            ((_get_today() - timedelta(days=1)).isoformat(),),
        ).fetchall()
    except Exception as exc:
        logger.warning("plan bg: constraint scan failed: %r", exc)
        goal_rows = []
    for g in goal_rows:
        try:
            if _action_violates_goal_windows(g):
                uid = g["user_id"]
                user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                if user:
                    logger.warning(
                        "plan bg: goal %s tasks violate its own blocked windows — redrawing", g["id"]
                    )
                    submit_plan(dict(user), g["id"])
        except Exception as exc:
            logger.warning("plan bg: constraint check goal %s failed: %r", g["id"], exc)


def _reset_plan_retries(goal_id):
    with _retry_state_lock:
        _plan_retry_state.pop(goal_id, None)


def _schedule_plan_retry(user, goal_id, delay=None):
    """Try the plan again with backoff, as the user wants: 'if there is some issue
    while creating a new one, try in a minute and keep the old one until then'.
    Bounded and deduplicated: never more than ONE pending retry per goal, at most
    _PLAN_RETRY_MAX retries total (60s, 2m, 4m), then we honestly stop
    instead of stacking threads forever."""
    with _retry_state_lock:
        st = _plan_retry_state.setdefault(goal_id, {"attempts": 0, "scheduled": False})
        if st["scheduled"]:
            return
        if st["attempts"] >= _PLAN_RETRY_MAX:
            try:
                conn = get_connection()
                conn.execute(
                    "UPDATE goals SET plan_status='active', plan_summary=? WHERE id=?",
                    ("model unavailable — keeping previous schedule", goal_id),
                )
                conn.commit()
            except Exception:
                pass
            return
        st["attempts"] += 1
        st["scheduled"] = True
        attempt = st["attempts"]
    wait = delay if delay is not None else min(60 * (2 ** (attempt - 1)), 900)

    def _retry():
        with _retry_state_lock:
            st = _plan_retry_state.get(goal_id)
            if st:
                st["scheduled"] = False
        submit_plan(user, goal_id)

    t = threading.Timer(wait, _retry)
    t.daemon = True
    t.start()


def _first_day_mail(conn, user, goal_id):
    try:
        from core import emails

        u = dict(conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())
        g = dict(conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())
        emails.send_first_day(conn, u, g)
    except Exception as exc:
        logger.warning("first-day mail failed for goal %s: %r", goal_id, exc)


def _write_plan(conn, goal, entries, blocked_min=None, provider=None, dated_busy=None, keep_in_place=False):
    """Insert the chosen entries and update plan_status/summary. Keeps up to three
    blocks per date (so a day is actually managed, not a single slot). Then re-slot
    so no two goals own the same time block on a date."""
    if not entries:
        # An empty write must NEVER succeed: it would badge the goal "active"
        # with a confidently wrong "0 tasks across 0 days" summary and leave
        # the schedule blank. Treat it as a failed draw and let the retry path
        # handle it.
        return None
    by_date = {}
    for e in entries:
        d = e["date"]
        by_date.setdefault(d, []).append(e)
    final = []
    for d, day_entries in sorted(by_date.items()):
        day_entries.sort(key=lambda e: (_to_min(e.get("start_time")) or 0))
        final.extend(day_entries)
    # Cross-goal conflict resolution: pull this user's OTHER goals' blocks and
    # this user's global blocked windows, shift any overlapping entry to the
    # next free slot.
    final = generation.reorder_naturally(final, goal.get("title") or goal.get("display_title") or "", conn)
    _alloc = generation.get_allocation()
    if _alloc is None or goal["id"] not in _alloc:
        final = _resolve_cross_goal_slots(
            conn, goal, final, blocked_min=blocked_min, dated_busy=dated_busy, keep_in_place=keep_in_place
        )
    if not final:
        # Even a non-empty draw can resolve to NOTHING once real cross-goal
        # conflicts are applied (every candidate slot was already booked by
        # another goal). An empty write must NEVER succeed: it would DELETE
        # the goal's existing tasks and badge it "active" with a confidently
        # wrong "0 tasks" summary. Keep whatever plan exists and let the retry
        # path re-run the draw against the real calendar.
        return None
    # A redraw replaces only the FUTURE pending tasks inside the window it just
    # drew. Done and missed history is what momentum, streaks and pressure are
    # computed from, and tasks beyond the detailed window stay as they are.
    first_d, last_d = final[0]["date"], max(e["date"] for e in final)
    try:
        ln = generation._local_now(conn, goal, None)
        today_s, now_s = ln.date().isoformat(), ln.strftime("%H:%M")
    except Exception:
        today_s, now_s = "", "00:00"
    try:
        whole = (
            generation.scope_for(
                max(generation.days_remaining(goal.get("deadline") or last_d, today_s or first_d), 0)
            )
            == "daily"
        )
    except Exception:
        whole = False
    conn.execute(
        "DELETE FROM actions WHERE goal_id=? AND status='pending' AND date>=? AND date<=? "
        "AND NOT (date=? AND start_time<?)",
        (goal["id"], first_d, "9999-12-31" if whole else last_d, today_s, now_s),
    )
    conn.execute(
        "DELETE FROM actions WHERE goal_id=? AND status='pending' AND date<? AND "
        "(date>? OR (date=? AND start_time>=?))",
        (goal["id"], first_d, today_s, today_s, now_s),
    )
    _insert_actions(conn, goal, final)
    outline = generation.last_outline()
    conn.execute(
        "UPDATE goals SET outline=? WHERE id=?", (json.dumps(outline) if outline else "", goal["id"])
    )
    summary = f"{len(final)} tasks across {len(set(e['date'] for e in final))} days"
    if provider and provider != "fallback" and provider != "none":
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


def _resolve_cross_goal_slots(conn, goal, entries, blocked_min=None, dated_busy=None, keep_in_place=False):
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
        f"SELECT date, start_time, end_time, goal_id FROM actions "
        f"WHERE user_id=? AND goal_id!=? AND date IN ({placeholders}) AND status!='done'",
        (user_id, goal["id"], *dates),
    ).fetchall()
    winners = _outranking_ids(conn, user_id, goal)
    load = {}
    for r in rows:
        if r["goal_id"] not in winners:
            continue
        a, b = _to_min(r["start_time"]), _to_min(r["end_time"])
        if a is not None and b is not None:
            load[r["date"]] = load.get(r["date"], 0) + (b - a)
        s, e = _to_min(r["start_time"]), _to_min(r["end_time"])
        if s is None or e is None:
            continue
        busy.setdefault(r["date"], []).append((s, e))
    # the user's global windows (e.g. gym 5-7pm) apply to EVERY day in the
    # plan
    if blocked_min:
        for d in dates:
            busy.setdefault(d, []).extend(blocked_min)
    for d, wl in (dated_busy or {}).items():
        if d in dates:
            busy.setdefault(d, []).extend(wl)
    if not busy:
        return entries
    anchors = [(9, 0), (12, 0), (15, 0), (18, 0)]

    def _merge(b):
        b = sorted(b)
        out = []
        for s, e in b:
            if out and s < out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        return out

    def _first_free(taken_set, from_min, need=90):
        """Earliest 90-min slot starting >= from_min that avoids every busy window.
        Searches the whole day (not just anchors) so it slides past any busy block."""
        obstacles = _merge(list(taken_set))
        cur = from_min
        for bs, be in obstacles:
            if be <= cur:
                continue
            if bs >= cur + need:
                break
            cur = be
        if cur + need > 22 * 60:
            return None
        return (cur, cur + need)

    out = []
    for e in entries:
        d = e["date"]
        s, e2 = _to_min(e.get("start_time")), _to_min(e.get("end_time"))
        taken = busy.get(d, [])
        overlap = s is not None and e2 is not None and any(s < be and bs < e2 for (bs, be) in taken)
        dur = max(5, min((e2 - s) if s is not None and e2 is not None else 90, 180))
        if not overlap:
            load[d] = load.get(d, 0) + dur
            out.append(e)
            continue
        if keep_in_place or load.get(d, 0) + dur > planner.DAY_FOCUS_CAP:
            continue
        # Prefer the earliest anchor that fits; otherwise slide the whole day
        # for any free 90-min window after the anchor. If the day is genuinely
        # full, drop the block instead of double-booking — a skipped slot
        # beats a liar's schedule.
        placed = None
        surf = _merge(list(taken))
        candidate_starts = [ah * 60 + am for (ah, am) in anchors]
        if s is not None and s not in candidate_starts:
            candidate_starts.append(s)
        for cs in sorted(filter(lambda x: x < 22 * 60, candidate_starts)):
            ce = min(cs + dur, 22 * 60)
            if ce - cs < dur:
                continue
            if any(cs < be and bs < ce for (bs, be) in surf):
                continue
            placed = (cs, ce)
            break
        if placed is None:
            # whole-day slide: find first free gap at/after the anchor, then
            # anywhere
            for cs in sorted(set([a * 60 for a in (9, 12, 15, 18)]) | {s or 540}):
                if cs >= 22 * 60:
                    continue
                f = _first_free(taken, cs, dur)
                if f and (placed is None or f[0] < placed[0]):
                    placed = f
            if placed is None:
                continue  # day is full of your own life; don't fake a slot
        if placed:
            cs, ce = placed
            taken.append((cs, ce))
            busy[d] = taken
            load[d] = load.get(d, 0) + dur
            e = {
                **e,
                "start_time": f"{cs // 60:02d}:{cs % 60:02d}",
                "end_time": f"{ce // 60:02d}:{ce % 60:02d}",
            }
        out.append(e)
    return out


GOAL_REFUSAL = (
    "That goal doesn't go on this board — Eloise plans real goals, not incest or coercion. "
    "A date, a hookup with protection, a memorable night for a partner: all in. That: no."
)


def _guard_title(title):
    """The same content guardrail on EVERY path that writes a goal title (create AND edit)."""
    if generation._is_truly_harmful(title or ""):
        raise HTTPException(status_code=400, detail=GOAL_REFUSAL)


def _clean_details(answers):
    """Questionnaire answers go straight into model prompts: flat, small, strings only."""
    if not isinstance(answers, dict) or len(answers) > 30:
        raise HTTPException(status_code=400, detail="answers must be a small flat object")
    out = {}
    for k, v in answers.items():
        if not isinstance(k, str) or len(k) > 40:
            raise HTTPException(status_code=400, detail="bad answer key")
        if isinstance(v, (int, float)):
            v = str(v)
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail=f"answer '{k}' must be text")
        out[k] = v.strip()[:500]
    for v in out.values():
        _guard_title(v)
    return out


@router.post("/goals")
def create_goal(body: GoalCreateRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    title = body.title.strip()[:400]
    _guard_title(title)
    open_ended = 0
    deadline = body.deadline
    if not deadline:
        open_ended = 1
        deadline = (_get_today(user) + timedelta(days=OPEN_HORIZON_DAYS)).isoformat()
    elif deadline < _get_today(user).isoformat():
        raise HTTPException(status_code=400, detail="that deadline has already passed")
    display = title if len(title) <= 60 else title[:57] + "..."
    constraints = json.dumps([c.strip() for c in body.constraints if c.strip()])
    cur = conn.execute(
        "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, open_ended) "
        "VALUES (?,?,?,?,?,?,?)",
        (user["id"], title, deadline, body.reminder_time, constraints, display, open_ended),
    )
    conn.commit()
    goal = dict(conn.execute("SELECT * FROM goals WHERE id=?", (cur.lastrowid,)).fetchone())
    # kick off plan generation in the background thread so response is
    # immediate
    submit_plan(user, goal["id"])
    _replan_siblings(user, goal["id"])  # everyone else shrinks to make room, right now
    # questions are fetched right after via POST /goals/{id}/clarify (the
    # model writes them for this specific goal); returning none here keeps
    # creation instant.
    return {"id": goal["id"], "async": True}


@router.post("/goals/{goal_id}/clarify")
def clarify_goal(goal_id: int, user: dict = Depends(require_user)):
    """Short model-written questions for this goal, minus anything already known.
    If the model is unreachable there are no questions and the response lists what
    the plan will assume instead."""
    from core import routine

    goal = _goal_or_404(user, goal_id)
    ratelimit.enforce(f"llm:{user['id']}", 30, 60)
    conn = get_connection()
    rt = routine.merged(conn, user)
    unknown = []
    if any(a.startswith("you sleep") for a in rt["assumed"]):
        unknown.append("when they sleep and wake")
    if any(a.startswith("meals") for a in rt["assumed"]):
        unknown.append("when they eat")
    questions = generation.generate_clarify_questions(
        goal.get("title") or goal.get("display_title") or "",
        db=conn,
        deadline=goal.get("deadline") or "",
        known=routine.describe(rt) if not rt["assumed"] else "",
        unknown=unknown,
    )
    return {"questions": questions, "assumed": rt["assumed"], "routine": routine.describe(rt)}


def _routine_view(rt):
    from core import routine

    fm = generation.fmt_min
    rows = [
        {
            "key": "sleep",
            "label": "Sleep",
            "from": fm(rt["sleep"][0]),
            "to": fm(rt["sleep"][1]),
            "source": rt["src"].get("sleep", "assumed"),
        }
    ]
    for n, w in rt["meals"].items():
        rows.append(
            {
                "key": n,
                "label": n.capitalize(),
                "from": fm(w[0]),
                "to": fm(w[1]),
                "source": rt["src"].get(n, "assumed"),
            }
        )
    if rt["workout"]:
        rows.append(
            {
                "key": "workout",
                "label": "Gym / workout",
                "from": fm(rt["workout"][0]),
                "to": fm(rt["workout"][1]),
                "source": rt["src"].get("workout", "assumed"),
            }
        )
    fixed = [{"from": fm(w[0]), "to": fm(w[1]), "days": "every day"} for w in rt["other"]]
    names = "Mon Tue Wed Thu Fri Sat Sun".split()
    fixed += [
        {"from": fm(w[0]), "to": fm(w[1]), "days": ", ".join(names[i] for i in sorted(d))}
        for w, d in rt["dated"]
    ]
    return {
        "routine": routine.describe(rt),
        "assumed": rt["assumed"],
        "sources": rt["src"],
        "rows": rows,
        "fixed": fixed,
    }


@router.get("/routine")
def get_routine(user: dict = Depends(require_user)):
    from core import routine

    return _routine_view(routine.merged(get_connection(), user))


def _sane_sleep(a, b):
    """Sleep given as bed->wake. Longer than 14h means the two were typed the wrong way round: swap them.
    Under 3h (or 0h, e.g. 12am-12am) cannot be a night's sleep: ask again instead of saving it."""
    length = (b - a) % 1440
    if length > 14 * 60:
        a, b = b, a
        length = (b - a) % 1440
    if length < 3 * 60:
        raise HTTPException(
            status_code=422, detail="Sleep must be at least 3 hours (for example 11pm to 7am)."
        )
    return [a, b]


@router.post("/routine")
def set_routine(payload: dict = Body(...), user: dict = Depends(require_user)):
    from core import routine

    conn = get_connection()
    patch = {}
    for key, val in (payload or {}).items():
        if (
            key not in ("sleep", "breakfast", "lunch", "dinner", "workout")
            or not isinstance(val, (list, tuple))
            or len(val) != 2
        ):
            continue
        t = generation.parse_clock_list(f"{val[0]} {val[1]}")
        if len(t) < 2:
            raise HTTPException(status_code=422, detail=f"Could not read the times for {key}.")
        if key in ("breakfast", "lunch", "dinner"):
            patch.setdefault("meals", {})[key] = [t[0], t[1] if t[1] - t[0] >= 20 else t[0] + 45]
        elif key == "sleep":
            patch[key] = _sane_sleep(t[0], t[1])
        else:
            patch[key] = [t[0], t[1]]
    if not patch:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    routine.save_override(conn, user["id"], patch)
    for g in conn.execute(
        "SELECT id FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall():
        submit_plan(user, g["id"])
    return _routine_view(routine.merged(conn, user))


@router.post("/goals/{goal_id}/details")
def save_goal_details(goal_id: int, user: dict = Depends(require_user), answers: dict = Body(default={})):
    """Save questionnaire answers and regenerate the schedule with them."""
    goal = _goal_or_404(user, goal_id)
    answers = _clean_details(answers)
    conn = get_connection()
    conn.execute("UPDATE goals SET details=? WHERE id=?", (json.dumps(answers), goal_id))
    conn.commit()
    # regenerate schedule with the new details
    submit_plan(user, goal_id)
    return {"ok": True}


@router.get("/goals")
def list_goals(user: dict = Depends(require_user)):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM goals WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    goals = []
    for g in rows:
        payload = _goal_payload(g, conn=conn, user=user)
        open_iv = conn.execute(
            "SELECT type FROM interventions WHERE goal_id=? AND user_id=? AND acknowledged=0",
            (g["id"], user["id"]),
        ).fetchone()
        payload["open_intervention_type"] = open_iv["type"] if open_iv else None
        goals.append(payload)
    return goals


@router.get("/goals/{goal_id}")
def get_goal(goal_id: int, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    actions = [
        dict(a)
        for a in conn.execute(
            "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (goal_id,)
        ).fetchall()
    ]
    blockers = [
        dict(b)
        for b in conn.execute(
            "SELECT * FROM blockers WHERE goal_id=? AND status='open'", (goal_id,)
        ).fetchall()
    ]
    chat_history = json.loads(goal["chat_history"] or "[]")
    progress = generation.progress_stats(goal, actions, today=_get_today(user))
    payload = _goal_payload(goal, conn=conn, user=user)
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
        _guard_title(body.title)
    if body.deadline is not None and body.deadline < _get_today(user).isoformat():
        raise HTTPException(status_code=400, detail="that deadline has already passed")
    if body.title is not None:
        fields.append("title=?")
        vals.append(body.title.strip()[:400])
        fields.append("display_title=?")
        d = body.title.strip()[:400]
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
        return _goal_payload(goal, conn=conn, user=user)
    vals.append(goal_id)
    conn.execute(f"UPDATE goals SET {', '.join(fields)} WHERE id=?", vals)
    # Changing the inputs that made a goal 'blocked' (no usable free time)
    # un-blocks it: kick a redraw so a relaxed window draws an honest schedule
    # right away.
    was_blocked = str(goal.get("plan_status") or "").startswith("blocked")
    shapes_plan = (
        (body.deadline is not None and body.deadline != goal.get("deadline"))
        or (body.constraints is not None)
        or (body.title is not None and body.title.strip() != (goal.get("title") or ""))
    )
    if was_blocked:
        conn.execute("UPDATE goals SET plan_status='active', plan_summary='' WHERE id=?", (goal_id,))
    conn.commit()
    if was_blocked or shapes_plan:
        submit_plan(user, goal_id)
    if body.constraints is not None:
        _replan_siblings(user, goal_id)
    return _goal_payload(_goal_or_404(user, goal_id), conn=conn, user=user)


@router.post("/goals/{goal_id}/plan")
def regen_plan(goal_id: int, user: dict = Depends(require_user), body: RegenPlanRequest = None):
    """Redraw a goal's schedule — returns INSTANTLY. The real LLM draw happens in a
    background thread so Redraw never blocks the UI for a minute+ of model time.
    Whatever plan exists stays up while the model works (never filler): on success the
    new schedule replaces it; on failure the OLD schedule stays and a retry is queued.
    The goal is flagged plan_status='generating' so the UI can keep showing the current
    schedule and render the result when it settles."""
    goal = _goal_or_404(user, goal_id)
    ratelimit.enforce(f"regen:{user['id']}", 30, 3600, "that's a lot of redraws — give the model a breather")
    conn = get_connection()
    existing = [
        dict(a)
        for a in conn.execute(
            "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (goal_id,)
        ).fetchall()
    ]
    conn.execute("UPDATE goals SET plan_status='generating' WHERE id=?", (goal_id,))
    conn.commit()
    # Explicit redraw = the one path that spends OpenRouter calls on purpose,
    # so it runs on the cloud model (fast, parallel with chat). Everything
    # automatic (create, retries, checkin, settings change) stays on the free
    # local model.
    submit_plan(user, goal_id, prefer_cloud=True)
    return {"ok": True, "plan": existing, "source": "redraw-bg"}


# Reads progress (deprecated/compat path, primary is in GET goal)
@router.get("/goals/{goal_id}/progress")
def get_progress(goal_id: int, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    actions = [
        dict(a)
        for a in conn.execute(
            "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (goal_id,)
        ).fetchall()
    ]
    return generation.progress_stats(goal, actions, today=_get_today(user))


@router.post("/goals/{goal_id}/complete")
def complete_goal(goal_id: int, body: CompleteGoalRequest, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    if body.claimed_success:
        conn.execute("UPDATE goals SET status='succeeded', manually_succeeded=1 WHERE id=?", (goal_id,))
        conn.execute("UPDATE actions SET status='done' WHERE goal_id=? AND status='pending'", (goal_id,))
        conn.commit()
        rebalance_user(user)  # the freed time goes back to the others
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
        conn.execute("DELETE FROM actions WHERE goal_id=? AND status!='done'", (goal_id,))
        conn.commit()
        rebalance_user(user)
        roast = (
            f'File closed. Reason: "{reason}". '
            "If this is real, fine. If you're just quitting, the next goal won't be easier."
        )
        return {"status": "cancelled", "roast": roast}


def close_if_finished(conn, goal_id):
    g = conn.execute("SELECT open_ended, status FROM goals WHERE id=?", (goal_id,)).fetchone()
    if not g or not g["open_ended"] or g["status"] != "active":
        return False
    left = conn.execute(
        "SELECT COUNT(*) FROM actions WHERE goal_id=? AND status='pending'", (goal_id,)
    ).fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM actions WHERE goal_id=? AND status='done'", (goal_id,)
    ).fetchone()[0]
    if left or not done:
        return False
    conn.execute("UPDATE goals SET status='succeeded' WHERE id=?", (goal_id,))
    conn.commit()
    return True


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
    closed = close_if_finished(conn, row["goal_id"]) if new == "done" else False
    return {"id": action_id, "status": new, "goal_closed": closed}


@router.post("/goals/{goal_id}/stake")
def place_stake(goal_id: int, body: StakeCreateRequest, user: dict = Depends(require_user)):
    """Pin a loss on the line: if this file misses its deadline, the punishment comes due."""
    _goal_or_404(user, goal_id)
    conn = get_connection()
    existing = conn.execute(
        "SELECT * FROM stakes WHERE user_id=? AND goal_id=? AND status='active'", (user["id"], goal_id)
    ).fetchone()
    if existing:
        conn.execute("UPDATE stakes SET punishment=? WHERE id=?", (body.punishment.strip(), existing["id"]))
        conn.commit()
        return {
            "ok": True,
            "stake": {"id": existing["id"], "punishment": body.punishment.strip(), "status": "active"},
        }
    cur = conn.execute(
        "INSERT INTO stakes (user_id, goal_id, punishment, status) VALUES (?,?,?,'active')",
        (user["id"], goal_id, body.punishment.strip()),
    )
    conn.commit()
    return {
        "ok": True,
        "stake": {"id": cur.lastrowid, "punishment": body.punishment.strip(), "status": "active"},
    }


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
    stats = generation.momentum_stats(user["id"], db=conn, today=_get_today(user))
    # attach current stakes so the board can show what's on the line
    stakes = [
        dict(s)
        for s in conn.execute(
            "SELECT s.id, s.goal_id, s.punishment, s.status, g.display_title "
            "FROM stakes s JOIN goals g ON g.id=s.goal_id "
            "WHERE s.user_id=? AND s.status='active'",
            (user["id"],),
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
    if goal["status"] == "succeeded":
        raise HTTPException(status_code=400, detail="that file was closed as done; there is no debt")
    if goal["status"] == "active" and goal["deadline"] >= _get_today(user).isoformat():
        raise HTTPException(status_code=400, detail="the deadline hasn't passed yet; the bet is still live")
    from datetime import datetime

    conn.execute(
        "UPDATE stakes SET status='enforced', enforced_at=? WHERE id=?",
        (_utcnow().isoformat(), stake["id"]),
    )
    conn.commit()
    roast = (
        f"The bet's as good as dead and so is the debt. You owe the punishment: "
        f"{stake['punishment']}. I'll be watching the ledger."
    )
    return {
        "ok": True,
        "roast": roast,
        "stake": {"id": stake["id"], "punishment": stake["punishment"], "status": "enforced"},
    }


@router.post("/onboarding/seed")
def seed_demo(user: dict = Depends(require_user)):
    """First-run: seed two live demo goals so the board has shape the moment you sit down."""
    conn = get_connection()
    cnt = conn.execute("SELECT COUNT(*) c FROM goals WHERE user_id=?", (user["id"],)).fetchone()["c"]
    if cnt > 0:
        conn.execute("UPDATE users SET onboarding_done=1 WHERE id=?", (user["id"],))
        conn.commit()
        return {"seeded": False, "seeded_goals": [], "already": True}
    today = _get_today(user)
    from datetime import timedelta as _td

    demos = [
        {
            "title": "Ship the v3 launch",
            "deadline": (today + _td(days=5)).isoformat(),
            "constraints": [],
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
            (
                user["id"],
                d["title"],
                d["deadline"],
                "09:00",
                json.dumps(d["constraints"]),
                display,
                "pending",
            ),
        )
        seeded.append(cur.lastrowid)
    conn.execute("UPDATE users SET onboarding_done=1 WHERE id=?", (user["id"],))
    conn.commit()
    for gid in seeded:
        submit_plan(user, gid)
    return {"seeded": True, "seeded_goals": seeded, "already": False}


@router.post("/goals/{goal_id}/delete")
def delete_goal(goal_id: int, body: DeleteGoalRequest, user: dict = Depends(require_user)):
    goal = _goal_or_404(user, goal_id)
    conn = get_connection()
    conn.execute("DELETE FROM actions WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM blockers WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM chat_messages WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM plans WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM stakes WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM interventions WHERE goal_id=?", (goal_id,))
    conn.execute("DELETE FROM attachments WHERE goal_id=?", (goal_id,))
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
    if getattr(body, "removeIndex", None) is not None:
        idx = body.removeIndex
        if 0 <= idx < len(cons):
            cons.pop(idx)
    else:
        txt = (getattr(body, "text", "") or "").strip()
        if txt:
            cons.append(txt)
    conn.execute("UPDATE goals SET constraints=? WHERE id=?", (json.dumps(cons), goal_id))
    # Relaxing/removing constraints can un-block a goal whose windows ate all
    # free time.
    if str(goal.get("plan_status") or "").startswith("blocked"):
        conn.execute("UPDATE goals SET plan_status='active', plan_summary='' WHERE id=?", (goal_id,))
    conn.commit()
    submit_plan(user, goal_id)
    _replan_siblings(user, goal_id)
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
    submit_plan(user, goal_id)