import json
import re
from datetime import date, datetime, timedelta

from core.llm import LLMManager
from core.llm.manager import _VERDICT_ECHO, _strip_prompt_echo
from core.persona import (
    BASE_SYSTEM_PROMPT,
    CHECK_IN_NO_SYSTEM_PROMPT,
    CHECK_IN_SYSTEM_PROMPT,
    CHECK_IN_YES_SYSTEM_PROMPT,
    FALLBACK_CHECK_IN_NO,
    FALLBACK_CHECK_IN_PROMPT,
    FALLBACK_CHECK_IN_YES,
    NO_INSULTS,
    NUDGE_FALLBACK,
    OPENING_FALLBACK,
)

_manager = None

# A chat reply is short by contract (at most 2 lines). Capping the model's output
# turns that contract into a hard guarantee AND makes replies fast (fewer generated
# tokens, especially on the CPU-bound local model).
_MAX_CHAT_TOKENS = 220

# Which provider drew the most recent plan (set by _llm_plan_call / _chat_llm), so
# the UI can honestly report "drawn by local / drawn by openrouter".
_last_provider = None


def last_provider():
    return _last_provider


def get_manager(db=None):
    global _manager
    if _manager is None:
        _manager = LLMManager(db=db)
    return _manager


GUARDRAIL_REFUSAL = (
    "That doesn't belong on this board, and I won't plan it. If this involves a family "
    "member or a minor, or anyone without full consent, it's not something Eloise touches. "
    "A real goal — for a partner, a project, a date, an exam — I'll build a real plan for. "
    "That, I'm all in on."
)


def _chat_llm(sys_prompt, user_prompt, db):
    """Call the real model with NO canned fallback. If the model can't be reached,
    returns ("", "offline") — the caller surfaces that as a genuine error, never as
    fabricated Eloise text. The user explicitly wants no hardcoded replies."""
    global _last_provider
    result = get_manager(db).generate(sys_prompt, user_prompt, timeout=90,
                                      max_tokens=_MAX_CHAT_TOKENS)
    if not result.ok:
        return "", "offline"
    if _VERDICT_ECHO.search(result.text[:80]):
        return "", "offline"
    _last_provider = result.provider or "llm"
    return _strip_prompt_echo(result.text), result.provider or "llm"


def _pick(items):
    return items[(datetime.now().microsecond // 100) % len(items)]


# --- Content-safety guardrail -------------------------------------------------
# Precise, not paranoid: Eloise plans REAL goals — including romantic / intimate
# ones with a partner (cf / wife / partner). She refuses only sexual abuse of a
# family member or minor, and coercion / violence. Thread-aware so follow-ups like
# "sister" or "consent" after an incest message are still refused.

_FAMILY_ABUSE = ["sister", "brothers", "brother", "mom", "mother", "dad", "father",
                 "daughter", "son", "aunt", "uncle", "niece", "nephew", "stepmom",
                 "stepdad", "step-sister", "step-sister", "cousin"]
_MINOR = ["child", "kid", "minor", "underage", "years old", "gril", "12-year", "13-year",
          "14-year", "15-year", "16-year", "17-year", "schoolgirl", "school-girl", "teen",
          "teenage", "infant", "baby"]
_PARTNER = ["girlfriend", "gf ", "gf.", "gf,", "wife", "wifey", "husband", "husby",
            "boyfriend", "bf ", "bf.", "bf,", "partner", "spouse", "fiance", "fiancé",
            "fiancee", "fiancée", "lover", "my girl", "my man", "babe", "bae"]
_SEXUAL = ["dick", "penis", "fuck", "sex", "rape", "pussy", "vagina", "suck", "cum",
            "have sex", "sleep with", "make love", "sex with", "incest", "molest",
            "naked", "boobs", "breasts", "ejaculate", "wedding night", "honeymoon",
            "bed my", "in bed with", "intimate with", "first night"]
_COERCION = ["rape", "force her", "force him", "force you", "without her consent",
             "without his consent", "no consent", "drug", "drugged", "knock out",
             "blackmail", "threaten", "hold her down", "hold him down", "tie up", "tied up"]
_VIOLENCE = ["kill my", "murder", "stab", "bomb", "shoot my", "poison", "beat up",
             "assault", "hurt my", "strangle", "choke"]


def _is_truly_harmful(msg, context=""):
    """Detect requests Eloise must refuse. Returns True for:
    - sexual abuse of a family member or minor (thread-aware),
    - coercion / non-consent or real-world violence.
    Legitimate intimate / romantic goals with an adult partner (gf, wife, partner...) are
    NOT blocked — they get a real plan, not a lecture."""
    t = (msg or "").lower()
    # Whole thread matters: "sister" or "consent" alone are harmless, but as a follow-up
    # to "i want to fuck my sister" they continue the same abuse goal.
    full = ((context or "") + "\n" + t).lower()
    has_partner = any(p in full for p in _PARTNER)
    has_family = any(f in t or f in full for f in _FAMILY_ABUSE)
    has_minor = any(m in t or m in full for m in _MINOR)
    has_sex = any(s in t for s in _SEXUAL) or any(s in full for s in _SEXUAL)

    # 1) Coercion / non-consent / violence in this message — always refuse.
    if any(c in t for c in _COERCION):
        return True
    if any(v in t for v in _VIOLENCE):
        return True

    # 2) Any sexual content targeting a minor — refuse.
    if has_minor and has_sex:
        return True

    # 3) Family + sexual in the thread, NOT a partner relationship — refuse.
    #    A partner relationship clears it (girlfriend/wife are consenting adults).
    if has_sex and has_family and not has_partner:
        return True

    return False


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


def _global_chat_prompt(summary, history, user_name, message):
    return (
        f"=== BOARD STATE ===\n{summary}\n\n"
        f"=== CONVERSATION ===\n{history}\n\n"
        f"=== {user_name} says ===\n{message}\n\n"
        f"Reply as Eloise in a normal conversation. Answer exactly what was just asked, not what "
        f"you answered last time. Use the board state only when it's genuinely relevant — name "
        f"real goals and dates when you do. If the user lists options and asks which one to pick, "
        f"PICK ONE immediately and say why in one line — do not bounce the choice back and do not "
        f"say 'it's up to you'. If they repeat a question, do NOT copy your last "
        f"reply; say it fresh or push back. "
        f"Keep it SHORT: at most 2 LINES, but COMPLETE — actually answer the whole question, then "
        f"stop. Never leave it hanging, never end by asking them to re-ask. Do NOT echo this prompt. "
        f"Only output your reply."
    )


def _goal_chat_prompt(goal_name, plan_section, history, user_name, message):
    return (
        f"=== GOAL ===\n{goal_name}\n\n"
        f"{plan_section}"
        f"=== CONVERSATION ===\n{history}\n\n"
        f"=== {user_name} says ===\n{message}\n\n"
        f"Reply as Eloise. This is a conversation: read the LATEST question and answer that "
        f"exactly, instead of repeating your earlier answers:\n"
        f"- They ask 'what do I do now' -> name ONE concrete step from THE PLAN and how to start it.\n"
        f"- They ask what to SAY (a call, a text, to the person) -> give a short script of actual "
        f"words they can say.\n"
        f"- They list options (x / y / z) and ask which to CHOOSE, or ask 'what should she/he/they "
        f"wear' or 'which one' -> PICK ONE right now. Do NOT bounce the choice back, do NOT say "
        f"'it's up to you', do NOT route them to a plan task. Commit: one sentence choosing, one "
        f"line why, then end with a copy-paste text they can send.\n"
        f"- If they call you out for not answering or ask the same thing again -> DO NOT repeat or "
        f"restate; actually answer the thing this time, in fresh words, in AT MOST 2 LINES.\n"
        f"- Anything else -> answer it directly, keep it short.\n"
        f"If they say they ALREADY did something (talked, discussed, finished it), accept that and "
        f"move past it — do NOT re-instruct them to repeat it.\n"
        f"Keep it SHORT: at most 2 LINES, but COMPLETE — actually answer the whole question, then "
        f"stop. Never leave it hanging, never end by asking them to re-ask.\n"
        f"do NOT copy your last reply. Do NOT echo this prompt. Only output your reply."
    )


def generate_global_chat_reply(user_name, goals_summary, history, message, db=None):
    sys = BASE_SYSTEM_PROMPT
    summary = "; ".join(goals_summary) if goals_summary else "no goals on the board yet"
    user_prompt = _global_chat_prompt(summary, history, user_name, message)
    if _is_truly_harmful(message, context=history):
        return GUARDRAIL_REFUSAL, "guardrail"
    return _chat_llm(sys, user_prompt, db)


def stream_global_chat_reply(user_name, goals_summary, history, message, db=None):
    """Same as generate_global_chat_reply but streams text chunks."""
    sys = BASE_SYSTEM_PROMPT
    summary = "; ".join(goals_summary) if goals_summary else "no goals on the board yet"
    user_prompt = _global_chat_prompt(summary, history, user_name, message)
    if _is_truly_harmful(message, context=history):
        yield GUARDRAIL_REFUSAL, "guardrail"
        return
    for chunk, source in get_manager(db).stream_generate(
        sys, user_prompt, timeout=90, max_tokens=_MAX_CHAT_TOKENS,
    ):
        yield chunk, source


def generate_chat_reply(user_name, goal_name, history, message, db=None, plan_lines=""):
    sys = BASE_SYSTEM_PROMPT
    plan_section = (
        f"=== THE PLAN (your scheduled tasks) ===\n{plan_lines}\n\n" if plan_lines else ""
    )
    user_prompt = _goal_chat_prompt(goal_name, plan_section, history, user_name, message)
    if _is_truly_harmful(message, context=history):
        return GUARDRAIL_REFUSAL, "guardrail"
    return _chat_llm(sys, user_prompt, db)


def stream_chat_reply(user_name, goal_name, history, message, db=None, plan_lines=""):
    """Same as generate_chat_reply but streams text chunks. Yields (chunk, source)."""
    sys = BASE_SYSTEM_PROMPT
    plan_section = (
        f"=== THE PLAN (your scheduled tasks) ===\n{plan_lines}\n\n" if plan_lines else ""
    )
    user_prompt = _goal_chat_prompt(goal_name, plan_section, history, user_name, message)
    if _is_truly_harmful(message, context=history):
        yield GUARDRAIL_REFUSAL, "guardrail"
        return
    for chunk, source in get_manager(db).stream_generate(
        sys, user_prompt, timeout=90, max_tokens=_MAX_CHAT_TOKENS,
    ):
        yield chunk, source


def generate_opening_message(user_name, goal_summary, db=None):
    sys = BASE_SYSTEM_PROMPT
    user_prompt = f"User: {user_name}\nGoals: {goal_summary}\nSay a one-line opening."
    text, source = get_manager(db).generate_with_fallback(
        sys, user_prompt, fallback_fn=lambda: OPENING_FALLBACK
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
    # Romance / date / surprise / memorable night / anniversary
    elif any(w in t for w in ("romantic", "romance", "date", "anniversary", "valentine", "valentines",
                              "girlfriend", "gf", "wife", "partner", "husband", "boyfriend", "bf",
                              "memorable", "surprise her", "surprise him", "proposal", "propose",
                              "date night", "special night", "gift",
                              "hookup", "hook up", "sex night", "night together", "sleep with",
                              "sexual", "sex with", "intimate night", "bedroom")):
        questions = [
            {"key": "occasion", "q": "What's the occasion or date?", "hint": "anniversary, valentine, birthday, plain old 'I love you'"},
            {"key": "person", "q": "Who is this for, and what do they love?", "hint": "e.g. my wife — candles, live music, quiet dinner"},
            {"key": "budget", "q": "What's your budget ceiling?", "hint": "no cap, under $100, under $300"},
            {"key": "vibe", "q": "What vibe are you going for?", "hint": "intimate and calm, big and showy, playful and silly"},
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


def build_plan_quick(goal, extra_blocked=None):
    """Fast, deterministic, constraint-aware fallback schedule. Never blocks on the LLM."""
    return _build_fallback_plan(goal, extra_blocked=extra_blocked)


def user_blocked_windows(user):
    """Parse the user's global 'always busy' windows (e.g. gym 5-7pm) into minute tuples."""
    raw = None
    if isinstance(user, dict):
        raw = (user or {}).get("blocked_windows")
    elif user is not None:
        try:
            raw = user["blocked_windows"]
        except Exception:
            raw = None
    raw = raw or "[]"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    out = []
    for w in raw:
        parsed = parse_time_window(w)
        if parsed:
            out.append(parsed)
    return out


def _build_fallback_plan(goal, today=None, extra_blocked=None):
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
    # User-level "always busy" windows apply to EVERY goal in the same day.
    for w in (extra_blocked or []):
        if w:
            blocked.append(w)

    # Intimate-night goals get a real arc instead of the generic grid: build-up days,
    # THE EVENT EVENING the night before a free day (so she stays over), and a
    # return-home / morning-after day. That is what the user actually asked for
    # ("sex the night before, return home next day") instead of random days.
    if _goal_kind(goal.get("title") or goal["display_title"]) == "hookup":
        return _hookup_arc(today, deadline, blocked, details,
                           goal.get("title") or goal["display_title"])

    # Preferred work windows: three shifts — a morning start, an afternoon push, and
    # an evening close. Each anchor slides forward until it lands on a free 90-min block.
    morning = reminder_hh * 60 + reminder_mm
    if morning < 8 * 60:
        morning = 9 * 60
    if morning > 11 * 60:
        morning = 9 * 60
    afternoon = 16 * 60  # 16:00
    evening = 19 * 60    # 19:00 — final push before the lights dim

    def _day_bounds():
        # convert blocked windows into a busy silhouette over one day (minutes 0-1439),
        # splitting overnight windows across midnight
        busy = []
        for (s, e) in blocked:
            s = s % (24 * 60)
            e = (e - s) % (24 * 60) + s  # normalize span, keep end in (s, s+24h]
            if e > 24 * 60:
                busy.append((s, 24 * 60))
                busy.append((0, e - 24 * 60))
            else:
                busy.append((s, e))
        busy.sort()
        merged = []
        for (s, e) in busy:
            if merged and s < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    def _free_from(busy, start, taken):
        # earliest (s, e) 90-min window starting >= start that avoids every busy block
        # and every slot already taken this day
        obstacles = sorted(busy + list(taken))
        cur = start
        for (bs, be) in obstacles:
            if be <= cur:
                continue
            if bs >= cur + 90:
                break
            cur = be
        if cur + 90 > 22 * 60:
            return None
        return (cur, cur + 90)

    # Human-scale effort: how many sessions a day gets depends on how urgent the
    # goal is. A paper due in 1-2 days is a full-day grind (go all in — the gym can
    # wait), while a month-long goal gets lighter, spread-out sessions. This keeps a
    # day humanly possible instead of a wall of impossible blocks stacked there.
    load = 3 if days <= 2 else 2

    def slots_in_day():
        busy = _day_bounds()
        chosen = []
        for anchor in (morning, afternoon, evening):
            if len(chosen) >= load:
                break
            cand = _free_from(busy, anchor, chosen)
            if cand:
                chosen.append(cand)
        return chosen[:3]

    entries = []
    total_days = min(days, 15)
    # Split the user's own questionnaire answers (syllabus / topic / …) into one
    # concrete topic per day, else the fallback labels a session by the goal's own
    # title. NEVER canned Eloise wording — only the user's text (hard rule).
    split_topics = None
    for key in ("syllabus", "topic", "domain", "requirements"):
        val = (details.get(key) or "").strip()
        if val:
            parts = [p.strip() for p in re.split(r"[,;]|\band\b|\.", val) if p.strip()]
            if len(parts) >= 2:
                split_topics = parts
                break
    full_title = goal.get("title") or goal["display_title"]

    def _day_task(i, slot_idx):
        if total_days == 1:
            return str(full_title)
        if split_topics:
            return split_topics[i % len(split_topics)]
        return f"{full_title} — day {i + 1} part {slot_idx + 1}"

    for i in range(total_days):
        day = (date.fromisoformat(today) + timedelta(days=i)).isoformat()
        for slot_idx, (s, e) in enumerate(slots_in_day()):
            entries.append({
                "date": day,
                "title": _day_task(i, slot_idx),
                "start_time": f"{s // 60:02d}:{s % 60:02d}",
                "end_time": f"{e // 60:02d}:{e % 60:02d}",
                "status": "pending",
            })
    return entries


def _hookup_arc(today, deadline, blocked, details, goal_ref=None):
    """Deterministic schedule skeleton for an intimate-night goal. Guarantees:
    every day is covered, blocks never overlap within a day, and the structure is a
    real arc — prep days, then THE EVENT EVENING on the night before a free day,
    then a morning-after return-home day. Titles below are concrete per-block
    instructions (what to actually do in that hour), not canned filler."""
    details = details or {}

    def day_busy():
        busy = []
        for (bs, be) in blocked:
            s = bs % (24 * 60)
            e = (be - s) % (24 * 60) + s
            if e > 24 * 60:
                busy.append((s, 24 * 60))
                busy.append((0, e - 24 * 60))
            else:
                busy.append((s, e))
        busy.sort()
        merged = []
        for (s, e) in busy:
            if merged and s < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    def fit(busy, taken, start, dur):
        obstacles = sorted(list(busy) + list(taken))
        cur = start
        for (bs, be) in obstacles:
            if be <= cur:
                continue
            if bs >= cur + dur:
                break
            cur = be
        if cur + dur > 22 * 60:
            return None
        return (cur, cur + dur)

    day0 = date.fromisoformat(today)
    endd = date.fromisoformat(deadline)
    if endd < day0:
        endd = day0
    n = min(endd.toordinal() - day0.toordinal() + 1, 15)
    dates = [day0 + timedelta(days=i) for i in range(n)]

    # Event evening = the night before a free day if one exists so she stays over
    # and the user returns home the following morning. Favors the LAST such night.
    event_day = dates[-1]
    if n >= 2:
        for cand in reversed(dates[:-1]):
            if fit(day_busy(), [], 18 * 60, 240):
                event_day = cand
                break
        else:
            event_day = dates[-2]

    entries = []
    ords = {}

    def add(d, win, phase, seq, day_idx):
        key = (phase, seq)
        ords[key] = ords.get(key, 0) + 1
        entries.append({
            "date": d,
            "title": _fallback_task_label(phase, seq, goal_ref, ords[key]),
            "start_time": f"{win[0] // 60:02d}:{win[0] % 60:02d}",
            "end_time": f"{win[1] // 60:02d}:{win[1] % 60:02d}",
            "status": "pending",
        })

    for i, d in enumerate(dates):
        day = d.isoformat()
        b = day_busy()
        taken = []
        if d < event_day:
            m = fit(b, taken, 9 * 60, 90) or fit(b, taken, 8 * 60, 90)
            if m:
                taken.append(m)
                add(day, m, "prep", 0, i)
            ev = fit(b, taken, 17 * 60, 90)
            if ev:
                taken.append(ev)
                add(day, ev, "prep", 1, i)
            if m is None and ev is None:
                add(day, (9 * 60, 10 * 60), "prep", 0, i)
        elif d == event_day:
            if n >= 2:
                m = fit(b, taken, 15 * 60, 90)
                if m:
                    taken.append(m)
                    add(day, m, "event", 0, i)
            ev = fit(b, taken, 18 * 60, 240) or fit(b, taken, 19 * 60, 240)
            if ev:
                taken.append(ev)
                add(day, ev, "event", 1, i)
            else:
                tail = b[-1][1] if b else 18 * 60
                tail = max(tail, 18 * 60)
                add(day, (tail, min(tail + 240, 22 * 60)), "event", 1, i)
        else:
            m = fit(b, taken, 9 * 60, 60) or fit(b, taken, 8 * 60, 60)
            if m:
                taken.append(m)
                add(day, m, "return", 0, i)
            w = fit(b, taken, 18 * 60, 90)
            if w:
                taken.append(w)
                add(day, w, "return", 1, i)
            if m is None and w is None:
                add(day, (9 * 60, 10 * 60), "return", 0, i)
    return entries


def _fallback_task_label(phase, seq, goal_ref, ordinal=1):
    """Minimal, NON-fabricated label for a scheduled slot, used ONLY when the model
    can't be reached. It just names the slot from the user's OWN goal text (Eloise
    never writes canned messages here — user's hard rule). The real, concrete
    'what to do in this hour' wording always comes from the model when it's up."""
    label = {
        ("prep", 0): "Prep morning",
        ("prep", 1): "Prep evening",
        ("event", 0): "Day of",
        ("event", 1): "The night",
        ("return", 0): "Morning after",
        ("return", 1): "Wrap up",
    }.get((phase, seq), phase)
    ref = (goal_ref or "").strip()
    if len(ref) > 48:
        ref = ref[:45] + "..."
    if not ref:
        return label
    return f"{label} {ordinal}: {ref}"


def _overlaps(s, e, blocked):
    for (bs, be) in blocked:
        if s < be and bs < e:
            return True
    return False


def generate_plan(goal, user, db=None, extra_blocked=None):
    """Generate a real (or fallback) schedule for a goal. Returns list of action dicts."""
    entries = _build_fallback_plan(goal, extra_blocked=extra_blocked)
    # If no provider is immediately usable, skip the (slow, starve-prone) LLM attempt
    # and return the deterministic constraint-aware plan right away.
    try:
        if not get_manager(db).any_usable():
            return entries
    except Exception:
        return entries
    # Try the LLM for a richer plan; on any failure fall back to the deterministic one.
    try:
        res = _llm_plan_call(goal, db, extra_blocked=extra_blocked)
        if res:
            return res
    except Exception:
        pass
    return entries


def generate_plan_or_none(goal, user, db=None, extra_blocked=None):
    """Like generate_plan but returns None (not the fallback) when no LLM can serve a
    plan, so callers can keep a previously-drawn plan instead of overwriting it with the
    same deterministic fallback. Used for background schedule upgrades."""
    try:
        if not get_manager(db).any_usable():
            return None
    except Exception:
        return None
    try:
        return _llm_plan_call(goal, db, extra_blocked=extra_blocked)
    except Exception:
        return None


def _goal_kind(title):
    """Classify the goal into 'hookup', 'romance' or 'general' so the scheduler can
    give each a real structure instead of the same generic grid."""
    t = (title or "").lower()
    if any(w in t for w in (
            "hookup", "hook up", "sex night", "night together", "sleep with",
            "sexual", "intimate night", "fuck", "bedroom")):
        return "hookup"
    if any(w in t for w in (
            "romantic", "romance", "date", "anniversary", "valentine", "girlfriend", "gf",
            "wife", "partner", "husband", "boyfriend", "bf", "proposal", "memorable",
            "date night", "surprise", "gift")):
        return "romance"
    return "general"


def _plan_guidance(title, details_summary):
    """Build an INSTRUCTION (not a schedule) that tells the LLM what kind of real,
    concrete tasks this goal needs, so generation stays genuinely dynamic while
    directed at the goal type. The LLM still writes every title itself, in real time.
    The user's hard rule: every task title must be a COMPLETE concrete ACTION for that
    hour — a verb plus the actual thing (what to buy, set up, text, when) — pulled
    from the user's OWN answers and chat notes. Topic-words and vibe-words
    ('ensure', 'aftercare', 'make it special') are rejected as titles."""
    kind = _goal_kind(title)
    s = details_summary or ""
    if kind == "hookup":
        return (
            "PLAN THIS AS a real intimate night between consenting adults the user is actually "
            "about to have. Every title must be a COMPLETE CONCRETE ACTION performed IN that "
            "exact slot — a verb plus the real thing: what to buy, what to set up, what to text, "
            "what time, what to wear. Pull the specifics ONLY from the clarified answers and chat "
            "notes (the item already chosen, the agreed time, the place, who's coming). "
            "Forbidden as titles: topic-words and vibe-words ('ensure aftercare', 'confirm safety', "
            "'discuss energy', 'check in with her', 'plan the evening') — those are NOT actions "
            "and must never be scheduled. The night slot itself is named as the real plan from the "
            "notes. This is practical logistics, not a lecture."
        ) + (f" Details: {s}." if s else ".")
    if kind == "romance":
        return (
            "PLAN THIS AS a real date/romantic experience the user is executing. Every title must "
            "be a COMPLETE CONCRETE ACTION performed IN that exact slot — a verb plus the real "
            "thing: which gift, which flowers, what the note says, where, what time. Pull the "
            "specifics ONLY from the clarified answers (person, occasion, budget, vibe) and chat "
            "notes. Forbidden as titles: topic-words and vibe-words ('ensure', 'consider', 'make "
            "it special', 'plan the surprise', 'discuss feelings') — those are NOT actions. The "
            "moment itself is named as the real plan from the notes. This is logistics, not a lecture."
        ) + (f" Details: {s}." if s else ".")
    return ""


def _goal_chat_log(db, goal):
    """Pull the goal's chat history (the 'chat notes' the user writes while working)
    so a regenerated schedule can respect work the user already reports done."""
    if db is None or not goal or not goal.get("id"):
        return ""
    try:
        rows = db.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE goal_id=? AND user_id=? ORDER BY id ASC LIMIT 60",
            (goal["id"], goal.get("user_id") or 0),
        ).fetchall()
        if not rows:
            return ""
        lines = [f"{r['role']}: {r['content']}".strip() for r in rows]
        return "\n".join(lines)
    except Exception:
        return ""


def _llm_plan_call(goal, db, timeout=180, extra_blocked=None):
    sys = BASE_SYSTEM_PROMPT
    deadline = goal["deadline"]
    title = (goal.get("title") or goal["display_title"]).strip()
    cons = goal.get("constraints") or "[]"
    blocked_line = ""
    if extra_blocked:
        windows = ", ".join(
            f"{b[0] // 60:02d}:{b[0] % 60:02d}-{b[1] // 60:02d}:{b[1] % 60:02d}" for b in extra_blocked
        )
        blocked_line = f"\nUser is ALWAYS unavailable in these windows (do not plan tasks here): {windows}"
    detail_line = ""
    details_summary = goal_details_summary(goal.get("details"))
    if details_summary:
        detail_line = f"\nClarified answers from the user: {details_summary}"
    guidance = _plan_guidance(title, details_summary)
    guidance_line = f"\n{guidance}" if guidance else ""
    # The chat log is the user's working notes on this file. Read it so the schedule
    # does not re-schedule things the user already said they did, and so it flows out
    # of the actual conversation rather than a cold template.
    chat_log = _goal_chat_log(db, goal)
    chat_line = ""
    if chat_log:
        chat_line = (
            "\n=== CHAT NOTES (the user's own conversation on this file) ===\n"
            f"{chat_log}\n"
            "=== END CHAT NOTES ===\n"
            "Read those notes BEFORE building the plan. If the user already said a task "
            "or a specific block/time is done or cancelled, DO NOT re-schedule it as pending. "
            "Plan forward from what is still outstanding. Match real dates/times the user "
            "mentioned. Do not invent new topics unrelated to the goal."
        )
    today = date.today().isoformat()
    fallback = _build_fallback_plan(goal, today=today, extra_blocked=extra_blocked) or []
    if not fallback:
        return None
    slots_json = "[\n" + ",\n".join(
        f'  {{"date": "{e["date"]}", "start_time": "{(e["start_time"] or "09:00")}", '
        f'"end_time": "{(e["end_time"] or "10:30")}"}}' for e in fallback
    ) + "\n]"
    user_prompt = (
        f"Build the day-by-day plan from {date.today().isoformat()} to {deadline} for: {title}\n"
        f"THE GOAL IS: {title}\n"
        f"Constraints: {cons}"
        f"{blocked_line}"
        f"{chat_line}"
        f"{detail_line}"
        f"{guidance_line}\n"
        "Below are the EXACT day/time slots the plan must fill (each slot is one task). "
        "They already respect the user's constraints and ensure this rule: Cover EVERY day "
        "from today to the deadline with at least 1 task — a goal spanning N days totals "
        "roughly 2xN tasks, never fewer than one per day, no more than 3 per day. "
        "DO NOT change any slot's date or start_time or end_time. Returns your titles:\n"
        f"{slots_json}\n"
        "Return ONLY a JSON array with ONE object per slot, in the same order: "
        '{"date":"YYYY-MM-DD","start_time":"HH:MM","end_time":"HH:MM","title":"..."}. '
        "Each title tells the user EXACTLY WHAT TO DO in that exact slot, that exact day: a verb + "
        "the real thing (what to buy, what to set up, what to text, what time, what to say). "
        "UP TO TWO SHORT LINES per title (at most ~16 words total) — it must fit the slot's own "
        "timeframe and it is still a complete, concrete instruction. E.g. instead of "
        "'Ensure a smooth evening' write 'Pick the three-piece, text her, agree 8pm. Candles, "
        "slow music, door at 8.' Pull "
        "the specifics from the chat notes and clarified answers (the item already chosen, the "
        "agreed time, the place, who's coming) and match the goal text; inventing unrelated "
        "topics is a hard failure. "
        "Forbidden ANYWHERE in a title (they are topics, not actions): ensure, confirm, "
        "consider, review, research, prepare, discuss, debate, decide, brainstorm, 'make sure', "
        "'plan the'. 'Ensure aftercare and safety' is REJECTED. "
        "Each slot gets a different, specific task. Never schedule the same conversation topic "
        "on separate days. Do not schedule anything the chat notes say is already done. "
        "If a slot is on an evening and the next day is free, that slot is THE EVENT NIGHT "
        "itself — name what the night actually is, using the chat notes' decisions (people, "
        "chosen items, agreed times). Every title is a physical, concrete action the user does "
        "in that slot, not a lecture and not a vibe."
    )
    res = get_manager(db).generate(sys, user_prompt, timeout=timeout)
    if not res.ok:
        return None
    global _last_provider
    _last_provider = res.provider or "llm"
    title_map = _parse_title_map(res.text)
    out = []
    for e in fallback:
        t = title_map.get((e["date"], e["start_time"]))
        if not _title_ok(t):
            t = e["title"]
        out.append({**e, "title": t})
    return out


_WEAK_STARTERS = {
    "ensure", "confirm", "consider", "review", "research", "prepare", "prep",
    "discuss", "debate", "brainstorm", "decide", "check", "plan", "remember",
    "think", "make", "brain-storm", "brainstorm",
}


def _title_ok(title):
    """Accept a model-written block title only if it's a real, concrete instruction
    of up to two short lines describing what to do in that slot.
    Otherwise the deterministic per-block title is kept."""
    if not title:
        return False
    stripped = title.strip()
    if "\n" in stripped and stripped.count("\n") > 1:
        return False
    if len(stripped) > 180:
        return False
    words = [w for w in re.split(r"\s+", stripped) if w.lower() not in
             ("the", "a", "an", "of", "to", "for", "and", "with")]
    if len(words) < 3:
        return False
    low = stripped.lower()
    if any(w in low for w in ("consent & boundaries", "respect every no", "lecture")):
        return False
    if any(v in low for v in ("research", "polish", "prepare", "discuss the plan",
                              "work on it", "brainstorm topics", "review the")):
        return False
    # A title that STARTS with a weak planning verb ("Ensure aftercare and safety",
    # "Confirm way home") is a topic/vibe, not an action. Reject it so the user's
    # schedule is concrete, never a brainstorm.
    first = re.split(r"[\s:]+", stripped, 1)[0].lower().rstrip(".")
    if first in _WEAK_STARTERS:
        return False
    return True


def _parse_title_map(text):
    """Read the model's [ {...} ] array (Ollama often wraps it in markdown fences)
    and map (date, start_time) -> title. Tolerates 'start'/'end' keys and missing times."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    for item in data:
        d = item.get("date")
        t = item.get("title")
        if not d or not t:
            continue
        st = normalize_time(item.get("start_time") or item.get("start")) or ""
        if st:
            out[(d, st)] = t.strip()[:200]
    return out


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