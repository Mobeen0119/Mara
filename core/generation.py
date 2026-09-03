import json
import re
from datetime import date, datetime, timedelta

from core.llm import LLMManager
from core.persona import (
    BASE_SYSTEM_PROMPT,
    CHECK_IN_NO_SYSTEM_PROMPT,
    CHECK_IN_SYSTEM_PROMPT,
    CHECK_IN_YES_SYSTEM_PROMPT,
    FALLBACK_CHECK_IN_NO,
    FALLBACK_CHECK_IN_PROMPT,
    FALLBACK_CHECK_IN_YES,
    GLOBAL_OPENER,
    LINKS_FALLBACK,
    MAIN_FALLBACKS,
    NO_INSULTS,
    NUDGE_FALLBACK,
    OPENING_FALLBACK,
    PLAN_FALLBACK,
    fallback_greeting,
)

_manager = None


def get_manager(db=None):
    global _manager
    if _manager is None:
        _manager = LLMManager(db=db)
    return _manager


def _pick(items):
    return items[(datetime.now().microsecond // 100) % len(items)]


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def days_remaining(deadline, today=None):
    today = today or date.today().isoformat()
    return days_between(today, deadline)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
def generate_global_chat_reply(user_name, goals_summary, history, message, db=None):
    sys = BASE_SYSTEM_PROMPT
    summary = "; ".join(goals_summary) if goals_summary else "no goals on the board yet"
    ctx = f"\n\nUser: {user_name}\nCurrent board: {summary}\n"
    user_prompt = ctx + f"Recent conversation:\n{history}\n\nUser just said: {message}\nReply:"
    text, source = get_manager(db).generate_with_fallback(
        sys, "Answer the user's latest message as Eloise." + user_prompt,
        fallback_fn=lambda: _pick(MAIN_FALLBACKS),
    )
    return text, source


def generate_chat_reply(user_name, goal_name, history, message, db=None):
    sys = BASE_SYSTEM_PROMPT
    user_prompt = (
        f"User: {user_name}\nGoal: {goal_name}\nRecent conversation:\n{history}\n\n"
        f"User just said: {message}\nReply: <reply>"
    )
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt, fallback_fn=lambda: _pick(MAIN_FALLBACKS)
    )
    return text, source


def generate_opening_message(user_name, goal_summary, db=None):
    sys = BASE_SYSTEM_PROMPT
    user_prompt = f"User: {user_name}\nGoals: {goal_summary}\nSay a one-line opening."
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt, fallback_fn=lambda: OPENING_FALLBACK
    )
    return text, source


def generate_link_suggestion(user_name, goal_name, message, db=None):
    sys = BASE_SYSTEM_PROMPT
    user_prompt = f"User: {user_name}\nGoal: {goal_name}\nThey asked: {message}\nSuggest 3 concrete resources."
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt, fallback_fn=lambda: LINKS_FALLBACK
    )
    return text, source


def generate_nudge(user_name, goal_summary, nearest, db=None):
    sys = BASE_SYSTEM_PROMPT
    user_prompt = f"User: {user_name}\nGoals: {goal_summary}\nNearest task: {nearest}\nNudge once."
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt, fallback_fn=lambda: NUDGE_FALLBACK
    )
    return text, source


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------
def generate_check_in_prompt(user_name, goals_summary, db=None):
    sys = CHECK_IN_SYSTEM_PROMPT
    user_prompt = f"User: {user_name}\nActive goals: {'; '.join(goals_summary) or 'none'}\nAsk the check-in question."
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt, fallback_fn=lambda: _pick(FALLBACK_CHECK_IN_PROMPT)
    )
    return text, source


def generate_check_in_response(user_name, done, db=None):
    if done:
        sys = CHECK_IN_YES_SYSTEM_PROMPT
        text, source = get_manager(db).generate_with_fallback(
            sys, f"User: {user_name} says they finished. React and set the next move.",
            fallback_fn=lambda: FALLBACK_CHECK_IN_YES,
        )
    else:
        sys = CHECK_IN_NO_SYSTEM_PROMPT
        text, source = get_manager(db).generate_with_fallback(
            sys, f"User: {user_name} says they did NOT finish. Insult lightly, then redirect.",
            fallback_fn=lambda: _pick(NO_INSULTS) + " I've redrawn the board so the gap is undeniable.",
        )
    return text, source


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_time(t):
    t = (t or "").strip()
    m = TIME_RE.match(t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def parse_time_window(text):
    """Parse a window like '11pm-7am', '3pm-5pm', '5-7pm', '9:00-12:00' or 'gym 5-7pm'.
    Returns (start_min, end_min) in minutes from midnight, handling overnight."""
    text = text or ""
    low = text.lower()
    numbers = re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", low)
    # Determine the meridian context: the last explicit am/pm in the string
    # applies to any bare 12h numbers that precede it (e.g. "5-7pm" -> 5pm).
    tail_mer = ("pm" if re.search(r"pm(?!\w)", low) else "am") if re.search(r"(?:am|pm)(?!\w)", low) else None

    times = []
    for num, minute, mer in numbers:
        hh = int(num)
        mm = int(minute) if minute else 0
        if mer:
            times.append(_hh_12_to_24(hh, mer) * 60 + mm)
        elif minute:
            times.append(hh * 60 + mm)
        elif tail_mer:
            times.append(_hh_12_to_24(hh, tail_mer) * 60 + mm)
        elif times:
            times.append(hh * 60 + mm)

    if len(times) < 2:
        return None
    start, end = times[0], times[-1]
    if end < start:
        return (start, end + 24 * 60)
    return (start, end)


def _hh_12_to_24(hh, mer):
    if hh == 12:
        return 0 if mer == "am" else 12
    return hh + 12 if mer == "pm" else hh


def _blocked_at(blocked, cur):
    """cur is a minute-of-day (may be >=1440 from the start offset). Decide if a
    raw time-of-day is inside any blocked window, including overnight wrap."""
    tod = cur % (24 * 60)
    for s, e in blocked:
        if e >= 24 * 60:
            # overnight wrap: blocked [s, 1440) and [0, e-1440)
            if tod >= s or tod < (e - 24 * 60):
                return True
        else:
            if s <= tod < e:
                return True
    return False


def _build_fallback_plan(goal, today=None):
    today = today or date.today().isoformat()
    deadline = goal["deadline"]
    days = max(days_remaining(deadline, today), 1)
    reminder = normalize_time(goal.get("reminder_time") or "09:00") or "09:00"
    reminder_min = int(reminder.split(":")[0]) * 60 + int(reminder.split(":")[1])
    start = 60 + reminder_min if reminder_min < 21 * 60 else (reminder_min + 60) % (24 * 60)
    start = min(start, 21 * 60)
    constraints = []
    try:
        constraints = json.loads(goal.get("constraints") or "[]")
    except Exception:
        constraints = []
    blocked = []
    for c in constraints:
        w = parse_time_window(c)
        if w:
            blocked.append(w)
    entries = []
    for i in range(min(days, 15)):
        day = (date.fromisoformat(today) + timedelta(days=i)).isoformat()
        slots = []
        cur = start
        count = 0
        while count < 2 and cur < 22 * 60:
            if not _blocked_at(blocked, cur):
                slots.append((cur, min(cur + 60, 22 * 60)))
                count += 1
            cur += 60
        for s, e in slots:
            entries.append({
                "date": day,
                "title": goal["display_title"],
                "start_time": f"{s // 60:02d}:{s % 60:02d}",
                "end_time": f"{e // 60:02d}:{e % 60:02d}",
                "status": "pending",
            })
    return entries


def generate_plan(goal, user, db=None):
    """Generate a real (or fallback) schedule for a goal. Returns list of action dicts."""
    entries = _build_fallback_plan(goal)
    # Try the LLM for a richer plan; on any failure fall back to the deterministic one.
    try:
        sys = BASE_SYSTEM_PROMPT
        deadline = goal["deadline"]
        title = goal["display_title"]
        cons = goal.get("constraints") or "[]"
        user_prompt = (
            f"Build a day-by-day plan from {date.today().isoformat()} to {deadline} for: {title}\n"
            f"Constraints: {cons}\n"
            "Return ONLY a JSON array of objects: "
            '{"date":"YYYY-MM-DD","title":"short task","start_time":"HH:MM","end_time":"HH:MM"}. '
            "Keep grouped tasks concrete and non-overlapping with constraints. Detect overrun by pushing, never past deadline."
        )
        res = get_manager(db).generate(sys, user_prompt, timeout=45)
        if res.ok:
            parsed = _parse_plan_json(res.text, deadline)
            if parsed:
                return parsed
    except Exception:
        pass
    return entries


def _parse_plan_json(text, deadline):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    entries = []
    seen = set()
    for item in data:
        d = item.get("date")
        t = item.get("title")
        if not d or not t:
            continue
        if d > deadline:
            d = deadline
        key = (d, t, item.get("start_time", ""))
        if key in seen:
            continue
        seen.add(key)
        entries.append({
            "date": d,
            "title": t.strip()[:120],
            "start_time": normalize_time(item.get("start_time")) or "",
            "end_time": normalize_time(item.get("end_time")) or "",
            "status": "pending",
        })
    return entries if entries else None


# ---------------------------------------------------------------------------
# Goal completion detection
# ---------------------------------------------------------------------------
def completion_detected(message):
    """Detect a genuine 'I finished the task' claim, guarding against questions
    and vague statements like 'what do I have to do today' or 'I'm done with...'."""
    text = (message or "").strip()
    if not text or len(text.split()) > 16:
        return False
    lowered = " " + re.sub(r"[^a-z0-9\s]", " ", text.lower()) + " "
    if "?" in text:
        return False
    # questions / ambiguous frames must never auto-close
    ambiguous = ["what", "how", "when", "do i", "am i", "should i", "whats", "what's",
                 "have to do", "need to do", "should do", "to do today", "schedule",
                 "today", "next", "now what", "anything"]
    if any((" " + a + " ") in lowered for a in ambiguous):
        return False
    success = [
        " all done ", " i'm done ", " i am done ", " i m done ", " im done ",
        " all finished ", " it's done ", " its done ", " it is done ", " it's finished ",
        " done with it ", " finished it ", " completed it ", " i completed ",
        " i finished ", " wrapped up ", " closed it out ", " got it done ",
        " finished ", " completed ",
    ]
    return any(p in lowered for p in success)


# ---------------------------------------------------------------------------
# Progress / stats
# ---------------------------------------------------------------------------
def progress_stats(goal, actions):
    total_planned = sum((a.get("duration_min") or 60) for a in actions) / 60.0
    completed = sum((a.get("duration_min") or 60) for a in actions if a.get("status") == "done") / 60.0
    missed = sum((a.get("duration_min") or 60) for a in actions if a.get("status") == "missed") / 60.0
    total_days = 1
    today = date.today()
    ddiff = days_remaining(goal.get("deadline", today.isoformat()))
    status = "safe"
    if ddiff < 0:
        status = "overdue"
    elif total_planned > 0 and completed < 0.2 * total_planned and ddiff <= 3:
        status = "watch"
    elif total_planned > 0 and completed / max(total_planned, 1) < 0.5 and ddiff <= 3:
        status = "at_risk"
    narrative = f"Completed {completed:.1f}h of a planned {total_planned:.1f}h."
    if missed:
        narrative += f" {missed:.1f}h slipped as missed."
    deadline = goal.get("deadline")
    narrative += f" {ddiff} day{'s' if abs(ddiff) != 1 else ''} left."
    return {
        "total_planned_hours": round(total_planned, 1),
        "completed_hours": round(completed, 1),
        "missed_hours": round(missed, 1),
        "available_hours": round(total_planned - completed, 1),
        "status": status,
        "narrative": narrative,
    }