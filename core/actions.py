import json
import re
from datetime import date, timedelta

from core import intents

_GATE = re.compile(
    r"\b(move|push|postpone|reschedule|change|set|make|redraw|redo|rebuild|replan|re-plan|add|create|file|start|extend|"
    r"shorten|remind|check-?in|deadline|due|constraint|block|avoid|never|always)\b", re.I)
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

CATALOG = """Actions (reply ONLY JSON {"action": name, "args": {...}} or {"action": "none"}):
- set_deadline {goal_id, deadline YYYY-MM-DD}
- replan {goal_id}  (redraw that goal's schedule)
- set_checkin {time HH:MM}  (daily check-in and email time)
- add_constraint {goal_id, text}  (a recurring busy window or rule, e.g. "gym 6-7pm")
- move_task {task_id, date YYYY-MM-DD}
- create_goal {title, deadline YYYY-MM-DD}
Use "none" for anything that is not a clear instruction to change the app."""


def _context(conn, user_id, today, goal_id):
    goals = [dict(r) for r in conn.execute(
        "SELECT id, display_title, deadline FROM goals WHERE user_id=? AND status='active'", (user_id,)).fetchall()]
    sql = ("SELECT a.id, a.title, a.date, a.goal_id FROM actions a JOIN goals g ON g.id=a.goal_id "
           "WHERE a.user_id=? AND a.status='pending' AND g.status='active' AND a.date BETWEEN ? AND ?")
    args = [user_id, today, (date.fromisoformat(today) + timedelta(days=7)).isoformat()]
    if goal_id:
        sql += " AND a.goal_id=?"
        args.append(goal_id)
    tasks = [dict(r) for r in conn.execute(sql + " ORDER BY a.date, a.start_time LIMIT 25", args).fetchall()]
    lines = [f"Today: {today}", "Goals:"] + [f"- id {g['id']}: {g['display_title']} (due {g['deadline']})" for g in goals]
    lines += ["Pending tasks, next 7 days:"] + [f"- id {t['id']} (goal {t['goal_id']}) {t['date']}: {t['title']}" for t in tasks]
    if goal_id:
        lines.append(f"The person is inside goal id {goal_id}; 'this goal' means that one.")
    return "\n".join(lines), goals, tasks


def propose(conn, user_id, message, today, goal_id=None, db=None):
    text = (message or "").strip()
    if not text or len(text.split()) > 40 or not _GATE.search(text):
        return None
    ctx, goals, tasks = _context(conn, user_id, today, goal_id)
    from core import generation
    try:
        r = generation.get_manager(db or conn).generate(
            "You turn a person's instruction into one app action. " + CATALOG, f"{ctx}\n\nInstruction: {text}",
            timeout=25, max_tokens=160)
    except Exception:
        return None
    if not (r is not None and getattr(r, "ok", False) and r.text):
        return None
    m = re.search(r"\{.*\}", r.text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return None
    return _validate(conn, user_id, today, data.get("action"), data.get("args") or {}, goals, tasks)


def _validate(conn, user_id, today, action, a, goals, tasks):
    gids = {g["id"]: g for g in goals}
    tids = {t["id"]: t for t in tasks}

    def gid():
        try:
            g = int(a.get("goal_id"))
        except Exception:
            return None
        return g if g in gids else None

    def day():
        d = str(a.get("deadline") or a.get("date") or "")
        try:
            dd = date.fromisoformat(d)
        except Exception:
            return None
        return d if date.fromisoformat(today) <= dd <= date.fromisoformat(today) + timedelta(days=730) else None

    if action == "set_deadline" and gid() and day():
        g = gids[gid()]
        return intents._store(conn, user_id, "set_deadline", gid(), {"deadline": day()},
                              f"Move \"{g['display_title']}\" deadline from {g['deadline']} to {day()} and redraw the plan?")
    if action == "replan" and gid():
        return intents._store(conn, user_id, "replan", gid(), {"note": message[:400]}, f"Redraw the schedule for \"{gids[gid()]['display_title']}\"?")
    if action == "set_checkin" and _TIME.match(str(a.get("time") or "")):
        return intents._store(conn, user_id, "set_checkin", None, {"time": a["time"]},
                              f"Send the daily check-in and email at {a['time']}?")
    if action == "add_constraint" and gid() and str(a.get("text") or "").strip():
        t = str(a["text"]).strip()[:80]
        return intents._store(conn, user_id, "add_constraint", gid(), {"text": t},
                              f"Add \"{t}\" as a fixed constraint on \"{gids[gid()]['display_title']}\" and redraw?")
    if action == "move_task" and day():
        try:
            tid = int(a.get("task_id"))
        except Exception:
            return None
        if tid in tids:
            return intents._store(conn, user_id, "move_task", tids[tid]["goal_id"], {"task_id": tid, "date": day()},
                                  f"Move \"{tids[tid]['title']}\" to {day()}?")
    if action == "create_goal":
        title = str(a.get("title") or "").strip()[:120]
        if title and day():
            return intents._store(conn, user_id, "create_goal", None, {"title": title, "deadline": day()},
                                  f"File a new goal \"{title}\" due {day()}?")
    return None


def execute(conn, user, row, payload):
    from core.routes import goal_routes
    uid, kind, gid = user["id"], row["kind"], row["goal_id"]
    if kind == "set_deadline":
        conn.execute("UPDATE goals SET deadline=? WHERE id=? AND user_id=?", (payload["deadline"], gid, uid))
        conn.commit()
        goal_routes.submit_plan(user, gid)
        return {"message": "Deadline moved. Redrawing the plan.", "goal_id": gid}
    if kind == "replan":
        note = (payload or {}).get("note") if isinstance(payload, dict) else None
        if note:
            g = conn.execute("SELECT details FROM goals WHERE id=? AND user_id=?", (gid, uid)).fetchone()
            try:
                d = json.loads(g["details"] or "{}")
            except Exception:
                d = {}
            d["plan_notes"] = (d.get("plan_notes") or [])[-5:] + [note]
            conn.execute("UPDATE goals SET details=?, plan_status='generating' WHERE id=?", (json.dumps(d), gid))
            conn.commit()
        goal_routes.submit_plan(user, gid, prefer_cloud=True)
        return {"message": "Redrawing it now.", "goal_id": gid}
    if kind == "set_checkin":
        conn.execute("UPDATE users SET checkin_time=? WHERE id=?", (payload["time"], uid))
        conn.commit()
        return {"message": f"Check-in and email now at {payload['time']}."}
    if kind == "add_constraint":
        cur = conn.execute("SELECT constraints FROM goals WHERE id=? AND user_id=?", (gid, uid)).fetchone()
        try:
            cons = json.loads(cur["constraints"] or "[]")
        except Exception:
            cons = []
        cons.append(payload["text"])
        conn.execute("UPDATE goals SET constraints=? WHERE id=? AND user_id=?", (json.dumps(cons[:20]), gid, uid))
        conn.commit()
        goal_routes.submit_plan(user, gid)
        goal_routes._replan_siblings(user, gid)
        return {"message": "Added. Every goal now plans around it.", "goal_id": gid}
    if kind == "move_task":
        conn.execute("UPDATE actions SET date=? WHERE id=? AND user_id=? AND status='pending'",
                     (payload["date"], payload["task_id"], uid))
        conn.commit()
        return {"message": "Moved.", "goal_id": gid}
    if kind == "create_goal":
        from core.models import GoalCreateRequest
        res = goal_routes.create_goal(GoalCreateRequest(title=payload["title"], deadline=payload["deadline"]), user)
        return {"message": "Filed. Drawing the plan.", "goal_id": res["id"]}
    return None