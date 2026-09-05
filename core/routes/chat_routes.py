import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.database import get_connection
from core.deps import require_user
from core import generation
from core.models import ChatRequest, LinkRequest, NudgeRequest

router = APIRouter(prefix="/api", tags=["chat"])


@router.get("/eloise")
def eloise_state(user: dict = Depends(require_user)):
    """Feed for the console panel: live telemetry + Eloise's current mood, derived
    from the real board (pressure of the hottest goal, momentum, stakes, uptime)."""
    conn = get_connection()
    today = date.today().isoformat()
    goals = [dict(g) for g in conn.execute(
        "SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()]
    top = None
    for g in goals:
        acts = [dict(a) for a in conn.execute("SELECT * FROM actions WHERE goal_id=?", (g["id"],)).fetchall()]
        blk = conn.execute("SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)).fetchone()["c"]
        miss = sum(1 for a in acts if a["status"] == "missed")
        pr = generation.pressure_rating(g, acts, open_blockers=blk, missed_days=miss)
        if top is None or pr["score"] > top["score"]:
            top = {"goal": g["display_title"], "score": pr["score"], "label": pr["label"],
                   "days_left": pr["days_left"], "remaining_hours": pr["remaining_hours"]}
    mom = generation.momentum_stats(user["id"], db=conn)
    stakes = conn.execute(
        "SELECT s.id, s.punishment, g.display_title FROM stakes s JOIN goals g ON g.id=s.goal_id "
        "WHERE s.user_id=? AND s.status='active'", (user["id"],)
    ).fetchall()
    # boot/uptime feel
    created_at = conn.execute("SELECT created_at FROM users WHERE id=?", (user["id"],)).fetchone()["created_at"]
    boot = created_at or "seated"
    # mood from the last Eloise line
    last_row = conn.execute(
        "SELECT content FROM general_chat WHERE user_id=? AND role='eloise' ORDER BY id DESC LIMIT 1", (user["id"],)
    ).fetchone()
    mood = generation.classify_mood(last_row["content"] if last_row else "")
    return {
        "mood": mood["mood"],
        "status": mood["status"],
        "uptime": str(boot)[:19].replace("T", " "),
        "top_pressure": top,
        "momentum": mom["momentum"],
        "streak": mom["current_streak"],
        "burn_hours": mom["burn_hours_per_day"],
        "stakes": [{"punishment": s["punishment"], "goal": s["display_title"]} for s in stakes],
    }


def _active_board_snapshot(conn, user_id):
    """A compact, live digest of the user's current board: active goals with their
    deadlines, today's pending/done tasks, and any unresolved open flags."""
    rows = conn.execute(
        "SELECT display_title, deadline, constraints, details FROM goals WHERE user_id=? AND status='active'", (user_id,)
    ).fetchall()
    today = date.today().isoformat()
    goals = []
    for r in rows:
        try:
            dd = generation.days_remaining(r["deadline"], today)
        except Exception:
            dd = 0
        cons = ""
        try:
            clist = json.loads(r["constraints"] or "[]")
            if clist:
                cons = f", blocked: {', '.join(clist[:3])}"
        except Exception:
            cons = ""
        details = generation.goal_details_summary(r["details"])
        detail_str = f" [details: {details}]" if details else ""
        goals.append(f"'{r['display_title']}' (due {r['deadline']}, {dd} days left{cons}{detail_str})")
    pending = []
    done = []
    tasks = conn.execute(
        "SELECT a.title, a.start_time, a.date, a.status, g.display_title "
        "FROM actions a JOIN goals g ON g.id=a.goal_id "
        "WHERE a.user_id=? AND a.date=? AND g.status='active' ORDER BY a.start_time",
        (user_id, today),
    ).fetchall()
    for t in tasks:
        entry = f"{t['display_title']}: {t['title']}"
        if t["start_time"]:
            entry += f" @ {t['start_time']}"
        (done if t["status"] == "done" else pending).append(entry)
    powers = conn.execute(
        "SELECT type, message FROM interventions WHERE user_id=? AND acknowledged=0", (user_id,)
    ).fetchall()
    flags = [f"{p['type']}: {p['message']}" for p in powers]
    summary = {
        "active_goals": goals,
        "today_pending": pending,
        "today_done": done,
        "open_flags": flags,
    }
    return "; ".join(
        [
            (f"active goals: {', '.join(goals)}" if goals else "no active goals"),
            (f"today's tasks: {', '.join(pending)}" if pending else "no tasks scheduled today"),
            (f"open flags: {', '.join(flags)}" if flags else ""),
        ]
    ).strip("; ")


def _add_message(conn, user_id, goal_id, role, content):
    if goal_id:
        conn.execute(
            "INSERT INTO chat_messages (goal_id, user_id, role, content) VALUES (?,?,?,?)",
            (goal_id, user_id, role, content),
        )
    else:
        conn.execute(
            "INSERT INTO general_chat (user_id, role, content) VALUES (?,?,?)",
            (user_id, role, content),
        )
    conn.commit()


def _history(conn, goal_id, user_id, limit=8):
    if goal_id:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE goal_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (goal_id, user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM general_chat WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [f"{r['role']}: {r['content']}" for r in reversed(rows)]


@router.post("/chat")
def chat(body: ChatRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    goal_id = body.goal_id
    goal = None
    if goal_id:
        goal = conn.execute(
            "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="goal not found")
        goal = dict(goal)

    _add_message(conn, user["id"], goal_id, "user", body.message)

    goals_summary = [
        r["display_title"]
        for r in conn.execute(
            "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    ]

    if goal_id and goal and generation.completion_detected(body.message):
        conn.execute("UPDATE goals SET status='succeeded', manually_succeeded=1 WHERE id=?", (goal_id,))
        conn.execute("UPDATE actions SET status='done' WHERE goal_id=? AND status='pending'", (goal_id,))
        conn.commit()
        reply = (
            "Claimed and closed. You said it's done, so the file reads done. If you're lying, "
            "the next check-in will find out. Don't insult my ledger with a fake 'done'."
        )
        _add_message(conn, user["id"], goal_id, "eloise", reply)
        return {"reply": reply, "goal_status": "succeeded", "source": "completion"}

    history = "\n".join(_history(conn, goal_id, user["id"]))
    goal_name = goal["display_title"] if goal else "general"

    # Content safety: refuse harmful requests before they reach the LLM.
    if generation._is_truly_harmful(body.message):
        refusal = "That kind of request doesn't belong on this board, and I won't help plan it. Whatever this is hoping to be — it isn't a task I'll execute. Put a real goal on the table and we'll move."
        _add_message(conn, user["id"], goal_id, "eloise", refusal)
        return {"reply": refusal, "source": "guardrail"}

    if goal_id:
        # goal-scoped chat: pull that goal's actual scheduled tasks so the model can
        # point at real concrete next steps instead of hand-waving.
        plan_rows = conn.execute(
            "SELECT date, title, start_time FROM actions WHERE goal_id=? AND status!='done' ORDER BY date, start_time LIMIT 12",
            (goal_id,),
        ).fetchall()
        plan_lines = "\n".join(
            f"- {r['date']} {r['start_time'] or ''}: {r['title']}" for r in plan_rows
        )
        text, source = generation.generate_chat_reply(
            user["name"], goal_name, history, body.message, db=conn, plan_lines=plan_lines
        )
    else:
        board = _active_board_snapshot(conn, user["id"])
        text, source = generation.generate_global_chat_reply(
            user["name"], [board], history, body.message, db=conn
        )
    _add_message(conn, user["id"], goal_id, "eloise", text)
    return {"reply": text, "source": source}


@router.get("/chat")
def chat_history(user: dict = Depends(require_user)):
    conn = get_connection()
    goal_id = None
    rows = conn.execute(
        "SELECT * FROM general_chat WHERE user_id=?", (user["id"],)
    ).fetchall()
    msgs = [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]
    if rows:
        goal_id = None
    return {"goal_id": goal_id, "messages": msgs}


@router.get("/chat/goals/{goal_id}")
def goal_chat_history(goal_id: int, user: dict = Depends(require_user)):
    conn = get_connection()
    goal = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])
    ).fetchone()
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE goal_id=? AND user_id=?", (goal_id, user["id"])
    ).fetchall()
    msgs = [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]
    return {"goal_id": goal_id, "messages": msgs}


@router.post("/chat/quick")
def quick_command(body: dict, user: dict = Depends(require_user)):
    """Fast, deterministic command responses — no LLM, instant. Kicks off the quick-command
    chips in the chat so Eloise answers about the live board the moment you tap."""
    import re as _re
    from datetime import date as _d
    cmd = str((body or {}).get("command") or "").strip().lower()
    conn = get_connection()
    today = _d.today().isoformat()

    if cmd in ("today", "plate"):
        items = conn.execute(
            "SELECT a.title, a.start_time, a.end_time, a.status, a.goal_id, a.date, "
            "g.display_title FROM actions a JOIN goals g ON g.id=a.goal_id "
            "WHERE a.user_id=? AND a.date=? AND g.status='active' ORDER BY a.start_time",
            (user["id"], today),
        ).fetchall()
        if not items:
            return {"reply": "Nothing booked today. That's a red flag or a lie — file something real.", "source": "quick"}
        lines = [f"{t['start_time'] or 'anytime'} {t['display_title']} — {t['title']} [{t['status']}]" for t in items]
        return {"reply": "Today's plate:\n" + "\n".join(lines), "source": "quick"}

    if cmd in ("score", "pressure", "crisis"):
        goals = [dict(g) for g in conn.execute("SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall()]
        if not goals:
            return {"reply": "No active files. No pressure. Suspiciously calm in here.", "source": "quick"}
        out = []
        for g in goals:
            acts = [dict(a) for a in conn.execute("SELECT * FROM actions WHERE goal_id=?", (g["id"],)).fetchall()]
            blk = conn.execute("SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)).fetchone()["c"]
            miss = sum(1 for a in acts if a["status"] == "missed")
            pr = generation.pressure_rating(g, acts, open_blockers=blk, missed_days=miss)
            out.append(f"{g['display_title']}: {pr['score']}/10 — {pr['label']} ({pr['days_left']}d left)")
        out.sort(key=lambda s: 0)
        return {"reply": "Pressure board:\n" + "\n".join(out), "source": "quick"}

    if cmd in ("urgent", "fire"):
        goals = [dict(g) for g in conn.execute("SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall()]
        best = None
        for g in goals:
            acts = [dict(a) for a in conn.execute("SELECT * FROM actions WHERE goal_id=?", (g["id"],)).fetchall()]
            blk = conn.execute("SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)).fetchone()["c"]
            miss = sum(1 for a in acts if a["status"] == "missed")
            pr = generation.pressure_rating(g, acts, open_blockers=blk, missed_days=miss)
            if best is None or pr["score"] > best[0]:
                best = (pr["score"], g, pr)
        if not best:
            return {"reply": "No fires. The building is empty — great, that's boring.", "source": "quick"}
        _, g, pr = best
        act = conn.execute(
            "SELECT * FROM actions WHERE goal_id=? AND status!='done' AND date<=? ORDER BY date LIMIT 1",
            (g["id"], today),
        ).fetchone()
        urgent = act["title"] if act else g["display_title"]
        return {"reply": f"Fire at {pr['score']}/10: '{g['display_title']}'. Crack open '{urgent}' now.", "source": "quick"}

    if cmd in ("momentum", "streak"):
        m = generation.momentum_stats(user["id"], db=conn)
        verdict = "on fire" if m["momentum"] >= 70 else "ticking" if m["momentum"] >= 40 else "stalled"
        return {"reply":
            f"Momentum {m['momentum']}/100 — {verdict}. Best run: {m['best_streak']} straight days. "
            f"Current streak: {m['current_streak']}. Averaging {m['burn_hours_per_day']}h/day of done work.",
            "source": "quick"}

    if cmd in ("reschedule", "fuckit", "fuck it", "f-it", "f*ckit", "shove it"):
        goals = conn.execute("SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall()
        if not goals:
            return {"reply": "Nothing to reschedule. 'Fuck it' only counts when there's work to dodge.", "source": "quick"}
        # push all pending actions one day out
        for g in goals:
            conn.execute(
                "UPDATE actions SET date=date(date,'+1 day') WHERE goal_id=? AND status='pending'", (g["id"],)
            )
        conn.commit()
        return {"reply": "Done. Everything pending just slid a day. Nice one — you paid for it tomorrow.", "source": "quick"}

    if cmd in ("stakes", "debt"):
        stakes = conn.execute(
            "SELECT s.punishment, s.status, g.display_title FROM stakes s JOIN goals g ON g.id=s.goal_id "
            "WHERE s.user_id=? AND s.status='active'", (user["id"],)
        ).fetchall()
        if not stakes:
            return {"reply": "Nothing staked. Play it safe if you want — I respect a coward who at least admits it.", "source": "quick"}
        return {"reply": "On the line:\n" + "\n".join(f"- {s['punishment']} (if {s['display_title']} slips)" for s in stakes), "source": "quick"}

    if cmd in ("awesome", "cool", "nice", "impressive", "how"):
        return {
            "reply": "Because it's not magic, it's a ledger you can't lie to: live pressure scores, sawed-streak "
                     "momentum, stakes pinned to deadlines, and a schedule that redraws around what's actually blocked. "
                     "The polish is just fear wearing mascara.",
            "source": "quick",
        }

    return {"reply": "I don't know that command. Try Today, Score, Fire, Momentum, Fuck it, or Stakes.", "source": "quick"}
    conn = get_connection()
    goals_summary = [
        r["display_title"]
        for r in conn.execute(
            "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    ]
    goal_name = goals_summary[0] if goals_summary else "your goal"
    text, source = generation.generate_link_suggestion(user["name"], goal_name, body.message, db=conn)
    return {"reply": text, "source": source}


@router.post("/chat/nudge")
def nudge(body: NudgeRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    goal = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (body.goal_id, user["id"])
    ).fetchone()
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")
    goals_summary = [
        r["display_title"]
        for r in conn.execute(
            "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    ]
    text, source = generation.generate_nudge(user["name"], "; ".join(goals_summary), goal["display_title"], db=conn)
    conn.execute(
        "INSERT INTO interventions (user_id, goal_id, type, message) VALUES (?,?,?,?)",
        (user["id"], body.goal_id, "nudge", text),
    )
    conn.commit()
    return {"reply": text, "source": source}