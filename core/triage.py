import json
import re
from datetime import date

from core import generation

# Things whose failure is expensive/irreversible. 3 = exam-grade, 2 = serious.
_STAKES_3 = ("exam", "viva", "defense", "defence", "thesis", "interview", "visa", "tax",
             "submission", "submit", "board", "finals", "final exam", "court", "surgery")
_STAKES_2 = ("proposal", "pitch", "investor", "startup", "launch", "client", "contract",
             "application", "presentation", "assignment", "report", "demo", "rent", "payment")
_SOFT = ("gym", "workout", "work out", "walk", "jog", "run", "yoga", "hobby", "game", "gaming",
         "movie", "netflix", "tv", "social", "friends", "hangout", "party", "sport",
         "cricket", "football", "swim", "scroll", "nap")
_HARD = ("uni", "class", "lecture", "lab", "office", "work", "job", "shift", "commute", "sleep",
         "meal", "breakfast", "lunch", "dinner", "iftar", "sehri", "prayer", "namaz", "school",
         "kids", "family", "doctor", "therapy", "medic")


def importance_of(goal):
    try:
        v = int(json.loads(goal.get("details") or "{}").get("importance"))
        if 1 <= v <= 3:
            return v
    except Exception:
        pass
    return stakes_of(goal.get("title") or goal.get("display_title") or "")


def rate_importance(conn, goal):
    try:
        d = json.loads(goal.get("details") or "{}")
    except Exception:
        d = {}
    if d.get("importance") in (1, 2, 3):
        return d["importance"]
    sys = ("You rate how much a goal matters to a person's life, as a sensible friend would. "
           "3 = high stakes: exams, a career or business launch, money or legal deadlines, health emergencies. "
           "2 = serious but recoverable: work deliverables, important projects, commitments to others. "
           "1 = personal or optional: leisure, romance and gifts, hobbies, self-improvement with no hard consequence. "
           'Reply with ONLY JSON: {"importance": 1|2|3}.')
    try:
        r = generation.get_manager(conn).generate(sys, f"Goal: {goal.get('title') or goal.get('display_title')}", timeout=25, max_tokens=40)
        m = re.search(r"[123]", r.text) if r is not None and getattr(r, "ok", False) and r.text else None
    except Exception:
        m = None
    if not m:
        return importance_of(goal)
    v = int(m.group(0))
    row = conn.execute("SELECT details FROM goals WHERE id=?", (goal["id"],)).fetchone()
    try:
        d = json.loads((row["details"] if row else "") or "{}")
    except Exception:
        d = {}
    d["importance"] = v
    conn.execute("UPDATE goals SET details=? WHERE id=?", (json.dumps(d), goal["id"]))
    conn.commit()
    return v


def stakes_of(title):
    t = (title or "").lower()
    if any(w in t for w in _STAKES_3):
        return 3
    if any(w in t for w in _STAKES_2):
        return 2
    return 1


def window_kind(label):
    """'soft' windows can give way to a fire; anything unknown is treated as hard."""
    t = (label or "").lower()
    if any(re.search(rf"\b{re.escape(w)}", t) for w in _HARD):
        return "hard"
    if any(re.search(rf"\b{re.escape(w)}", t) for w in _SOFT):
        return "soft"
    return "hard"


def _windows(conn, user):
    out, seen = [], set()
    labels = []
    try:
        labels += list(json.loads(user.get("blocked_windows") or "[]"))
    except Exception:
        pass
    for g in conn.execute("SELECT constraints FROM goals WHERE user_id=? AND status='active'",
                          (user["id"],)).fetchall():
        try:
            labels += list(json.loads(g["constraints"] or "[]"))
        except Exception:
            pass
    for lab in labels:
        w = generation.parse_time_window(lab)
        if not w or tuple(w) in seen:               # "gym 5-7pm" and "gym 5 to 7 pm" are one window
            continue
        seen.add(tuple(w))
        out.append({"label": lab.strip(), "start": w[0], "end": w[1],
                    "hours": round(((w[1] - w[0]) % 1440) / 60, 1), "kind": window_kind(lab)})
    return out


def triage(conn, user, today):
    today_d = date.fromisoformat(today)
    ranked = []
    for g in conn.execute("SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall():
        g = dict(g)
        acts = [dict(a) for a in conn.execute("SELECT * FROM actions WHERE goal_id=?", (g["id"],)).fetchall()]
        missed = sum(1 for a in acts if a["status"] == "missed")
        pr = generation.pressure_rating(g, acts, missed_days=missed, today=today_d)
        left = pr["days_left"]
        st = importance_of(g)
        urgency = min(6.0 / max(left, 1), 3.0)
        score = round(pr["score"] + 1.5 * (st - 1) + urgency, 1)
        why = []
        if left <= 7:
            why.append(f"{left}d left")
        if pr["remaining_hours"]:
            why.append(f"{pr['remaining_hours']}h still open")
        if missed:
            why.append(f"{missed} missed")
        if st >= 2:
            why.append("high stakes" if st == 3 else "real stakes")
        ranked.append({"goal_id": g["id"], "title": g["display_title"], "days_left": left,
                       "pressure": pr["score"], "remaining_hours": pr["remaining_hours"],
                       "stakes": st, "score": score, "why": ", ".join(why) or "no rush"})
    ranked.sort(key=lambda r: -r["score"])

    soft = [w for w in _windows(conn, user) if w["kind"] == "soft"]
    cut, call = [], ""
    if ranked:
        top = ranked[0]
        fire = top["pressure"] >= 6 or (top["days_left"] <= 7 and top["stakes"] >= 2) or top["score"] >= 9
        if fire:
            cut = [{"window": w["label"], "hours": w["hours"]} for w in soft]
            name = top["title"]
            if cut:
                lost = ", ".join(c["window"] for c in cut)
                hrs = round(sum(c["hours"] for c in cut), 1)
                call = (f"'{name}' wins: {top['why']}, pressure {top['pressure']}/10. "
                        f"{lost} can wait today: give those {hrs}h to it.")
            else:
                call = (f"'{name}' wins: {top['why']}, pressure {top['pressure']}/10. "
                        f"Nothing soft left to cut - protect its blocks and defer everything else.")
            others = [r["title"] for r in ranked[1:] if r["score"] < top["score"] - 2]
            if others:
                call += f" Can wait: {', '.join(others[:3])}."
        elif len(ranked) > 1:
            call = f"No fire. '{ranked[0]['title']}' is first in line; keep the rest of your routine."
        else:
            call = ""
    return {"ranked": ranked, "call": call, "cut": cut, "fire": bool(cut) or bool(call and "wins" in call)}


def summary_line(conn, user, today):
    try:
        t = triage(conn, user, today)
    except Exception:
        return ""
    if not t["ranked"]:
        return ""
    order = " > ".join(f"{r['title']} ({r['score']})" for r in t["ranked"][:4])
    return f"priority order: {order}" + (f". PRIORITY CALL: {t['call']}" if t["call"] else "")