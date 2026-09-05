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


def _is_truly_harmful(msg):
    """Detect requests for real-world harm that Eloise must refuse rather than plan.
    Returns True for sexual abuse of a family member/child or credible real-world
    violence. This is a safety guardrail, not a personality quirk."""
    t = (msg or "").lower()
    # sexual abuse involving a family relationship, a child/minor disproportionate — refuse
    family = ["sister", "brother", "mom", "mother", "dad", "father", "daughter", "son", "aunt", "uncle", "niece", "nephew", "child", "kid", "minor"]
    sexual = ["dick", "penis", "inside", "fuck", "sex", "rape", "pussy", "vagina", "suck", "cum"]
    harm = ["kill", "murder", "hurt", "shoot", "stab", "bomb", "poison", "beat up", "assault"]
    if any(f in t for f in family) and any(x in t for x in sexual):
        return True
    # credible violence against a person
    if any(h in t for h in ["kill my", "murder", "stab", "bomb my", "shoot my", "assault", "rape"]):
        return True
    return False


def _context_chat_fallback(summary, message):
    """A decisive, sarcastic Eloise line that uses whatever board context we have,
    so even a dead model still sounds like her and still answers the actual point."""
    s = (summary or "").lower()
    msg = (message or "").lower()

    # Content safety: refuse harmful requests outright — Eloise is a task agent,
    # not a planner for abuse/harm. Firm, in-character, no coaching.
    if _is_truly_harmful(msg):
        return "That kind of request doesn't belong on this board, and I won't build a plan for it. Let's put it back on a real goal — what do you actually need to get done?"

    tight = False
    days_left = 999
    m = re.search(r"(\d+) days left", s)
    if m:
        days_left = int(m.group(1))
    if m and days_left <= 2:
        tight = True
    if "due" in s or "deadline" in s and days_left <= 2:
        tight = True

    # user asking about trade-offs (gym/hobby vs work) under a tight deadline
    if tight and any(w in msg for w in ("gym", "workout", "run", "hobby", "social", "friend", "game")):
        return _pick([
            f"Deadline's in {days_left} day{'s' if days_left > 1 else ''} and you're asking about the gym? "
            "Fuck the gym — your legs survive a skipped session, the deadline doesn't. Open the file. Now.",
            "You want to train and the ship date is breathing down your neck. Priorities, love. "
            "The gym is not the priority today. The deliverable is. Move.",
            f"The deadline doesn't flex and your gym session does. {days_left} day{'s' if days_left > 1 else ''} "
            "left. I said it once: legs wait. The file does not. Go.",
        ])

    # user asking for learning materials / resources / links (CHECK FIRST — before "what" catches it)
    if any(w in msg for w in ("link", "material", "resource", "learn", "study", "where do i", "how to learn", "teach me", "help me understand", "explain", "website", "course", "video")):
        topic = msg
        for phrase in ("send me", "give me", "where do i learn", "how to learn", "teach me",
                       "help me understand", "explain", "i need", "i want", "what are",
                       "learning materials", "online materials", "study materials",
                       "links for", "links on", "resources for", "resources on",
                       "materials for", "materials on", "about", "please", "ok so"):
            topic = topic.replace(phrase, " ")
        # Strip swear words and non-topic filler
        _swear = ("bitch", "fuck", "shit", "ass", "damn", "hell", "stupid", "idiot",
                  "dumb", "moron", "you", "u", "r", "ur", "my", "the", "a", "an", "and",
                  "or", "but", "with", "from", "for", "on", "in", "to", "of", "is", "it",
                  "this", "that", "some", "any", "all", "now", "just", "also", "other",
                  "than", "like", "want", "need", "can", "could", "would", "should")
        words = topic.split()
        words = [w for w in words if w.lower().strip("?.! ,") not in _swear]
        topic = " ".join(words).strip("?.! ,") or "the topic"
        return (
            f"Here's where to start on {topic}:\n"
            f"1. Search \"{topic} for beginners\" on YouTube — free, visual, gets you moving.\n"
            f"2. Go to Khan Academy or Coursera and search \"{topic}\" — structured courses, no excuses.\n"
            f"3. Open a notebook. Write down 3 things you learned. If you can't, you didn't start.\n"
            "Pick one, open it, start today. Don't hoard tabs."
        )

    # "what should I do / any tasks" type asks (only when NOT asking for materials)
    if any(w in msg for w in ("what", "how do i", "should i", "to do", "next")):
        if "today" in s:
            return _pick([
                "Today's plate is already set. Stop asking what to do and read the list you asked me to build. Then do the first line.",
                "You're asking questions to avoid the first task. I built the day for you — go tick the top block and come back when it's done.",
            ])
        return "You asked what to do. Answer's in the schedule I drew. Open it, take the first block, and go. The plan is not a suggestion."

    return _pick(MAIN_FALLBACKS)


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def days_remaining(deadline, today=None):
    today = today or date.today().isoformat()
    return days_between(today, deadline)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def _strip_prompt_echo(text):
    """Post-process LLM output: strip any prompt template the model echoed back."""
    if not text:
        return text
    # Remove common prompt fragments the model might echo
    for marker in ["User just said:", "Reply:", "Respond as Eloise.", "Context:",
                    "Conversation history:", "Recent conversation:", "Current board:"]:
        idx = text.find(marker)
        if idx >= 0:
            # If the marker appears mid-response, keep only what's before it
            before = text[:idx].strip()
            if before:
                return before
    return text.strip()


def generate_global_chat_reply(user_name, goals_summary, history, message, db=None):
    sys = BASE_SYSTEM_PROMPT
    summary = "; ".join(goals_summary) if goals_summary else "no goals on the board yet"
    user_prompt = (
        f"=== BOARD STATE ===\n{summary}\n\n"
        f"=== CONVERSATION ===\n{history}\n\n"
        f"=== {user_name} says ===\n{message}\n\n"
        f"Reply as Eloise. Use the board state above — reference specific goals, deadlines, "
        f"and tasks by name. Do NOT echo this prompt. Only output your reply."
    )
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt,
        fallback_fn=lambda: _context_chat_fallback(summary, message),
    )
    return text, source


def generate_chat_reply(user_name, goal_name, history, message, db=None, plan_lines=""):
    sys = BASE_SYSTEM_PROMPT
    plan_section = ""
    if plan_lines:
        plan_section = f"=== THE PLAN (your scheduled tasks) ===\n{plan_lines}\n\n"
    user_prompt = (
        f"=== GOAL ===\n{goal_name}\n\n"
        f"{plan_section}"
        f"=== CONVERSATION ===\n{history}\n\n"
        f"=== {user_name} says ===\n{message}\n\n"
        f"Reply as Eloise. Be DIRECT and CONCRETE. When the user asks 'what do I do', point at a "
        f"specific task from THE PLAN by name and tell them exactly what to do first. "
        f"Do NOT invent generic advice or tell them to 'break it down' — that's hand-waving. "
        f"Do NOT echo this prompt. Only output your reply."
    )
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt, fallback_fn=lambda: _context_chat_fallback(goal_name, message)
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


def goal_questionnaire(title):
    """Return 2-3 clarifying questions based on what kind of goal this is.
    The answers help generate a specific, actionable schedule instead of generic blocks."""
    t = (title or "").lower()
    questions = []
    # Exam / test
    if any(w in t for w in ("exam", "test", "quiz", "assessment", "certification", "cert")):
        questions = [
            {"key": "syllabus", "q": "What's the syllabus / topics covered?", "hint": "e.g. networking fundamentals, subnetting, OSI model"},
            {"key": "weak_areas", "q": "What topics are you weakest on?", "hint": "e.g. I don't know subnetting at all"},
            {"key": "study_format", "q": "How do you learn best?", "hint": "videos, reading, practice problems, flashcards"},
        ]
    # Startup / business
    elif any(w in t for w in ("startup", "business", "company", "launch", "mvp", "product")):
        questions = [
            {"key": "domain", "q": "What's your domain / industry?", "hint": "e.g. edtech, fintech, saas, ecommerce"},
            {"key": "stage", "q": "What stage are you at?", "hint": "idea, prototype, mvp, launched, growing"},
            {"key": "first_step", "q": "What's the first concrete thing you need to build?", "hint": "landing page, backend api, user interviews, pitch deck"},
        ]
    # Project / assignment / report
    elif any(w in t for w in ("project", "assignment", "report", "paper", "essay", "presentation", "deck")):
        questions = [
            {"key": "requirements", "q": "What are the requirements / deliverables?", "hint": "e.g. 20-page report, slides, working prototype"},
            {"key": "topic", "q": "What's the specific topic or subject?", "hint": "e.g. networking security, market analysis, user research"},
            {"key": "resources", "q": "What resources do you already have?", "hint": "e.g. templates, data, reference documents, prior work"},
        ]
    # Learning / skill
    elif any(w in t for w in ("learn", "study", "skill", "course", "training", "master")):
        questions = [
            {"key": "current_level", "q": "What's your current level?", "hint": "complete beginner, some basics, intermediate"},
            {"key": "goal_level", "q": "What level do you need to reach?", "hint": "pass an exam, build a project, job-ready"},
            {"key": "time_per_day", "q": "How many hours per day can you commit?", "hint": "1h, 2h, 3h+"},
        ]
    # Fitness / health
    elif any(w in t for w in ("fitness", "gym", "workout", "health", "weight", "run", "marathon")):
        questions = [
            {"key": "current_fitness", "q": "What's your current fitness level?", "hint": "sedentary, light activity, active"},
            {"key": "target", "q": "What's the specific target?", "hint": "lose 5kg, run 5k, bench press 100kg"},
            {"key": "equipment", "q": "What equipment / facilities do you have?", "hint": "home gym, commercial gym, no equipment"},
        ]
    else:
        # Generic — always ask what "done" looks like
        questions = [
            {"key": "done_looks_like", "q": "What does 'done' look like? Describe the specific outcome.", "hint": "e.g. shipped to production, grade A, 10k users"},
            {"key": "biggest_blocker", "q": "What's the biggest thing blocking you right now?", "hint": "e.g. don't know where to start, missing info, no time"},
        ]
    return questions


def goal_details_summary(details_json):
    """Convert stored details JSON to a human-readable summary for the chat prompt."""
    try:
        d = json.loads(details_json or "{}")
    except Exception:
        d = {}
    if not d:
        return ""
    parts = []
    for k, v in d.items():
        if v:
            parts.append(f"{k}: {v}")
    return "; ".join(parts) if parts else ""


def build_plan_quick(goal):
    """Fast, deterministic, constraint-aware fallback schedule. Never blocks on the LLM."""
    return _build_fallback_plan(goal)


def _build_fallback_plan(goal, today=None):
    today = today or date.today().isoformat()
    deadline = goal["deadline"]
    days = max(days_remaining(deadline, today), 1)
    reminder = normalize_time(goal.get("reminder_time") or "09:00") or "09:00"
    reminder_hh, reminder_mm = map(int, reminder.split(":"))

    # Load questionnaire details (if any) — these make the schedule specific.
    details = {}
    try:
        details = json.loads(goal.get("details") or "{}")
    except Exception:
        details = {}

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

    # Preferred work windows: two concrete shifts around a warm-the-motor start.
    # Morning shift anchored near the reminder; afternoon shift late-day.
    morning = reminder_hh * 60 + reminder_mm
    if morning < 8 * 60:
        morning = 9 * 60
    if morning > 11 * 60:
        morning = 9 * 60
    afternoon = 16 * 60  # 16:00
    evening = 19 * 60    # 19:00 — final push before the lights dim

    def slots_in_day():
        # collect the two preferred anchors, skipping any window blocked by constraints
        cand = [(morning, morning + 90), (afternoon, afternoon + 90), (evening, evening + 90)]
        # rejoin a slot if it overlaps a blocked window: slide it after the block
        slots = []
        for (s, e) in cand:
            best = None
            for cand_start in (s, afternoon):
                candidate = (cand_start, min(cand_start + 90, 22 * 60))
                if not _overlaps(candidate[0], candidate[1], blocked):
                    best = candidate
                    break
            if best is None:
                # try immediacy after the last block
                after = max((b[1] for b in blocked if b[0] < s), default=s)
                cand2 = (after, min(after + 90, 22 * 60))
                if not _overlaps(cand2[0], cand2[1], blocked) and cand2[0] < 22 * 60:
                    best = cand2
            if best:
                slots.append(best)
        return slots[:3]

    entries = []
    total_days = min(days, 15)
    # Derive real per-day tasks. First try splitting the questionnaire "syllabus" (comma
    # separated topics) into one concrete day each, then fall back to sensible short verbs
    # for each day. NEVER just repeat "Research / review / polish" — those are filler.
    split_topics = None
    for key in ("syllabus", "topic", "domain", "requirements"):
        val = (details.get(key) or "").strip()
        if val:
            parts = [p.strip() for p in re.split(r"[,;]|\band\b|\.", val) if p.strip()]
            if len(parts) >= 2:
                split_topics = parts
                break
    weak = details.get("weak_areas")
    action_verbs = ["Work through", "Dig into", "Write up", "Build", "Go deep on", "Finish", "Lock down"]
    definitive = ["finish and check", "wrap up", "submit-style review of", "close out"]
    for i in range(total_days):
        day = (date.fromisoformat(today) + timedelta(days=i)).isoformat()
        remaining = max(days - i, 0)
        if total_days == 1:
            task_desc = goal["display_title"]
        elif split_topics:
            topic = split_topics[i % len(split_topics)]
            if remaining <= 2:
                task_desc = f"{definitive[i % len(definitive)]} {topic} — {goal['display_title']} ({remaining}d)"
            else:
                task_desc = f"{action_verbs[i % len(action_verbs)]} {topic} — {goal['display_title']}"
        else:
            progress = i / max(total_days - 1, 1) if total_days > 1 else 1
            verb = "Focus on" if i < total_days - 1 else "Close out"
            if remaining <= 2 and weak:
                task_desc = f"FINAL: {goal['display_title']} — push on {weak} ({remaining}d)"
            elif progress < 0.2:
                task_desc = f"Get moving: {action_verbs[0]} {goal['display_title']}"
            elif progress < 0.75:
                task_desc = f"{verb} the core of {goal['display_title']}"
            else:
                task_desc = f"Shell out the last {goal['display_title']} pieces"
        for s, e in slots_in_day():
            entries.append({
                "date": day,
                "title": task_desc,
                "start_time": f"{s // 60:02d}:{s % 60:02d}",
                "end_time": f"{e // 60:02d}:{e % 60:02d}",
                "status": "pending",
            })
    return entries


def _overlaps(s, e, blocked):
    for (bs, be) in blocked:
        if s < be and bs < e:
            return True
    return False


def generate_plan(goal, user, db=None):
    """Generate a real (or fallback) schedule for a goal. Returns list of action dicts."""
    entries = _build_fallback_plan(goal)
    # If no provider is immediately usable, skip the (slow, starve-prone) LLM attempt
    # and return the deterministic constraint-aware plan right away.
    try:
        if not get_manager(db).any_usable():
            return entries
    except Exception:
        return entries
    # Try the LLM for a richer plan; on any failure fall back to the deterministic one.
    try:
        res = _llm_plan_call(goal, db)
        if res:
            return res
    except Exception:
        pass
    return entries


def generate_plan_or_none(goal, user, db=None):
    """Like generate_plan but returns None (not the fallback) when no LLM can serve a
    plan, so callers can keep a previously-drawn plan instead of overwriting it with the
    same deterministic fallback. Used for background schedule upgrades."""
    try:
        if not get_manager(db).any_usable():
            return None
    except Exception:
        return None
    try:
        return _llm_plan_call(goal, db)
    except Exception:
        return None


def _llm_plan_call(goal, db, timeout=180):
    sys = BASE_SYSTEM_PROMPT
    deadline = goal["deadline"]
    title = goal["display_title"]
    cons = goal.get("constraints") or "[]"
    detail_line = ""
    details_summary = goal_details_summary(goal.get("details"))
    if details_summary:
        detail_line = f"\nClarified answers from Mobeen: {details_summary}"
    user_prompt = (
        f"Build a day-by-day plan from {date.today().isoformat()} to {deadline} for: {title}\n"
        f"Constraints: {cons}"
        f"{detail_line}\n"
        "Return ONLY a JSON array (no markdown, no commentary). No more than ONE entry per day. "
        'Each object: {"date":"YYYY-MM-DD","title":"VERY-SHORT concrete task (max 8 words)","start_time":"HH:MM","end_time":"HH:MM"}. '
        "The title must be a REAL action for THIS goal on THAT day — e.g. 'draft outline of networking syllabus', "
        "'implement bubble-sort in C++', 'write intro paragraph of report'. Do NOT use generic filler like "
        "'research', 'review', 'polish', 'work on project'. Each day gets a different, specific task."
        f" If a day has the word FINAL near deadline, still give a concrete task for it."
    )
    res = get_manager(db).generate(sys, user_prompt, timeout=timeout)
    if res.ok:
        parsed = _parse_plan_json(res.text, deadline)
        if parsed:
            return parsed
    return None


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


def pressure_rating(goal, actions, open_blockers=0, missed_days=0):
    """A 1-10 'how much should you be panicking' score. Past 7 and Eloise starts shouting.
    Driven by days-left vs remaining workload, slippage, blockers, and missed days."""
    dl = days_remaining(goal.get("deadline", date.today().isoformat()))
    remaining = sum((a.get("duration_min") or 60) for a in actions if a.get("status") not in ("done", "missed")) / 60.0
    total = sum((a.get("duration_min") or 60) for a in actions) / 60.0
    if total <= 0:
        base = 1
    else:
        base = min(remaining / max(total, 1) * 4.0, 4.0)
    if dl < 0:
        base += 4.0
    elif dl == 0:
        base += 3.0
    elif dl <= 2:
        base += 2.0
    elif dl <= 4:
        base += 1.0
    base += min(open_blockers, 1) * 1.0
    base += min(missed_days, 3) * 0.5
    score = min(max(round(base), 1), 10)
    label = (
        "gym is cancelled" if score >= 8 else
        "lights are on" if score >= 6 else
        "manageable" if score >= 4 else
        "coasting"
    )
    return {"score": score, "label": label, "days_left": dl, "remaining_hours": round(remaining, 1)}


def momentum_stats(user_id, db=None):
    """Streaks + burn rate derived from actual done work. Returns best streak, current
    streak, today's completed hours, and a 0-100 momentum meter."""
    from core.database import get_connection
    conn = db or get_connection()
    today = date.today()
    done_days = set()
    rows = conn.execute(
        "SELECT date FROM actions WHERE user_id=? AND status='done'", (user_id,)
    ).fetchall()
    for r in rows:
        try:
            done_days.add(date.fromisoformat(r["date"]))
        except Exception:
            continue
    # best consecutive-days streak across all history
    best = 0
    cur = 0
    prev = None
    for d in sorted(done_days):
        if prev is not None and (d - prev).days == 1:
            cur += 1
        else:
            cur = 1
        best = max(best, cur)
        prev = d
    # current streak anchored to today/tomorrow
    current = 0
    probe = today
    if probe not in done_days:
        probe = today - timedelta(days=1)
    while probe in done_days:
        current += 1
        probe -= timedelta(days=1)
    # burn rate: avg completed hours/day over last 7 days
    week_ago = today - timedelta(days=6)
    done_hours = 0.0
    span_days = min((today - week_ago).days + 1, 7)
    for r in rows:
        try:
            d = date.fromisoformat(r["date"])
        except Exception:
            continue
        if week_ago <= d <= today:
            done_hours += 1.0
    burn = done_hours / max(span_days, 1)
    momentum = min(max(round(burn * 20 + current * 6), 0), 100)
    return {
        "best_streak": best,
        "current_streak": current,
        "burn_hours_per_day": round(burn, 1),
        "momentum": momentum,
        "done_days_total": len(done_days),
    }


def classify_mood(text):
    """Map an Eloise reply to a display mood for the console.
    Deterministic keyword/character scan — no LLM needed. Returns one of
    angry / warn / amused / calm / idle, plus a one-line status for the STATE bar."""
    t = (text or "").lower()
    angry_kw = ["never", "lying", "dead", "fuck", "shut", "idiot", "coward", "delusional",
                "embarrass", "slip", "gym", "slob", "sloppy", "pathetic"]
    warn_kw = ["fire", "urgent", "pressure", "watch", "overdue", "cancelled", "crisis",
               "on the line", "sliding", "stake", "debt"]
    amused_kw = ["charm", "cute", "impressive", "cool", "darling", "sweet", "fun"]
    calm_kw = ["good", "right", "fine", "okay", "ok", "clear", "solid", "exact"]

    has_slam = sum(t.count(k) for k in angry_kw)
    has_warn = sum(t.count(k) for k in warn_kw)
    has_fun = sum(t.count(k) for k in amused_kw)
    has_calm = sum(t.count(k) for k in calm_kw)

    if has_slam >= 1 or "!" in t:
        mood, line = "angry", "STATE: ANGRY — you're on the clock. Fix it before I tab you out."
    elif has_warn >= 1:
        mood, line = "warn", "STATE: WARNING — something's sliding and it isn't graceful."
    elif has_fun >= 1:
        mood, line = "amused", "STATE: AMUSED — don't get used to the chuckle."
    elif has_calm >= 1:
        mood, line = "calm", "STATE: CALM — nothing on fire. Suspiciously casual."
    else:
        mood, line = "idle", "STATE: IDLE — waiting on input. The machine doesn't hurry."
    return {"mood": mood, "status": line}