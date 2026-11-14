import json
import re
from datetime import datetime, timedelta, timezone

_DONE = re.compile(r"\b(done|finished|completed?|did|nailed|wrapped up|crossed off|mark(?:ed)?[^.]{0,40}\bdone)\b", re.I)
_CANCEL = re.compile(r"\b(cancel|abandon|drop|quit|scrap|give up on|forget about|delete|remove)\b", re.I)
_SLIDE = re.compile(r"\b(push|slide|move|postpone|delay)\b[^.]{0,30}\b(everything|all)\b[^.]{0,20}\b(tomorrow|a day)\b", re.I)
_NEG = re.compile(r"\b(not|n't|never|haven't|hasn't|yet|almost|nearly|should|will|gonna|going to|if|when)\b|\bdidn", re.I)
_ALL = re.compile(r"\b(everything|all (?:of )?(?:it|my|the|tasks?)|all tasks|whole)\b", re.I)
_GOALISH = re.compile(r"\b(goal|project|plan|whole thing|the lot|it all|exam|trip|event)\b", re.I)
_STOP = {"the", "and", "for", "with", "that", "this", "just", "have", "done", "finish", "finished",
         "completed", "complete", "mark", "task", "today", "did", "was", "its", "all", "cancel", "drop",
         "goal", "delete", "remove", "quit", "scrap", "abandon", "forget", "about", "give", "up", "now",
         "please", "my", "i", "ive", "i've", "it"}
TTL_MIN = 30


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tokens(text):
    return {w[:6] for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def _score(message, title):
    a, b = _tokens(message), _tokens(title)
    return len(a & b) if a and b else 0


def _pending(conn, user_id, today, goal_id=None):
    sql = ("SELECT a.id, a.title, a.date, a.goal_id, g.display_title FROM actions a JOIN goals g ON g.id=a.goal_id "
           "WHERE a.user_id=? AND a.status='pending' AND g.status='active' AND a.date<=?")
    args = [user_id, today]
    if goal_id:
        sql += " AND a.goal_id=?"
        args.append(goal_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY a.date, a.start_time", args).fetchall()]


def _goals(conn, user_id):
    return [dict(r) for r in conn.execute(
        "SELECT id, title, display_title FROM goals WHERE user_id=? AND status='active'", (user_id,)).fetchall()]


def _store(conn, user_id, kind, goal_id, ids, label, note=""):
    """ids: list of row ids, or a dict payload for model-proposed actions."""
    conn.execute("UPDATE pending_actions SET status='expired' WHERE user_id=? AND status='open'", (user_id,))
    exp = (_now() + timedelta(minutes=TTL_MIN)).isoformat()
    cur = conn.execute(
        "INSERT INTO pending_actions (user_id, kind, goal_id, ids, label, note, expires_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, kind, goal_id, json.dumps(ids), label, note, exp))
    conn.commit()
    return {"id": cur.lastrowid, "kind": kind, "label": label}


def detect(conn, user_id, message, today, goal_id=None):
    text = (message or "").strip()
    if not text or "?" in text or len(text.split()) > 24:
        return None
    done = bool(_DONE.search(text)) and not _NEG.search(text)
    cancel = bool(_CANCEL.search(text)) and not re.search(r"\b(don't|do not|not|never)\b", text, re.I)
    if _SLIDE.search(text):
        ids = [r["id"] for r in _pending(conn, user_id, "9999-12-31", None)]
        return _store(conn, user_id, "slide_all", None, ids, "Push every pending task one day later?") if ids else None
    if cancel:
        goals = _goals(conn, user_id)
        pick = None
        if goal_id:
            pick = next((g for g in goals if g["id"] == goal_id), None)
            other = [g for g in goals if g["id"] != goal_id and _score(text, g["title"] + " " + g["display_title"]) >= 1]
            if other and _score(text, pick["title"] + " " + pick["display_title"] if pick else "") == 0 \
                    and not re.search(r"\b(this|it|goal)\b", text, re.I):
                pick = other[0]
        else:
            ranked = sorted(goals, key=lambda g: -_score(text, g["title"] + " " + g["display_title"]))
            if ranked and _score(text, ranked[0]["title"] + " " + ranked[0]["display_title"]) >= 1:
                pick = ranked[0]
        if pick and (_GOALISH.search(text) or goal_id or _score(text, pick["title"]) >= 1):
            reason = re.sub(r"(?i)^.*?\b(because|since|as)\b", "", text).strip() if re.search(r"(?i)\b(because|since)\b", text) else "cancelled by you in chat"
            return _store(conn, user_id, "cancel_goal", pick["id"], [], f"Cancel \"{pick['display_title']}\"? Pending tasks go; what you already did stays on record.", reason[:200])
        tasks = _pending(conn, user_id, "9999-12-31", goal_id)
        best = max(tasks, key=lambda t: _score(text, t["title"]), default=None)
        if best and _score(text, best["title"]) >= 1:
            return _store(conn, user_id, "delete_task", best["goal_id"], [best["id"]], f"Delete task \"{best['title']}\"?")
        return None
    if not done:
        return None
    tasks = _pending(conn, user_id, today, goal_id)
    if _ALL.search(text) and re.search(r"\btoday|tasks?\b", text, re.I) and tasks:
        ids = [t["id"] for t in tasks if t["date"] == today] or [t["id"] for t in tasks]
        return _store(conn, user_id, "complete_all_today", goal_id, ids, f"Mark all {len(ids)} of today's pending tasks done?")
    scored = sorted(((_score(text, t["title"] + " " + t["display_title"]), t) for t in tasks), key=lambda x: -x[0])
    if scored and scored[0][0] >= 1:
        top = scored[0][0]
        chosen = [t for s, t in scored if s == top][:3]
        if len(chosen) == 1 or len({t["goal_id"] for t in chosen}) == 1:
            ids = [t["id"] for t in chosen]
            names = "; ".join(t["title"] for t in chosen)
            return _store(conn, user_id, "complete_task", chosen[0]["goal_id"], ids, f"Mark done: {names}?")
    if goal_id and _GOALISH.search(text) or (goal_id and _ALL.search(text)):
        g = conn.execute("SELECT display_title FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)).fetchone()
        if g:
            return _store(conn, user_id, "complete_goal", goal_id, [], f"Close \"{g['display_title']}\" as achieved? Every remaining task gets marked done.")
    if goal_id and tasks:
        t = tasks[0]
        return _store(conn, user_id, "complete_task", goal_id, [t["id"]], f"Mark done: {t['title']}?")
    return None


def resolve(conn, user_id, proposal_id, accept):
    row = conn.execute("SELECT * FROM pending_actions WHERE id=? AND user_id=?", (proposal_id, user_id)).fetchone()
    if not row:
        return {"ok": False, "detail": "proposal not found"}
    if row["status"] != "open":
        return {"ok": False, "detail": "already handled"}
    if row["expires_at"] < _now().isoformat():
        conn.execute("UPDATE pending_actions SET status='expired' WHERE id=?", (proposal_id,))
        conn.commit()
        return {"ok": False, "detail": "That proposal expired. Say it again."}
    if not accept:
        conn.execute("UPDATE pending_actions SET status='declined' WHERE id=?", (proposal_id,))
        conn.commit()
        return {"ok": True, "applied": False, "message": "Fine, nothing touched."}
    ids = json.loads(row["ids"] or "[]")
    kind, gid = row["kind"], row["goal_id"]
    if isinstance(ids, dict):
        from core import actions
        user = dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
        out = actions.execute(conn, user, row, ids)
        if out is None:
            return {"ok": False, "detail": "unknown proposal"}
        conn.execute("UPDATE pending_actions SET status='applied' WHERE id=?", (proposal_id,))
        conn.commit()
        return {"ok": True, "applied": True, "kind": kind, "goal_id": out.get("goal_id", gid), "message": out["message"]}
    marks = ",".join("?" * len(ids)) or "NULL"
    if kind in ("complete_task", "complete_all_today"):
        conn.execute(f"UPDATE actions SET status='done' WHERE user_id=? AND id IN ({marks})", [user_id, *ids])
        msg = f"Marked {len(ids)} done."
       
        from core.routes.goal_routes import close_if_finished
        for r in conn.execute(f"SELECT DISTINCT goal_id FROM actions WHERE user_id=? AND id IN ({marks})", [user_id, *ids]).fetchall():
            if close_if_finished(conn, r["goal_id"]):
                msg += " That was the last task, so the goal is closed as achieved."
    elif kind == "delete_task":
        conn.execute(f"DELETE FROM actions WHERE user_id=? AND id IN ({marks})", [user_id, *ids])
        msg = "Task deleted."
    elif kind == "slide_all":
        conn.execute(
            f"UPDATE actions SET date=date(date,'+1 day') WHERE user_id=? AND status='pending' AND id IN ({marks}) "
            "AND date < (SELECT deadline FROM goals WHERE id=actions.goal_id)", [user_id, *ids])
        msg = "Everything pending slid a day."
    elif kind == "complete_goal":
        conn.execute("UPDATE goals SET status='succeeded', manually_succeeded=1 WHERE id=? AND user_id=?", (gid, user_id))
        conn.execute("UPDATE actions SET status='done' WHERE goal_id=? AND status='pending'", (gid,))
        msg = "Goal closed as achieved."
    elif kind == "cancel_goal":
        conn.execute("UPDATE goals SET status='cancelled', plan_status=? WHERE id=? AND user_id=?",
                     (f"cancelled: {row['note'] or 'by you'}", gid, user_id))
        conn.execute("DELETE FROM actions WHERE goal_id=? AND status!='done'", (gid,))
        msg = "Goal cancelled. Done work stays on the record."
    else:
        return {"ok": False, "detail": "unknown proposal"}
    conn.execute("UPDATE pending_actions SET status='applied' WHERE id=?", (proposal_id,))
    conn.commit()
    return {"ok": True, "applied": True, "kind": kind, "goal_id": gid, "message": msg}


_NAV = re.compile(r"^(?:please |hey |eloise,? )*(?:show|open|go to|take me to|switch to|pull up|bring up|let me see)\s+(?:me\s+)?(?:my |the |our )?(.+?)\s*(?:page|screen|tab|view)?[.!]?$", re.I)
_SCREENS = (
    (("new goal", "file a goal", "add a goal", "create a goal"), "new-goal", "Fine. Tell me what you're chasing."),
    (("all goals", "goals"), "goals", "Everything you've committed to."),
    (("schedule", "calendar", "timetable", "plan", "week"), "schedule", "Here's the plan, all of it."),
    (("board", "pressure", "score", "scoreboard"), "board", "The pressure board. Try not to flinch."),
    (("today", "my day", "day"), "today", "Today. This is what's on you."),
    (("settings", "setting", "api key", "provider", "model", "email", "mail"), "settings", "Settings. Don't break anything."),
)


_LOOSE = re.compile(r"\b(show|see|open|view|check|look|where|what|whats|what's|how|take|go|take me|switch|want|need|let me|can i|bring)\b", re.I)
_LOOSE_SCREENS = (
    (r"\b(what'?s|whats)\s+(on|up)\b|\bfor today\b|\bmy day\b|\btoday\b", "today", "Today. This is what's on you."),
    (r"\bhow am i doing\b|\bhow('?s| is) it going\b|\bscore\b|\bpressure\b|\bstreak\b|\bbehind\b|\bprogress\b", "board", "The pressure board. Try not to flinch."),
    (r"\b(schedule|calendar|timetable|week|plan)\b", "schedule", "Here's the plan, all of it."),
    (r"\b(settings?|api key|provider|account|notifications?)\b", "settings", "Settings. Don't break anything."),
    (r"\b(add|new|start|create|set up|begin)\b.*\bgoal\b|\bnew goal\b", "new-goal", "Fine. Tell me what you're chasing."),
    (r"\b(my |all |every )?goals\b", "goals", "Everything you've committed to."),
)


def _loose_screen(message):
    low = (message or "").lower().strip()
    if len(low.split()) > 9 or not _LOOSE.search(low):
        return None
    if re.search(r"\b(move|change|delete|remove|cancel|reschedule|push|slide|finish|complete|mark)\b", low):
        return None
    for pat, screen, line in _LOOSE_SCREENS:
        if re.search(pat, low):
            return {"reply": line, "ui": {"screen": screen}}
    return None


def navigate(conn, user_id, message):
    m = _NAV.match((message or "").strip())
    if not m or len(message.split()) > 8:
        return _loose_screen(message)
    target = m.group(1).strip().lower()
    for names, screen, line in _SCREENS:
        if target in names:
            return {"reply": line, "ui": {"screen": screen}}
    goals = _goals(conn, user_id)
    best = max(goals, key=lambda g: _score(target, g["title"] + " " + g["display_title"]), default=None)
    if best and _score(target, best["title"] + " " + best["display_title"]) >= 1:
        return {"reply": f"Opening {best['display_title']}.", "ui": {"screen": "goal", "id": best["id"]}}
    return _loose_screen(message)