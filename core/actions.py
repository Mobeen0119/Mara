import json
import re
from datetime import date, timedelta

from core import intents
from core.clock import today_for

_GATE = re.compile(
    r"\b(move|push|postpone|reschedule|change|set|make|redraw|redo|rebuild|replan|re-plan|add|create|file|start|extend|"
    r"shorten|remind|check-?in|deadline|due|constraint|block|avoid|never|always|"
    r"busy|free|available|unavailable|can'?t|cannot|won'?t|later|another|other|instead|next|tonight|weekend|"
    r"tomorrow|tmrw|res[a-z]{0,3}edul\w*)\b",
    re.I,
)
_MOVE_WORD = re.compile(r"\b(res[a-z]{0,3}edul\w*|redraw|redo|move|push|postpone|shift|delay)\b", re.I)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

CATALOG = """Actions (reply ONLY JSON {"action": name, "args": {...}} or {"action": "none"}):
- set_deadline {goal_id, deadline YYYY-MM-DD}
- replan {goal_id}  (redraw that goal's schedule)
- set_checkin {time HH:MM}  (daily check-in and email time)
- add_constraint {goal_id, text}  (a recurring busy window or rule, e.g. "class 9-11am")
- move_task {task_id, date YYYY-MM-DD}  (use it for ANY wish to do a task on another day: "she's busy today",
  "can't make it tonight", "not free", "later in the week", "after my exam", "some other evening", "next monday".
  Resolve relative days from Today; with no day named, pick the next day that makes sense. The task is the one
  being discussed, else the nearest pending task of the goal the person is inside.)
- create_goal {title, deadline YYYY-MM-DD}
Use "none" for anything that is not a clear instruction to change the app."""


def parse_day(text, today):
    """A date said in plain words: 2026-11-22, tomorrow (and its typos), friday, 'to 22'. None if absent."""
    low = (text or "").lower()
    base = date.fromisoformat(today)
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    m = re.search(r"\bin\s+(\d{1,2}|a|an|one|two|three|four|five|six|seven)\s+(day|week)s?\b", low)
    if m:
        words = dict(a=1, an=1, one=1, two=2, three=3, four=4, five=5, six=6, seven=7)
        n = words.get(m.group(1)) or int(m.group(1))
        return base + timedelta(days=n * (7 if m.group(2) == "week" else 1))
    if re.search(r"\bnext\s+week\b", low):
        return base + timedelta(days=7)
    if re.search(r"\b(this|the|next)\s+weekend\b", low):
        return base + timedelta(days=(5 - base.weekday()) % 7 or 7)
    if re.search(r"\bday after tom\w*", low):
        return base + timedelta(days=2)
    if re.search(r"\bt[ou]m+[oa]r+[ow]+\b|\btmrw\b|\btomorrow\b", low):
        return base + timedelta(days=1)
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", low):
            gap = (i - base.weekday()) % 7 or 7
            return base + timedelta(
                days=gap + (7 if re.search(rf"\bnext\s+{name}\b", low) and gap < 7 else 0)
            )
    m = re.search(r"\b(?:to|on|for|the)\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?:am|pm|:|\d))", low)
    if m and 1 <= int(m.group(1)) <= 31:
        n = int(m.group(1))
        for add in range(0, 3):
            y, mo = base.year + (base.month - 1 + add) // 12, (base.month - 1 + add) % 12 + 1
            try:
                cand = date(y, mo, n)
            except ValueError:
                continue
            if cand >= base:
                return cand
    return None


def _recent_move(conn, goal_id):
    row = conn.execute("SELECT details FROM goals WHERE id=?", (goal_id,)).fetchone()
    try:
        return json.loads(row["details"] or "{}").get("last_move")
    except Exception:
        return None


def reschedule(conn, user_id, message, today, goal_id=None):
    """'reschedule to tomorrow', 'redraw to 2026-11-22': move the task being discussed to that day."""
    text = (message or "").strip()
    
    if not text or "?" in text or len(text.split()) > 20 or not _MOVE_WORD.search(text):
        return None
    target = parse_day(text, today)
    if target is None or target < date.fromisoformat(today):
        return None
    sql = (
        "SELECT a.id, a.title, a.date, a.goal_id, g.deadline FROM actions a JOIN goals g ON g.id=a.goal_id "
        "WHERE a.user_id=? AND a.status='pending' AND g.status='active' AND a.date>=?"
    )
    args = [user_id, today]
  
    if goal_id:
        sql += " AND a.goal_id=?"
        args.append(goal_id)
    tasks = [dict(r) for r in conn.execute(sql + " ORDER BY a.date, a.start_time LIMIT 30", args).fetchall()]
    if not tasks:
        return None
    best = max(tasks, key=lambda t: intents._score(text, t["title"]), default=None)
    task = best if intents._score(text, best["title"]) >= 1 else tasks[0]
 
    if intents._score(text, task["title"]) < 1 and goal_id:
        recent = _recent_move(conn, goal_id)
        task = next((t for t in tasks if t["date"] == recent), task)
    if task["date"] == target.isoformat():
        return None
    note = " (this also moves the goal's deadline)" if target.isoformat() > task["deadline"] else ""
    return intents._store(
        conn,
        user_id,
        "move_task",
        task["goal_id"],
        {"task_id": task["id"], "date": target.isoformat(), "from": task["date"]},
        f"Move \"{task['title']}\" from {task['date']} to {target.isoformat()}{note}? "
        "Redraws will keep it there.",
    )


def _context(conn, user_id, today, goal_id):
    goals = [
        dict(r)
        for r in conn.execute(
            "SELECT id, display_title, deadline FROM goals WHERE user_id=? AND status='active'", (user_id,)
        ).fetchall()
    ]
    sql = (
        "SELECT a.id, a.title, a.date, a.goal_id FROM actions a JOIN goals g ON g.id=a.goal_id "
        "WHERE a.user_id=? AND a.status='pending' AND g.status='active' AND a.date BETWEEN ? AND ?"
    )
    args = [user_id, today, (date.fromisoformat(today) + timedelta(days=14)).isoformat()]
    if goal_id:
        sql += " AND a.goal_id=?"
        args.append(goal_id)
    tasks = [dict(r) for r in conn.execute(sql + " ORDER BY a.date, a.start_time LIMIT 25", args).fetchall()]
    lines = [f"Today: {today} ({date.fromisoformat(today).strftime('%A')})", "Goals:"] + [
        f"- id {g['id']}: {g['display_title']} (due {g['deadline']})" for g in goals
    ]
    lines += ["Pending tasks, next 14 days:"] + [
        f"- id {t['id']} (goal {t['goal_id']}) {t['date']}: {t['title']}" for t in tasks
    ]
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
            "You turn a person's instruction into one app action. " + CATALOG,
            f"{ctx}\n\nInstruction: {text}",
            timeout=25,
            max_tokens=160,
        )
    except Exception:
        return None
    if not (r is not None and getattr(r, "ok", False) and r.text):
        return None
    m = re.search(r"\{.*\}", r.text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return None
    return _validate(
        conn, user_id, today, data.get("action"), data.get("args") or {}, goals, tasks, note=text
    )


def _validate(conn, user_id, today, action, a, goals, tasks, note=""):
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
        return (
            d if date.fromisoformat(today) <= dd <= date.fromisoformat(today) + timedelta(days=730) else None
        )

    if action == "set_deadline" and gid() and day():
        g = gids[gid()]
        return intents._store(
            conn,
            user_id,
            "set_deadline",
            gid(),
            {"deadline": day()},
            f"Move \"{g['display_title']}\" deadline from {g['deadline']} to {day()} and redraw the plan?",
        )
    if action == "replan" and gid():
        return intents._store(
            conn,
            user_id,
            "replan",
            gid(),
            {"note": (note or "")[:400]},
            f"Redraw the schedule for \"{gids[gid()]['display_title']}\"?",
        )
    if action == "set_checkin" and _TIME.match(str(a.get("time") or "")):
        return intents._store(
            conn,
            user_id,
            "set_checkin",
            None,
            {"time": a["time"]},
            f"Send the daily check-in and email at {a['time']}?",
        )
    if action == "add_constraint" and gid() and str(a.get("text") or "").strip():
        t = str(a["text"]).strip()[:80]
        return intents._store(
            conn,
            user_id,
            "add_constraint",
            gid(),
            {"text": t},
            f"Add \"{t}\" as a fixed constraint on \"{gids[gid()]['display_title']}\" and redraw?",
        )
    if action == "move_task" and day():
        try:
            tid = int(a.get("task_id"))
        except Exception:
            return None
        if tid in tids:
            return intents._store(
                conn,
                user_id,
                "move_task",
                tids[tid]["goal_id"],
                {"task_id": tid, "date": day()},
                f"Move \"{tids[tid]['title']}\" to {day()}?",
            )
    if action == "create_goal":
        title = str(a.get("title") or "").strip()[:120]
        if title and day():
            return intents._store(
                conn,
                user_id,
                "create_goal",
                None,
                {"title": title, "deadline": day()},
                f'File a new goal "{title}" due {day()}?',
            )
    return None


def _move_task(conn, user, gid, payload):
    """Move one task and remember it, so a redraw keeps the new day and leaves the old one free."""
    uid, target = user["id"], payload["date"]
    task = conn.execute(
        "SELECT duration_min, date FROM actions WHERE id=? AND user_id=? AND status='pending'",
        (payload["task_id"], uid),
    ).fetchone()
    if not task:
        return {"message": "That task is already done or gone.", "goal_id": gid}
    conn.execute("UPDATE actions SET date=? WHERE id=?", (target, payload["task_id"]))
    goal = conn.execute("SELECT details, deadline FROM goals WHERE id=? AND user_id=?", (gid, uid)).fetchone()
    try:
        details = json.loads(goal["details"] or "{}")
    except Exception:
        details = {}
    today = today_for(user).isoformat()
    skips = {d for d in details.get("skip_days", []) if d >= today}
    pins = {d: m for d, m in (details.get("pin_days") or {}).items() if d >= today}
    if task["date"] in pins:
        del pins[task["date"]]
    else:
        skips.add(task["date"])
    skips.discard(target)
    pins[target] = int(task["duration_min"] or 60)
    details["skip_days"], details["pin_days"], details["last_move"] = sorted(skips), pins, target
    deadline = max(goal["deadline"], target)
    conn.execute("UPDATE goals SET details=?, deadline=? WHERE id=?", (json.dumps(details), deadline, gid))
    conn.commit()
    from core.routes import goal_routes

    goal_routes.submit_plan(user, gid)
    return {"message": f"Moved to {target}. I'll keep it there when I redraw.", "goal_id": gid}


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
            conn.execute(
                "UPDATE goals SET details=?, plan_status='generating' WHERE id=?", (json.dumps(d), gid)
            )
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
        conn.execute(
            "UPDATE goals SET constraints=? WHERE id=? AND user_id=?", (json.dumps(cons[:20]), gid, uid)
        )
        conn.commit()
        goal_routes.submit_plan(user, gid)
        goal_routes._replan_siblings(user, gid)
        return {"message": "Added. Every goal now plans around it.", "goal_id": gid}
    if kind == "move_task":
        return _move_task(conn, user, gid, payload)
    if kind == "create_goal":
        from core.models import GoalCreateRequest

        res = goal_routes.create_goal(
            GoalCreateRequest(title=payload["title"], deadline=payload["deadline"]), user
        )
        return {"message": "Filed. Drawing the plan.", "goal_id": res["id"]}
    return None