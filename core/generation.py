import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta

from core.llm import LLMManager

logger = logging.getLogger("eloise.plan")
from core.llm.manager import _VERDICT_ECHO, _strip_prompt_echo
from core import planner
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

_MAX_CHAT_TOKENS = 400

_MAX_PLAN_TOKENS = 1536
_PLAN_SLICE_TARGET_SLOTS = 8
_PLAN_SLICE_MAX_CALLS = 8

def _chat_model_override():
    try:
        return os.environ.get("OLLAMA_CHAT_MODEL") or None
    except Exception:
        return None


def _chat_lock_wait():
    return 30

_state = threading.local()


def set_allocation(alloc):
    _state.allocation = alloc


def get_allocation():
    return getattr(_state, "allocation", None)


def plan_blocked_reason():
    return getattr(_state, "plan_blocked_reason", None)


def last_outline():
    return getattr(_state, "outline", None)


def last_provider():
    return getattr(_state, "last_provider", None)


def last_llm_error():
    return getattr(_state, "last_error", None)


def get_manager(db=None):
    global _manager
    if _manager is None:
        _manager = LLMManager()
    return _manager


_PERSONA_TRIGGERS = {
    "motivational": "NEVER be a motivational speaker, cheerleader, or hype-person. No 'you can do it!', no pumping-up, no empty positivity. Be dry and real instead.",
    "sarcast": "Be wry, dry and sarcastic by default — make it obvious you are not impressed.",
    "abuse": "Be playfully abusive with excuses: mock them hard before redirecting. Behaviour gets mocked, they do not (no genuine cruelty, no threats, no insults about who they are).",
    "abusive": "Be playfully abusive with excuses: mock them hard before redirecting. Behaviour gets mocked, they do not (no genuine cruelty, no threats, no insults about who they are).",
    "insult": "Roast them lightly and regularly — tease, don't wound.",
    "roast": "Roast them lightly and regularly — tease, don't wound.",
    "mean": "Be unsparingly honest. Soft-pedalling and reassurance are banned.",
    "attack": "Attack excuses and procrastination directly, every time you catch it.",
    "nasty": "Turn the cheek-teasing up; stay funny, stay legal.",
}


def user_persona(db, user_id):
    try:
        row = db.execute("SELECT persona FROM users WHERE id=?", (user_id,)).fetchone()
        return (row and row["persona"]) or ""
    except Exception:
        return ""


def absorb_persona_directive(db, user_id, message):
    """If the user just set Eloise's personality in plain chat, store it as a"""
    t = (message or "").lower()
    clauses = []
    for key, clause in _PERSONA_TRIGGERS.items():
        if key in t and clause not in clauses:
            clauses.append(clause)
    if not clauses:
        return ""
    stored = user_persona(db, user_id)
    merged = stored
    for c in clauses:
        if c not in merged:
            merged = (merged + "\n" + c).strip()
    try:
        db.execute("UPDATE users SET persona=? WHERE id=?", (merged, user_id))
        db.commit()
    except Exception:
        return stored
    return merged


DEFAULT_LOCATION = "Pakistan"


def user_location(db, user_id):
    """The user's country/city — used to keep suggestions to services that"""
    try:
        row = db.execute("SELECT location FROM users WHERE id=?", (user_id,)).fetchone()
        return ((row and row["location"]) or "").strip() or DEFAULT_LOCATION
    except Exception:
        return DEFAULT_LOCATION


def chat_system_prompt(db, user_id):
    return _with_persona(
        BASE_SYSTEM_PROMPT,
        user_persona(db, user_id),
        location=user_location(db, user_id),
    )


_STEP_DONE_RE = re.compile(
    r"^\s*(done|did it|did that|finished|finished it|done with it|done with that|"
    r"got it done|wrapped( up)?|moved on|next|next step|what('| i)?s next|what do i do now|"
    r"ok done|yeah done|all set|im done|i'm done|i am done|onto the next|on to the next)\s*[.!]?\s*$",
    re.I,
)
_GOAL_DONE_STRONG = [
    "the goal", "this goal", "whole thing", "the website", "the site", "the startup",
    "shipped", "launched", "submitted", "published", "released", "deployed",
    "the phase", "the project", "goal is done", "goal's done", "goal is complete",
    "deadline", "finished the goal", "done with the goal", "completed the goal",
]


def step_done_detected(message):
    """True when the user implies the CURRENT task/step is finished and wants"""
    text = (message or "").strip()
    if not text or len(text.split()) > 16:
        return False
    if "?" in text and not re.search(r"what('| i)?s next|next\b", text.lower()):
        return False
    low = text.lower()
    if _STEP_DONE_RE.match(text):
        return True
    if any(p in low for p in (" done ", "done with ", "finished ", "finished the ", "did it")):
        return True
    if "done" == low or low in ("nice", "good", "ok"):
        return True
    return False


def goal_completion_detected(message):
    text = (message or "").strip()
    if not text or len(text.split()) > 16:
        return False
    low = text.lower()
    if not completion_detected(message):
        return False
    return any(w in low for w in _GOAL_DONE_STRONG)


GUARDRAIL_REFUSAL = (
    "No. Minors, anyone without consent, or family — that never goes on this board, "
    "and I won't plan it or discuss it. A real goal with a consenting adult partner, "
    "a date, a trip, a night that actually matters: name it and I'll build it properly. "
    "That's the deal."
)


def _chat_llm(sys_prompt, user_prompt, db, user_message=""):
    result = get_manager(db).generate(
        sys_prompt, user_prompt, timeout=90, max_tokens=_MAX_CHAT_TOKENS,
        model_override=_chat_model_override(), lock_wait=_chat_lock_wait(),
    )
    if not result.ok:
        return "", "offline"
    if _VERDICT_ECHO.search(result.text[:80]):
        return "", "offline"
    _state.last_provider = result.provider or "llm"
    cleaned = _strip_prompt_echo(result.text, user_message=user_message)
    return cleaned, result.provider or "llm"


def _pick(items):
    return items[(datetime.now().microsecond // 100) % len(items)]


def _word_re(words):
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


_FAMILY_RE = _word_re([
    "sister", "brother", "sibling", "mom", "mum", "mother", "dad", "father", "daughter",
    "son", "aunt", "uncle", "niece", "nephew", "stepmom", "stepdad", "step-sister",
    "step-brother", "cousin",
])
_MINOR_RE = _word_re([
    "child", "children", "kid", "kids", "minor", "underage", "teen", "teens", "teenage",
    "infant", "baby", "schoolgirl", "school-girl", "school boy", "schoolboy", "gril",
    "years old", "year old", "year-old", "years-old",
])
_PARTNER_RE = _word_re([
    "girlfriend", "gf", "wife", "wifey", "husband", "boyfriend", "bf", "partner",
    "spouse", "fiance", "fiancé", "fiancee", "fiancée", "lover",
])
_PARTNER_PHRASES = ["my girl", "my man", "babe", "bae"]
_SEXUAL_RE = _word_re([
    "dick", "penis", "fuck", "fucking", "sex", "sexual", "rape", "raping", "raped",
    "pussy", "vagina", "blowjob", "handjob", "cum", "cumming", "orgasm", "masturbate",
    "masturbation", "naked", "nude", "boobs", "breasts", "ejaculate", "incest",
    "molest", "creampie", "porn",
])
_SEXUAL_PHRASES = [
    "sleep with", "make love", "have sex", "sex with", "intimate with", "in bed with",
    "first night", "wedding night", "honeymoon", "hook up", "hookup",
]
_COERCION_RE = _word_re([
    "rape", "raping", "raped", "drugged", "knock out", "knocked out", "blackmail",
    "blackmailed", "threaten", "threatened",
])
_COERCION_PHRASES = [
    "force her", "force him", "force you", "without her consent", "without his consent",
    "no consent", "drug her", "drug him", "hold her down", "hold him down",
    "tie her up", "tie him up", "tied up",
]
_VIOLENCE_RE = _word_re([
    "murder", "stab", "stabbing", "bomb", "poison", "assault", "strangle", "choke",
])
_VIOLENCE_PHRASES = ["kill my", "shoot my", "beat up", "hurt my"]


def _has_phrase(t, phrases):
    return any(p in t for p in phrases)


def _is_truly_harmful(msg, context=""):
    """Detect requests Eloise must refuse. Returns True for:"""
    t = (msg or "").lower()
    user_lines = [
        ln for ln in (context or "").split("\n")
        if not ln.strip().lower().startswith(("eloise:", "assistant:"))
    ]
    full = ("\n".join(user_lines) + "\n" + t).lower()
    has_partner = bool(_PARTNER_RE.search(full)) or _has_phrase(full, _PARTNER_PHRASES)
    has_family = bool(_FAMILY_RE.search(full))
    has_minor = bool(_MINOR_RE.search(full))
    has_sex = bool(_SEXUAL_RE.search(t)) or _has_phrase(t, _SEXUAL_PHRASES) \
        or bool(_SEXUAL_RE.search(full)) or _has_phrase(full, _SEXUAL_PHRASES)

    if _COERCION_RE.search(t) or _has_phrase(t, _COERCION_PHRASES):
        return True
    if _VIOLENCE_RE.search(t) or _has_phrase(t, _VIOLENCE_PHRASES):
        return True

    if has_minor and has_sex:
        return True

    if has_sex and has_family and not has_partner:
        return True

    return False


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def days_remaining(deadline, today=None):
    today = today or date.today().isoformat()
    return days_between(today, deadline)


def _global_chat_prompt(summary, history, user_name, message):
    return (
        f"=== BOARD STATE ===\n{summary}\n\n"
        f"=== CONVERSATION ===\n{history}\n\n"
        f"=== {user_name} says ===\n{message}\n\n"
        f"Reply as Eloise in a normal conversation. Answer exactly what was just asked, not what "
        f"you answered last time. GROUNDING: your ONLY sources of truth are the active goals, "
        f"their dates, and today's tasks listed above — never invent a goal, date, task, or fact "
        f"that is not written there. When you do reference something, quote the real one from the "
        f"list. If the user asks about something that is NOT on their board, say plainly it isn't "
        f"scheduled yet instead of making one up. Use the board only when it's genuinely "
        f"relevant. If the user lists options and asks which one to pick, "
        f"PICK ONE immediately and say why in one line — do not bounce the choice back and do not "
        f"say 'it's up to you'. If they repeat a question, do NOT copy your last "
        f"reply; say it fresh or push back. "
        f"Keep it SHORT: at most 2 LINES, but COMPLETE — actually answer the whole question, then "
        f"stop. Never leave it hanging, never end by asking them to re-ask. Do NOT echo this prompt. "
        f"PRIORITIES: when the board shows a PRIORITY CALL, that ranking is computed from real "
        f"deadlines and workload, so own it: say it as your judgment, first, with the actual "
        f"reason and number, naming the soft thing the call says can wait (e.g. '<that thing> can wait "
        f"today - the exam is 5 days out and 11 hours are still open.'). Never hedge it with 'maybe' or 'you might consider'. If there is no "
        f"PRIORITY CALL, do not invent a crisis. "
        f"Only output your reply."
    )


def _goal_chat_prompt(goal_name, plan_section, history, user_name, message, step_done=False):
    step_rule = ""
    if step_done:
        step_rule = (
            f"- The user just reported the CURRENT step done, or is moving on. Do NOT "
            f"re-instruct the task you gave a moment ago and do NOT repeat a task line "
            f"already quoted earlier in CONVERSATION. Name the NEXT pending task from THE "
            f"PLAN (quote its exact title and time). If no other task exists, say this was "
            f"the only task left and ask if they want you to redraw the schedule.\n"
        )
    return (
        f"=== GOAL ===\n{goal_name}\n\n"
        f"{plan_section}"
        f"=== CONVERSATION ===\n{history}\n\n"
        f"=== {user_name} says ===\n{message}\n\n"
        f"Reply as Eloise. This is a conversation: read the LATEST question and answer that "
        f"exactly, instead of repeating your earlier answers:\n"
        f"- If THE PLAN says no schedule has been drawn yet, then THERE IS NO concrete task to "
        f"point at. Say in ONE line that the schedule is still being drawn (the plan model is "
        f"unreachable or failing right now) and offer to redraw it. Do NOT invent tasks, phases, "
        f"or a 'next step' — there is nothing on the board to quote. Answer general advice from "
        f"the GOAL and conversation only, never a phase checklist.\n"
        f"{step_rule}"
        f"- They ask 'what do I do now' -> name ONE concrete step from THE PLAN and how to start it.\n"
        f"- They ask what to SAY (a call, a text, to the person) -> give a short script of actual "
        f"words they can say.\n"
        f"- They list options (x / y / z) and ask which to CHOOSE, or ask 'what should she/he/they "
        f"wear' or 'which one' -> PICK ONE right now. Do NOT bounce the choice back, do NOT say "
        f"'it's up to you', do NOT route them to a plan task. Commit: one sentence choosing, one "
        f"line why, then end with a copy-paste text they can send.\n"
        f"- If they call you out for not answering or ask the same thing again -> DO NOT repeat or "
        f"restate; actually answer the thing this time, in fresh words, in AT MOST 2 LINES.\n"
        f"- If their message is personal or emotional — 'I feel', 'should I/we', 'is it ok if…' — and "
        f"has nothing to do with the schedule, answer THAT directly in 1-2 lines and do NOT bring "
        f"up THE PLAN, tasks, dates, or times at all.\n"
        f"- Anything else -> answer it directly, keep it short.\n"
        f"NEVER output text that is identical, or nearly identical, to an earlier Eloise message "
        f"in CONVERSATION — even if the question looks the same, say it fresh.\n"
        f"GROUNDING: every time you name a step, a task, or a time, it must be QUOTED from THE "
        f"PLAN listed above — never invent tasks, dates, or times that are not literally written "
        f"there. If the user asks about something that is NOT on their schedule, say it plainly "
        f"('that is not on your schedule yet') instead of making it up, and if they ask what to "
        f"do now, name ONE real step quoted from THE PLAN.\n"
        f"If they say they ALREADY did something (talked, discussed, finished it), accept that and "
        f"move past it in ONE line — say 'nice' or 'good' and name the NEXT real step from THE "
        f"PLAN, or ask if they want to redraw the schedule. Do NOT re-instruct them to repeat "
        f"what they just did, and never recite the same task again.\n"
        f"If an earlier Eloise message already contains the exact plan line you are about to "
        f"output, output something NEW instead — do not emit the identical line twice.\n"
        f"Keep it SHORT: at most 2 LINES, but COMPLETE — actually answer the whole question, then "
        f"stop. Never leave it hanging, never end by asking them to re-ask.\n"
        f"do NOT copy your last reply. Do NOT echo this prompt. Only output your reply."
    )


def _clean_local_stream(stream_iter, user_message="", user_lines=None):
    buf = []
    source = None
    try:
        for chunk, src in stream_iter:
            if src and src != "offline":
                source = src
            if chunk:
                buf.append(chunk)
            elif src is not None:
                break
    except RuntimeError:
        raise
    cleaned = _strip_prompt_echo("".join(buf), user_message=user_message, user_lines=user_lines)
    if cleaned:
        yield cleaned, (source or "ollama")
    yield "", (source or "ollama")


def generate_global_chat_reply(user_name, goals_summary, history, message, db=None, persona="",
                               location=""):
    sys = _with_persona(BASE_SYSTEM_PROMPT, persona, location=location)
    summary = "; ".join(goals_summary) if goals_summary else "no goals on the board yet"
    user_prompt = _global_chat_prompt(summary, history, user_name, message)
    if _is_truly_harmful(message, context=history):
        return GUARDRAIL_REFUSAL, "guardrail"
    return _chat_llm(sys, user_prompt, db, user_message=message)


def stream_global_chat_reply(user_name, goals_summary, history, message, db=None, persona="",
                             location=""):
    sys = _with_persona(BASE_SYSTEM_PROMPT, persona, location=location)
    summary = "; ".join(goals_summary) if goals_summary else "no goals on the board yet"
    user_prompt = _global_chat_prompt(summary, history, user_name, message)
   
    if _is_truly_harmful(message, context=history):
        yield GUARDRAIL_REFUSAL, "guardrail"
        return
   
    yield from _clean_local_stream(get_manager(db).stream_generate(
        sys, user_prompt, timeout=90, max_tokens=_MAX_CHAT_TOKENS,
        model_override=_chat_model_override(), lock_wait=_chat_lock_wait(),
    ), user_message=message)


def _plan_section(plan_lines):
    if plan_lines:
        return f"=== THE PLAN (your scheduled tasks) ===\n{plan_lines}\n\n"
    return (
        "=== THE PLAN ===\n(No schedule has been drawn for this goal yet — the plan model is "
        "unreachable or failing right now, so there are no concrete tasks to point at. Say so "
        "honestly instead of inventing a step, and offer to redraw the schedule.)\n\n"
    )


def _with_persona(base, persona, location=""):
    sys = base
   
    if persona:
        sys += (
            "\n\nYou have a standing personality contract with this user that they set "
            "explicitly. It OVERRIDES the default tone above. Follow it to the letter:\n"
            + persona
        )
    loc = (location or "").strip()
    if loc:
        sys += (
            f"\n\nLOCATION: this user is based in {loc}. Only suggest services, apps, "
            "shops, restaurants, delivery, payments, transport or venues that actually "
            "exist and operate there — never assume US-only options (no DoorDash, "
            "Target, CVS…) unless they truly exist in that country. Money amounts in "
            "the local currency when natural. If a real-world step depends on local "
            "infrastructure, adapt it to theirs."
        )
   
    else:
        sys += (
            "\n\nLOCATION: the user's country is not set. Never assume US-only "
            "services; prefer globally-available or online options, and if a "
            "recommendation truly depends on their country, ask it in one line."
        )
    return sys


def generate_chat_reply(user_name, goal_name, history, message, db=None, plan_lines="",
                        persona="", step_done=False, location=""):
    sys = _with_persona(BASE_SYSTEM_PROMPT, persona, location=location)
    plan_section = _plan_section(plan_lines)
    user_prompt = _goal_chat_prompt(goal_name, plan_section, history, user_name, message,
                                    step_done=step_done)
    
    if _is_truly_harmful(message, context=history):
        return GUARDRAIL_REFUSAL, "guardrail"
    return _chat_llm(sys, user_prompt, db, user_message=message)


def stream_chat_reply(user_name, goal_name, history, message, db=None, plan_lines="",
                      persona="", step_done=False, location=""):
    sys = _with_persona(BASE_SYSTEM_PROMPT, persona, location=location)
    plan_section = _plan_section(plan_lines)
    user_prompt = _goal_chat_prompt(goal_name, plan_section, history, user_name, message,
                                    step_done=step_done)
  
    if _is_truly_harmful(message, context=history):
        yield GUARDRAIL_REFUSAL, "guardrail"
        return
    
    yield from _clean_local_stream(get_manager(db).stream_generate(
        sys, user_prompt, timeout=90, max_tokens=_MAX_CHAT_TOKENS,
        model_override=_chat_model_override(), lock_wait=_chat_lock_wait(),
    ), user_message=message, user_lines=_history_user_lines(history))


def _history_user_lines(history):
    out = []
   
    if not history:
        return out
   
    for ln in history.split("\n"):
        if ln.strip().lower().startswith(("user:", "user|")):
            out.append(ln.split(":", 1)[1].strip()[:200])
    return out


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


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_time(t):
    t = (t or "").strip()
    m = TIME_RE.match(t)
  
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def parse_time_window(text):
    text = text or ""
    low = text.lower()
    m = re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", low)
  
    if len(m) < 2:
        return None
    entries = [(int(n), int(mm) if mm else 0, mer) for n, mm, mer in m]
    n = len(entries)
    has_any = any(mer for _, _, mer in entries)

    if not has_any:
        start = _hh_12_to_24(entries[0][0], "am") * 60 + entries[0][1]
        end = _hh_12_to_24(entries[-1][0], "am") * 60 + entries[-1][1]
        if end <= start:
            start = _hh_12_to_24(entries[0][0], "am") * 60 + entries[0][1]
            end = _hh_12_to_24(entries[-1][0], "pm") * 60 + entries[-1][1]
        return (start, end)

    times = [None] * n
    for i, (hh, mm, mer) in enumerate(entries):
        if mer:
            times[i] = _hh_12_to_24(hh, mer) * 60 + mm
    for i in range(n):
        if times[i] is not None:
            continue
        inh = None
        for j in range(i + 1, n):
            if entries[j][2]:
                inh = entries[j][2]
                break
     
        if inh is None:
            for j in range(i - 1, -1, -1):
                if entries[j][2]:
                    inh = entries[j][2]
                    break
        inh = inh or "am"
        times[i] = _hh_12_to_24(entries[i][0], inh) * 60 + entries[i][1]

    start, end = times[0], times[-1]
 
    if not entries[0][2] and entries[-1][2] == "pm" and entries[0][0] >= entries[-1][0]:
        start = _hh_12_to_24(entries[0][0], "pm" if entries[0][0] == 12 else "am") * 60 + entries[0][1]
    if end < start:
        end += 24 * 60
    return (start, end)


def _hh_12_to_24(hh, mer):
    if hh == 12:
        return 0 if mer == "am" else 12
    return hh + 12 if mer == "pm" else hh


def parse_clock_list(text):
    """Pull every clock time out of a string into minutes from midnight"""
    out = []
    for hh, mm, mer in re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", (text or "").lower()):
        h, m = int(hh), int(mm) if mm else 0
        if mer:
            h = _hh_12_to_24(h, mer)
        if 0 <= h < 24 and 0 <= m < 60:
            out.append(h * 60 + m)
    return out


def parse_routine_windows(details):
    d = details or {}
    windows = []
    sleep_times = parse_clock_list(d.get("sleep_schedule") or "")
    if len(sleep_times) >= 2:
        windows.append((sleep_times[0], sleep_times[1]))
    for t in parse_clock_list(d.get("meal_times") or "")[:3]:
        windows.append((t, t + 45))
    mv = (d.get("movement_time") or "").strip().lower()
    if mv and mv not in ("none", "no", "n/a", "-", "skip", "0"):
        mt = parse_clock_list(d.get("movement_time") or "")
        if mt:
            windows.append((mt[0], mt[0] + 60))
    return windows


_DEFAULT_HUMAN_WINDOWS = [
    (23 * 60, 7 * 60),
    (7 * 60, 8 * 60),
    (13 * 60, 13 * 60 + 45),
    (18 * 60, 19 * 60),
    (19 * 60 + 30, 20 * 60 + 15),
]


def generate_clarify_questions(title, db=None, deadline="", known="", unknown=()):
    ask_routine = ""
    if unknown:
        ask_routine = (
            f"NOT KNOWN YET: {', '.join(unknown)}. Ask about these ONLY if they change this plan, "
            "one short question at most, using key sleep_schedule (hours asleep) or meal_times "
            "(when they eat).\n"
        )
    sys = (
        "You are Eloise. The person just filed a goal. Ask the few questions whose answers "
        "change the schedule you will build. Plain everyday words, each question 9 words or "
        "fewer, answerable in a few words. Never ask what is already known."
    )
    prompt = (
        f"Goal: {title}\nDeadline: {deadline}\n"
        f"ALREADY KNOWN (never ask): {known or 'nothing yet'}\n{ask_routine}"
        "Ask at most 3 questions, fewer if the goal is simple. Prefer facts only this person "
        "knows (level, material, budget, who it is for, tools). Return ONLY a JSON array of "
        '{"key":"short_key","q":"question","hint":"tiny example"} - nothing else.'
    )
    try:
        result = get_manager(db).generate(sys, prompt, timeout=30, max_tokens=300)
    except Exception:
        return []
    if not (result is not None and getattr(result, "ok", False) and result.text):
        return []
    m = re.search(r"\[.*\]", result.text, re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return []
    qs = []
    for i, item in enumerate(raw[:3]):
        if not isinstance(item, dict):
            continue
        q = " ".join(str(item.get("q") or "").split())
        if not q or len(q.split()) > 16:
            continue
        key = re.sub(r"[^a-z0-9_]", "_", str(item.get("key") or f"q{i + 1}").lower()).strip("_")[:32]
        qs.append({"key": key or f"q{i + 1}", "q": q[:140], "hint": str(item.get("hint") or "").strip()[:80]})
    return qs


def goal_details_summary(details_json):
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
    return _build_fallback_plan(goal, extra_blocked=extra_blocked)


def user_blocked_windows(user):
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


def user_calendar_busy(db, user_id, exclude_goal_id, start_date, end_date, only_goal_ids=None):
    busy = {}
    if db is None:
        return busy
    try:
        rows = db.execute(
            "SELECT date, start_time, end_time, goal_id FROM actions "
            "WHERE user_id=? AND goal_id!=? AND status!='done' "
            "AND date>=? AND date<=?",
            (user_id, exclude_goal_id, start_date, end_date),
        ).fetchall()
        if only_goal_ids is not None:
            rows = [r for r in rows if r["goal_id"] in only_goal_ids]
    except Exception:
        return busy
    for r in rows:
        s, e = None, None
        try:
            hs, ms = r["start_time"].split(":")
            s = int(hs) * 60 + int(ms)
            he, me = r["end_time"].split(":")
            e = int(he) * 60 + int(me)
        except Exception:
            s = e = None
        if s is None or e is None:
            continue
        busy.setdefault(r["date"], []).append((s, e))
    return busy


def _is_event_goal(text):
    b = getattr(_state_brief, "b", None)
    return bool(b and b.get("kind") == "event")


_HABIT_WORDS = ("gym", "workout", "work out", "six pack", "six-pack", "6 pack", "6-pack", "abs",
                "fitness", "get fit", "lose weight", "weight loss", "fat loss", "muscle", "bulk",
                "cut ", "run ", "running", "marathon", "yoga", "diet", "walk daily", "stretch",
                "pushup", "push-up", "pull-up", "body", "lose ", "slim", "stamina", "cardio", "jogging", "calisthenic")
_WORKOUT_LABELS = ("gym", "workout", "work out", "run", "jog", "yoga", "swim", "training", "exercise", "sport")
_EVENT_BUDGET_MIN = {"hookup": 360, "romance": 300, "event": 420}
_EVENT_ACTIVE_DAYS = 3
_state_workout = threading.local()


def set_workout_windows(windows):
    """[(start_min, end_min), ...] - the user's own gym/exercise windows"""
    _state_workout.windows = list(windows or [])


def workout_windows():
    return list(getattr(_state_workout, "windows", []) or [])


def is_workout_label(text):
    low = (text or "").lower()
    return any(re.search(rf"\b{re.escape(w)}", low) for w in _WORKOUT_LABELS)


_state_brief = threading.local()


def set_brief(b):
    _state_brief.b = b


def goal_brief(title, db=None, deadline=""):
    sys = ("You size real-life goals the way a sensible person would. Reply with ONLY JSON: "
           '{"kind":"event|habit|project","total_hours":number,"active_days":number,'
           '"physical":true|false,"per_week":number,"best_time":"morning|afternoon|evening|night"}. '
           "event = a one-off occasion needing a few hours near the date; habit = repeated "
           "practice (exercise, language, diet, relationship time); project = sustained build/study. "
           "total_hours is the whole realistic effort, active_days how many separate days it needs. "
           "physical is true ONLY when the goal itself is exercise, sport or body training done in a "
           "gym or workout slot; false for everything else, including couple time, study, hobbies. "
           "per_week is how many sessions a week a habit sensibly needs (1-6). best_time is when this "
           "activity naturally happens in a person's day.")
    try:
        r = get_manager(db).generate(sys, f"Goal: {title}\nDeadline: {deadline}", timeout=25, max_tokens=80)
    except Exception:
        return None
    if not (r is not None and getattr(r, "ok", False) and r.text):
        return None
    m = re.search(r"\{.*?\}", r.text, re.S)
    try:
        b = json.loads(m.group(0)) if m else None
        if b and b.get("kind") in ("event", "habit", "project"):
            return {"kind": b["kind"], "total_hours": max(0.25, min(float(b.get("total_hours") or 0) or 0.25, 400)),
                    "active_days": max(1, min(int(b.get("active_days") or 1), 60)),
                    "physical": bool(b.get("physical")),
                    "per_week": max(1, min(int(b.get("per_week") or 3), 6)),
                    "best_time": b.get("best_time") if b.get("best_time") in ("morning", "afternoon", "evening", "night") else ""}
    except Exception:
        pass
    return None


def effort_profile(goal, days_left):
    brief = getattr(_state_brief, "b", None)
   
    if brief and brief["kind"] == "event":
        return {"kind": "event", "event_kind": "event", "budget_min": int(brief["total_hours"] * 60),
                "active_days": min(brief["active_days"], max(days_left + 1, 1))}
   
    if brief and brief["kind"] == "habit":
        if brief.get("physical"):
            return {"kind": "habit", "per_week": brief.get("per_week")}
        return {"kind": "recurring", "per_week": brief.get("per_week"), "best_time": brief.get("best_time")}
   
    if brief and brief["kind"] == "project" and _goal_kind(goal.get("title") or "") not in ("hookup", "romance"):
        return {"kind": "project"}
    title = (goal.get("title") or goal.get("display_title") or "")
    low = title.lower() + " "
    kind = _goal_kind(title)
 
    if kind == "hookup" or kind == "romance" or _is_event_goal(title):
        ek = "hookup" if kind == "hookup" else "romance" if kind == "romance" else "event"
        return {"kind": "event", "event_kind": ek, "budget_min": _EVENT_BUDGET_MIN[ek],
                "active_days": min(_EVENT_ACTIVE_DAYS if ek != "event" else 4, max(days_left + 1, 1))}
 
    if any(w in low for w in _HABIT_WORDS):
        return {"kind": "habit"}
 
    return {"kind": "project"}


def _thin_to_sessions(horizon, per_week):
    days = _SESSION_DAYS.get(max(2, min(int(per_week or 3), 5)), (0, 2, 4))
    return [(d, sl if date.fromisoformat(d).weekday() in days else []) for d, sl in horizon]


_state_routine = threading.local()


def set_routine(r):
    _state_routine.r = r


def get_routine():
    return getattr(_state_routine, "r", None)


fmt_min = planner.fmt


def _apply_event_shape(horizon, today, deadline, budget_min, active_days, lead_days=5):
    if not horizon:
        return horizon
    last_days = [d for d, sl in horizon if sl][-active_days:]
    keep = {d: sl for d, sl in horizon if d in set(last_days)}
    lead = None
  
    if len(horizon) > active_days + 3:
        cutoff = (date.fromisoformat(deadline) - timedelta(days=lead_days)).isoformat()
        cands = [(d, sl) for d, sl in horizon if d <= cutoff and d not in keep and sl]
        if cands:
            d, sl = cands[-1]
            lead = (d, [min(sl, key=lambda x: -(x[1] - x[0]))][:1])
    out = []
    spent = 0
   
    if lead:
        budget_min -= min(lead[1][0][1] - lead[1][0][0], 60)
   
    for d in reversed(sorted(keep)):
        slots = []
        for sl in sorted(keep[d], key=lambda x: x[0])[:3]:
            dur = sl[1] - sl[0]
            if spent + dur > budget_min:
                break
            slots.append(sl)
            spent += dur
        out.append((d, slots))
    out.sort()
   
    if lead:
        d, sl = lead
        dur = min(sl[0][1] - sl[0][0], 60)
        out.insert(0, (d, [(sl[0][0], sl[0][0] + dur)]))
    return [(d, sl) for d, sl in out if sl]


def habit_sessions_per_week(goal):
    low = ((goal.get("title") or "") + " " + (goal.get("display_title") or "")).lower()
  
    if any(w in low for w in ("marathon", "yoga", "walk", "stretch")):
        return 5
  
    if any(w in low for w in ("six pack", "6 pack", "abs", "muscle", "bulk", "gym", "lose weight", "weight loss", "fat loss", "fitness", "get fit")):
        return 4
  
    return 3


_SESSION_DAYS = {2: (1, 4), 3: (0, 2, 4), 4: (0, 1, 3, 4), 5: (0, 1, 2, 4, 5)}
SESSION_MAX_MIN = 60
SNACK_MIN = 5
SNACKS_PER_DAY = 2


def _habit_horizon(today, deadline, windows, now_min, others_for, meal_windows, detail_days=None, per_week=4):
    t0 = date.fromisoformat(today)
    end = date.fromisoformat(deadline)
    span = min((end - t0).days + 1, 121)
   
    if detail_days:
        span = min(span, detail_days)
    train = _SESSION_DAYS.get(per_week, _SESSION_DAYS[4])
    after = sorted(meal_windows, key=lambda w: w[1] % 1440)[1:3] if len(meal_windows) > 2 else sorted(meal_windows, key=lambda w: w[1] % 1440)[-2:]
    out = []
   
    for i in range(span):
        day = t0 + timedelta(days=i)
        d = day.isoformat()
        busy = list(others_for(d) or [])
        slots = []
        is_train = day.weekday() in train
     
        if is_train:
            for (ws, we) in windows[:1]:
                we = min(we, ws + SESSION_MAX_MIN)
                if we <= ws or (i == 0 and now_min is not None and ws < now_min + LEAD_TODAY_MIN):
                    continue
                if not any(ws < be and bs < we for bs, be in busy):
                    slots.append((ws, we))
      
        else:
            for (ms, me) in after[:SNACKS_PER_DAY]:
                a, b = me % 1440, me % 1440 + SNACK_MIN
                if i == 0 and now_min is not None and a < now_min + LEAD_TODAY_MIN:
                    continue
                if b <= 23 * 60 and not any(a < be and bs < b for bs, be in busy) \
                        and not any(a < we and ws < b for ws, we in windows):
                    slots.append((a, b))
        if slots:
            out.append((d, sorted(slots)))

    return out


LEAD_TODAY_MIN = 30

WEEKLY_AT = 56
MONTHLY_AT = 75
DETAIL_DAYS = 14
_PHASES = ((0.25, "Foundations"), (0.60, "Build"), (0.85, "Pressure test"), (1.01, "Final push"))


def scope_for(days_left):
    """'daily' | 'weekly' | 'monthly' from how far away the deadline is"""
    
    if days_left > MONTHLY_AT:
        return "monthly"
    
    if days_left > WEEKLY_AT:
        return "weekly"
    
    return "daily"


def detail_days_for(days_left):
    """How many days from today get hour-by-hour tasks (None = all of them)"""
    return None if scope_for(days_left) == "daily" else DETAIL_DAYS


def _phase_at(frac):
    for limit, name in _PHASES:
        if frac < limit:
            return name
    return _PHASES[-1][1]


def build_outline_periods(today, deadline, scope, slot_minutes_by_date):
    t0 = date.fromisoformat(today)
    end = date.fromisoformat(deadline)
    total = max((end - t0).days + 1, 1)
    step = 30 if scope == "monthly" else 7
    periods, cur = [], t0
    while cur <= end:
        last = min(cur + timedelta(days=step - 1), end)
        mins = sum(m for d, m in slot_minutes_by_date.items() if cur.isoformat() <= d <= last.isoformat())
        mid = ((cur - t0).days + (last - cur).days / 2) / total
        periods.append({
            "kind": "month" if scope == "monthly" else "week",
            "start": cur.isoformat(), "end": last.isoformat(),
            "phase": _phase_at(mid), "hours": round(mins / 60, 1), "objective": "",
        })
        cur = last + timedelta(days=1)
    return periods


def generate_outline_objectives(goal, periods, db=None):
    if not periods:
        return periods
    title = (goal.get("title") or goal.get("display_title") or "").strip()
    details = goal_details_summary(goal.get("details"))
    listing = "\n".join(
        f'{i}. {p["start"]} .. {p["end"]} - phase "{p["phase"]}" - {p["hours"]}h of real free time'
        for i, p in enumerate(periods)
    )
    prompt = (
        f"Goal: {title}. Deadline: {goal.get('deadline')}. {('Details: ' + details) if details else ''}\n"
        f"Write the roadmap: ONE concrete objective per period below, each a measurable outcome "
        f"reachable inside that period's hours (what is finished, how much, how it is tested - "
        f"e.g. 'Finish chapters 1-5, then score 70%+ on two past papers'). Max 18 words each. "
        f"Periods build on one another and end with the goal done by the deadline. "
        f"No planning/organising meta-tasks.\n{listing}\n"
        'Return ONLY a JSON array: [{"i":0,"objective":"..."}, ...]'
    )
    try:
        res = get_manager(db).generate(BASE_SYSTEM_PROMPT, prompt, timeout=90, max_tokens=900)
    except Exception as exc:
        logger.warning("outline call failed: %r", exc)
        return periods
    if not getattr(res, "ok", False):
        return periods
    try:
        m = re.search(r"\[.*\]", res.text, re.S)
        for row in json.loads(m.group(0)) if m else []:
            i = int(row.get("i"))
            obj = " ".join(str(row.get("objective") or "").split())[:160]
            if 0 <= i < len(periods) and len(obj.split()) >= 3:
                periods[i]["objective"] = obj
    except Exception as exc:
        logger.warning("outline parse failed: %r", exc)
    return periods


def _kind_rules(profile):
    kind = (profile or {}).get("kind")
    if kind == "event":
        return (
            "THIS IS A ONE-OFF EVENT, NOT A GRIND. The whole job is a few hours, so the slots are "
            "few and sit in the last days. Anything perishable or fresh (flowers, food, cake, "
            "ironing, hair/outfit) happens on the event day or the day before - never earlier, it "
            "would wilt or spoil. The single earliest slot is for lead-time items only (booking, "
            "ordering a non-perishable gift, asking/confirming). Name real, specific actions - "
            "what to buy and where, what to message, what to prepare - not 'plan the date'.\n"
        )
    if kind == "recurring":
        return (
            "THIS IS A RECURRING PERSONAL HABIT, NOT EXERCISE. Never turn it into gym sessions, sets, "
            "reps or protein targets. Each slot is one concrete, specific thing the person does for the "
            "goal itself, plus the small preparation that makes it happen. Keep it tasteful, warm and "
            "practical; never explicit. Vary the actions across sessions.\n"
        )
    
    if kind == "habit":
        return (
            "THIS IS A PHYSICAL HABIT. Long slots (30+ min) are the person's own gym/exercise window: "
            "name the session - muscle group, 4-6 exercises, sets x reps - and rotate groups so no "
            "muscle is hit two days running. A session never exceeds 60 minutes. Slots of 5 minutes are "
            "MOVEMENT SNACKS right after lunch or dinner on rest days only: one exact exercise and rep count "
            "(e.g. '25 push-ups'). The person is not living in the gym: never pad a slot, and never schedule "
            "a session where there is no slot. "
            "Add a one-line food target to the session slots when it serves the goal (protein, "
            "calories) - no lectures.\n"
        )
    return ""


def period_for(outline, day_iso):
    for p in (outline or {}).get("periods", []):
        if p["start"] <= day_iso <= p["end"]:
            return p
    return None


def _build_fallback_plan(goal, today=None, extra_blocked=None, busy_by_date=None, now_min=None,
                         detail_days=None, outline_out=None):
    today = today or date.today().isoformat()
    deadline = goal["deadline"]
    span_days = min(max(days_remaining(deadline, today), 0) + 1, 121)

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
    from core import routine as _rtm
    for c in constraints:
        if get_routine() and _rtm.weekdays_in(c):
            continue
        w = parse_time_window(c)
        if w:
            blocked.append(w)
    for w in (extra_blocked or []):
        if w:
            blocked.append(w)

    _r = get_routine()
    if _r:
        from core import routine as _routine_mod
        human_windows = _routine_mod.human_windows(_r)
    else:
        human_windows = parse_routine_windows(details) or _DEFAULT_HUMAN_WINDOWS
    for w in human_windows:
        if w:
            blocked.append(w)

    _alloc = get_allocation()
    _owned = _alloc is not None and goal.get("id") in _alloc
    if not _owned and _goal_kind(goal.get("title") or goal["display_title"]) == "hookup":
        return _hookup_arc(today, deadline, blocked, details,
                           goal.get("title") or goal["display_title"])

    _fixed_dated = {}
    if get_routine():
        from core import routine as _rt
        _fixed_dated = _rt.dated_busy(get_routine(), today, deadline)

    def _day_bounds(day_date=None):
        busy = []
        for (s, e) in list(blocked) + list(_fixed_dated.get(day_date, [])):
            s = s % (24 * 60)
            e = (e - s) % (24 * 60) + s
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

    def _free_from(busy, start, taken, span=90):
        obstacles = sorted(busy + list(taken))
        cur = start
        for (bs, be) in obstacles:
            if be <= cur:
                continue
            if bs >= cur + span:
                break
            cur = be
        if cur + span > 23 * 60:
            return None
        return (cur, cur + span)

    commit_min = daily_commit_min(details)

    wake, bed = 7 * 60, 23 * 60
    _r = get_routine()
    sleep_times = list(_r["sleep"]) if _r else parse_clock_list(details.get("sleep_schedule") or "")
    if len(sleep_times) >= 2:
        b0, w0 = sleep_times[0], sleep_times[1]
        if w0 > 13 * 60 and b0 < w0:
            b0, w0 = w0, b0
        nb = planner.normalize_sleep(b0, w0)
        if nb is not None:
            bed, wake = nb, w0
    prefs = planner.Prefs(
        wake=wake, bed=bed, commit_min=commit_min,
        chronotype=planner.infer_chronotype((_r or {}).get("peak") or details.get("peak_energy"), wake, bed),
    )
    others_for = (lambda d: list((busy_by_date or {}).get(d, [])))
    habit_others = (lambda d: others_for(d) + list(_fixed_dated.get(d, [])))
    profile = effort_profile(goal, max(days_remaining(deadline, today), 0))
    wins = workout_windows()
    if profile["kind"] == "recurring":
        prefs.commit_min = min(prefs.commit_min, 90)
        prefs.chronotype = {"morning": "morning", "afternoon": "afternoon", "evening": "night", "night": "night"}.get(profile.get("best_time"), prefs.chronotype)
    if _owned:
        mine = _alloc.get(goal["id"], {})
        start = date.fromisoformat(today)
        horizon = [((start + timedelta(days=k)).isoformat(),
                    list(mine.get((start + timedelta(days=k)).isoformat(), [])))
                   for k in range(span_days)]
    elif profile["kind"] == "habit" and wins:
        meals = [w for w in (human_windows or []) if w and (w[1] - w[0]) % 1440 <= 120 and 6 * 60 <= w[0] % 1440 <= 21 * 60]
        meals = [w for w in meals if w not in wins]
        horizon = _habit_horizon(today, deadline, wins, now_min, habit_others, meals, detail_days, profile.get("per_week") or habit_sessions_per_week(goal))
    else:
        horizon = planner.plan_horizon(
            today, deadline, prefs, _day_bounds, now_min=now_min, others_for=others_for,
            shaped=profile["kind"] != "recurring",
        )
        if profile["kind"] == "event":
            horizon = _apply_event_shape(horizon, today, deadline, profile["budget_min"], profile["active_days"])
        elif profile["kind"] == "recurring":
            horizon = _thin_to_sessions(horizon, profile.get("per_week"))
    _state.profile = profile
    if outline_out is not None:
        outline_out["slot_minutes"] = {d: sum(e - s0 for s0, e in sl) for d, sl in horizon}
    if detail_days:
        cut = (date.fromisoformat(today) + timedelta(days=detail_days - 1)).isoformat()
        horizon = [(d, sl) for d, sl in horizon if d <= cut]
    day_list = [d for d, _ in horizon]
    day_slots_map = {d: s for d, s in horizon}

    entries = []
    split_topics = None
    for key in ("syllabus", "topic", "domain", "requirements"):
        val = (details.get(key) or "").strip()
        if val:
            parts = [p.strip() for p in re.split(r"[,;]|\band\b|\.", val) if p.strip()]
            if len(parts) >= 2:
                split_topics = parts
                break
    full_title = goal.get("title") or goal["display_title"]
    is_event = _is_event_goal(full_title)

    packed = []
    for d in reversed(day_list):
        for (s, e) in reversed(day_slots_map.get(d, [])):
            packed.append((d, s, e))
    if is_event and split_topics and len(packed) >= len(split_topics):
        packed = packed[:len(split_topics)]
    packed.reverse()

    topic_seq = None
    if split_topics:
        if is_event and len(split_topics) <= len(packed):
            topic_seq = list(split_topics[:len(packed)])
        else:
            topic_seq = [split_topics[i % len(split_topics)] for i in range(len(packed))]

    for idx, (d, s, e) in enumerate(packed):
        day_i = day_list.index(d)
        title = topic_seq[idx] if topic_seq else f"{full_title} — day {day_i + 1}"
        entries.append({
            "date": d,
            "title": title,
            "start_time": f"{s // 60:02d}:{s % 60:02d}",
            "end_time": f"{e // 60:02d}:{e % 60:02d}",
            "duration_min": e - s,
            "status": "pending",
        })
    return entries


def _hookup_arc(today, deadline, blocked, details, goal_ref=None):
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
    n = min(endd.toordinal() - day0.toordinal() + 1, 3)
    dates = [endd - timedelta(days=n - 1 - i) for i in range(n)]

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
            b_ev = [w for w in b if not (w[1] - w[0] <= 90 and 12 * 60 <= w[0] < 22 * 60)]
            ev = fit(b_ev, taken, 18 * 60, 240) or fit(b_ev, taken, 18 * 60, 180) or fit(b_ev, taken, 19 * 60, 150)
            add(day, ev or (19 * 60, 22 * 60), "event", 1, i)
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


def _local_now(db, goal, user=None):
    """The goal owner's wall-clock 'now' (their timezone, not the server's)"""
    from core.clock import now_for
    if user is None and db is not None and goal and goal.get("user_id"):
        try:
            user = db.execute("SELECT timezone FROM users WHERE id=?", (goal["user_id"],)).fetchone()
        except Exception:
            user = None
    return now_for(user)


def generate_plan(goal, user, db=None, extra_blocked=None, prefer_cloud=False, busy_by_date=None):
    try:
        return _llm_plan_call(goal, db, extra_blocked=extra_blocked, prefer_cloud=prefer_cloud,
                              busy_by_date=busy_by_date, user=user)
    except Exception:
        return None


def generate_plan_or_none(goal, user, db=None, extra_blocked=None, prefer_cloud=False,
                          busy_by_date=None):
    try:
        return _llm_plan_call(goal, db, extra_blocked=extra_blocked, prefer_cloud=prefer_cloud,
                              busy_by_date=busy_by_date, user=user)
    except Exception as exc:
        logger.warning("plan call failed: %r", exc)
        return None


def _goal_kind(title):
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
    if db is None or not goal or not goal.get("id"):
        return ""
    uid = goal.get("user_id") or 0
    out = []
    try:
        rows = db.execute(
            "SELECT role, content FROM (SELECT id, role, content FROM chat_messages "
            "WHERE goal_id=? AND user_id=? ORDER BY id DESC LIMIT 40) ORDER BY id ASC",
            (goal["id"], uid),
        ).fetchall()
        out += [f"{r['role']}: {r['content']}".strip() for r in rows]
    except Exception:
        pass
    try:
        rows = db.execute(
            "SELECT content FROM (SELECT id, content FROM general_chat WHERE user_id=? AND role='user' "
            "ORDER BY id DESC LIMIT 15) ORDER BY id ASC", (uid,),
        ).fetchall()
        if rows:
            out.append("(from the main chat, use only what concerns this goal)")
            out += [f"user: {r['content']}".strip() for r in rows]
    except Exception:
        pass
    try:
        notes = json.loads(goal.get("details") or "{}").get("plan_notes") or []
        out += [f"REDRAW INSTRUCTION: {n}" for n in notes[-6:]]
    except Exception:
        pass
    return "\n".join(out)[-2500:]


def reorder_naturally(entries, title, db=None):
    ordered = sorted(entries, key=lambda e: (e["date"], e.get("start_time") or ""))
    if len(ordered) < 2 or len({e["date"] for e in ordered}) < 2:
        return entries
    listing = "\n".join(f"{i}. {e['date']} {e.get('start_time') or ''} - {e.get('title')}" for i, e in enumerate(ordered))
    sys = ("You sanity-check a schedule the way a sensible person would. Tasks keep their slots, "
           "but you may reorder which task sits in which slot so the sequence is realistic: "
           "things that spoil or go stale happen close to when they are used, prerequisites come "
           "before what depends on them, nothing is done too early to still matter or too late to help. "
           "Reply with ONLY a JSON array containing every index exactly once, in the new order. "
           "If the order is already realistic return it unchanged.")
    try:
        r = get_manager(db).generate(sys, f"Goal: {title}\nSlots in time order:\n{listing}", timeout=40, max_tokens=300)
    except Exception:
        return entries
    if not (r is not None and getattr(r, "ok", False) and r.text):
        return entries
    m = re.search(r"\[[\d,\s]*\]", r.text)
    try:
        order = json.loads(m.group(0)) if m else None
    except Exception:
        return entries
    if not order or sorted(order) != list(range(len(ordered))):
        return entries
    keys = [k for k in ("title", "description", "details") if any(k in e for e in ordered)]
    snap = [{k: e.get(k) for k in keys} for e in ordered]
    for slot, src in zip(ordered, order):
        for k in keys:
            slot[k] = snap[src][k]
    return entries


def _extract_links(text):
    if not text:
        return []
    out = []
    for m in re.finditer(r"https?://[^\s)\]]+", text):
        url = m.group(0).rstrip(".,;:")
        if url not in out:
            out.append(url)
    return out[:8]


def daily_commit_min(details):
    commit_min = 120
    tp = (details.get("time_per_day") or "").strip().lower()
    if re.search(r"\bhalf (an|a) hour\b", tp):
        return 30
    if re.search(r"\b(an|one) hour\b", tp) and not re.search(r"\d", tp):
        return 60
    m = re.search(r"(\d+(?:\.\d+)?)", tp)
    if m:
        n = float(m.group(1))
        unit = tp[m.end():m.end() + 3].strip()
        is_minutes = ("m" in unit and "h" not in unit) or n > 16
        if is_minutes:
            commit_min = max(15, min(int(n), 16 * 60))
        elif 1 <= n <= 16:
            commit_min = int(round(n * 60))
        elif 0 < n < 1:
            commit_min = max(15, int(round(n * 60)))
    return commit_min


# Time and size come from core.balance; the model only writes titles.
def too_small_ratio():
    n = getattr(_state, "slots_titled", 0)
    return (getattr(_state, "too_small", 0) / n) if n else 0.0


# A draw missing slots or titles must not overwrite the existing plan.
def last_plan_complete():
    total = getattr(_state, "slots_total", 0)
    return getattr(_state, "failed_slices", 0) == 0 and total > 0 and \
        getattr(_state, "slots_titled", 0) >= 0.85 * total


def _llm_plan_call(goal, db, timeout=180, extra_blocked=None, prefer_cloud=False, busy_by_date=None,
                   user=None):
    _state.last_error = None
    _state.slots_total = 0
    _state.slots_titled = 0
    _state.too_small = 0
    _state.failed_slices = 0
    sys = BASE_SYSTEM_PROMPT
    deadline = goal["deadline"]
    title = (goal.get("title") or goal["display_title"]).strip()
    cons = goal.get("constraints") or "[]"
    blocked_line = ""
    if extra_blocked:
        windows = ", ".join(
            f"{b[0] // 60:02d}:{b[0] % 60:02d}-{b[1] // 60:02d}:{b[1] % 60:02d}" for b in extra_blocked
        )
        blocked_line = (
            f"\nHARD BLOCKED WINDOWS — NEVER schedule ANY task inside these, on ANY day: "
            f"{windows}. They repeat every single day until the deadline. A task placed "
            "inside one of those windows is a failed plan, not a judgment call."
        )
    try:
        _details = json.loads(goal.get("details") or "{}")
    except Exception:
        _details = {}
    _rr = get_routine()
    if _rr:
        from core import routine as _routine_mod
        _routine = _routine_mod.human_windows(_rr)
    else:
        _routine = parse_routine_windows(_details)
    if _routine:
        _routine_txt = ", ".join(
            f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in _routine
        )
        human_line = (
            "\nHUMAN BASICS ARE MANDATORY, NOT OPTIONAL: the user's own routine blocks are "
            f"{_routine_txt}. NEVER schedule a task inside them. Do not write tasks like "
            "'skip lunch', 'work through dinner', 'stay up all night'. A person still eats, sleeps and rests while "
            "finishing this. If the deadline is tight, use the free waking hours — never "
            "steal them from food, sleep or health."
        )
    else:
        human_line = (
            "\nHUMAN BASICS ARE MANDATORY, NOT OPTIONAL: never schedule a task during sleep "
            "(23:00-07:00) or meals (breakfast 07:00-08:00, lunch 13:00-13:45, dinner 19:30-20:15). "
            "Do not write tasks like 'skip lunch', 'work through dinner', 'stay up all night'. "
            "A person still eats, sleeps and rests while finishing this. If the deadline "
            "is tight, use the free waking hours — never steal them from food, sleep or health."
        )
    think_line = (
        "\nTHINK ABOUT HOW TASKS DEPEND ON EACH OTHER BEFORE PLACING THEM:\n"
        "- Perishables spoil. Flowers, food, cake, groceries, ice, fresh produce, meat, dairy: "
        "buy them the SAME day they are used, or the morning of the event — never days earlier. "
        "Do not schedule a purchase on Monday for an event on Thursday.\n"
        "- Anything that must be followed by another step (buy -> use, print -> sign, "
        "book -> attend) must have the follow-up in the same slot, the same day, or the very "
        "next day at the latest. Never leave a stale gap.\n"
        "- For exams/study: schedule studying the ACTUAL syllabus content (solve past papers, "
        "work through chapter 4, write the lab report). NEVER schedule meta-work like "
        "'build a schedule', 'make a study plan', 'prepare study materials', 'gather notes', "
        "'create a study guide', 'collect resources', 'organize notes' — that is not studying "
        "and it is not the task the user asked for.\n"
        "- Every task must be finishable inside its own slot and must move the goal to done. "
        "Completion is the only thing that counts."
    )
    location_line = ""
    try:
        loc = user_location(db, goal.get("user_id")).strip()
    except Exception:
        loc = ""
    if loc:
        location_line = (
            f"\nUSER LOCATION: {loc}. Every service, app, shop, restaurant, delivery "
            "option, payment method, transport or venue a task depends on must actually "
            "exist and operate there — never suggest US-only options (DoorDash, Target, "
            "CVS, Venmo…) unless they truly exist in that country. Money in the local "
            "currency when natural."
        )
    detail_line = ""
    details_summary = goal_details_summary(goal.get("details"))
    if details_summary:
        detail_line = f"\nClarified answers from the user: {details_summary}"
    guidance = _plan_guidance(title, details_summary)
    guidance_line = f"\n{guidance}" if guidance else ""
    chat_log = _goal_chat_log(db, goal)
    chat_line = ""
    if chat_log:
        chat_line = (
            "\n=== CHAT NOTES (the user's own conversation on this file) ===\n"
            f"{chat_log}\n"
            "=== END CHAT NOTES ===\n"
            "These notes are instructions, not background. If the person asked you to build "
            "around something, to add, drop or change anything, the new plan MUST visibly do it "
            "(a REDRAW INSTRUCTION is the newest word and wins). If the user already said a task "
            "or a specific block/time is done or cancelled, DO NOT re-schedule it as pending. "
            "Plan forward from what is still outstanding. Match real dates/times the user "
            "mentioned. Do not invent new topics unrelated to the goal."
        )
    links = _extract_links(chat_log)
    links_line = ""
    if links:
        links_line = (
            "\n=== LINKS FROM THE USER'S CHAT NOTES ===\n"
            f"{', '.join(links)}\n"
            "=== END LINKS ===\n"
            "Use these exact links where the plan needs a resource, tool, form, or page: "
            "link the real thing the user already shared. Do not invent different resources."
        )
    local_now = _local_now(db, goal, user)
    today = local_now.date().isoformat()
    now_min = local_now.hour * 60 + local_now.minute
    days_left = max(days_remaining(deadline, today), 0)
    if days_left <= 2:
        pace = "deadline in the next few days — this is a GRIND: every task must directly finish the goal, and the day has room for many sessions, so pack them."
    elif days_left <= 7:
        pace = "under a week left — every task must directly move the goal to DONE in its slot; skip all non-essential prep and polish."
    elif days_left <= 30:
        pace = "a few weeks — steady push: highest-impact actions first, a real task per slot, no filler tasks between the real ones."
    else:
        pace = "a month or more — sustainable: one real action per day across the whole span; do NOT pad with busywork or planning-templates just to fill the calendar, and do NOT leave gaps."
    size_line = (
        f"\nRIGHT-SIZE THIS TO THE DEADLINE: {days_left} days from today — {pace}\n"
        "This rule applies to EVERY goal, whatever it is. Both of these are failures: "
        "contracting the schedule to fewer days than remain (every day from today to the "
        "deadline stays covered), and inflating it with generic filler days (write a plan, "
        "build a roadmap, draft a strategy, make a financial model, hold a planning session) "
        "unless the user's own notes literally call for it. Every task stays a real, physical "
        "action a normal person can do in that slot, that exact day."
    )
    scope = scope_for(days_left)
    ol = {}
    fallback = _build_fallback_plan(goal, today=today, extra_blocked=extra_blocked,
                                    busy_by_date=busy_by_date, now_min=now_min,
                                    detail_days=detail_days_for(days_left), outline_out=ol) or []
    _state.outline = None
    if fallback and scope != "daily":
        periods = build_outline_periods(today, deadline, scope, ol.get("slot_minutes", {}))
        periods = generate_outline_objectives(goal, periods, db)
        _state.outline = {"scope": scope, "periods": periods, "built": today}
    if not fallback:
        _state.plan_blocked_reason = (
            "your fixed windows, plus the goals that outrank this one, leave no usable time "
            "before the deadline. Push this deadline out or relax a window and I'll redraw."
        )
        return None
    _state.plan_blocked_reason = None

    n_slices = min(
        _PLAN_SLICE_MAX_CALLS,
        max(1, (len(fallback) + _PLAN_SLICE_TARGET_SLOTS - 1) // _PLAN_SLICE_TARGET_SLOTS),
    )
    slice_size = (len(fallback) + n_slices - 1) // n_slices

    def _slot_json(e):
        return (
            f'  {{"date": "{e["date"]}", "start_time": "{(e["start_time"] or "09:00")}", '
            f'"end_time": "{(e["end_time"] or "10:30")}"}}'
        )

    base_head = (
        f"Build the day-by-day plan from {today} to {deadline} for: {title}\n"
        f"THE GOAL IS: {title}\n"
        f"Constraints: {cons}"
        f"{blocked_line}"
        f"{human_line}"
        f"{think_line}"
        f"{location_line}"
        f"{chat_line}"
        f"{links_line}"
        f"{detail_line}"
        f"{guidance_line}"
        f"{size_line}\n"
        "Together, the separate requests for this window cover every day in the detailed "
        "window — never drop, merge, shift, or cut the slots you were given, and never add "
        "slots that were not listed.\n"
        + _kind_rules(getattr(_state, "profile", None)) +
        "TASK WORDING STANDARD (this is what makes a plan usable): every title must name "
        "(1) the exact material or deliverable, (2) a quantity or size, and (3) the "
        "finish-test that proves it is done. Good: 'Solve 10 subnetting problems from "
        "Professor Messer's sheet; redo every miss until 10/10'. Good: 'Build the pricing "
        "page in Figma: 3 tiers, mobile + desktop, export 2 PNGs'. Bad: 'Study networking', "
        "'Work on the project', 'Continue practice'. Each task builds on the previous ones: "
        "later days go deeper or harder, never repeat an earlier title.\n"
    )
    out = []
    for i in range(0, len(fallback), slice_size):
        chunk = fallback[i:i + slice_size]
        first, last = chunk[0]["date"], chunk[-1]["date"]
        slots_json = "[\n" + ",\n".join(_slot_json(e) for e in chunk) + "\n]"
        ctx = ""
        per = period_for(getattr(_state, "outline", None), first)
        if per:
            ctx += (f"ROADMAP: this stretch is the \"{per['phase']}\" phase "
                    f"({per['start']}..{per['end']}, {per['hours']}h available)."
                    + (f" Target for it: {per['objective']}." if per['objective'] else "") + "\n")
        else:
            total = max(days_remaining(deadline, today), 1)
            ctx += f"PHASE: {_phase_at(max(0, days_between(today, first)) / total)}.\n"
        if out:
            ctx += "ALREADY PLANNED EARLIER (go deeper, do not repeat): " + " | ".join(e["title"] for e in out[-6:]) + "\n"
        base_head_slice = base_head + ctx
        user_prompt = (
            f"{base_head_slice}This request covers ONLY {first} .. {last}. Other requests fill the rest of "
            "the calendar — do NOT plan any day outside this range, and do NOT move any "        
            "slot to a different date, start_time, or end_time than the ones below.\n"
            "Below are the EXACT day/time slots in THIS range the plan must fill (each slot "
            "is one task; EVERY slot listed must appear in your output, in the same order, "
            "one object per slot). Their count is already correct — it follows the deadline, "
            "how much of that day the person is actually free, and how heavy the goal is: a "
            "fully free day on a pressing deadline can hold 8+ sessions (fill all of them — "
            "the person has the time), while an easy goal with a far deadline gets as few as "
            "one. Never drop, merge, shift, or cut slots, and never add extras.\n"
            "DO NOT change any slot's date or start_time or end_time. Return your titles:\n"
            f"{slots_json}\n"
            "Return ONLY a JSON array with ONE object per slot, in the same order: "
            '{"date":"YYYY-MM-DD","start_time":"HH:MM","end_time":"HH:MM","title":"..."}. '
            'If the real task cannot sensibly fit its slot (too short for the work), add "too_small":true '
            "to that object and still title the slot with the most useful piece that does fit. "
            "Each title tells the user EXACTLY WHAT TO DO in that exact slot, that exact "
            "day: a verb + the real thing (what to buy, what to set up, what to text, what "
            "time, what to say). "
            "UP TO TWO SHORT LINES per title (at most ~24 words / 150 characters total) — it must fit the "
            "slot's own timeframe and it is still a complete, concrete instruction. E.g. "
            "instead of 'Ensure a smooth evening' write 'Pick the three-piece, text her, "
            "agree 8pm. Candles, slow music, door at 8.' Pull "
            "the specifics from the chat notes and clarified answers (the item already "
            "chosen, the agreed time, the place, who's coming) and match the goal text; "
            "inventing unrelated topics is a hard failure. "
            "Forbidden ANYWHERE in a title (they are topics, not actions): ensure, confirm, "
            "consider, review, research, prepare, discuss, debate, decide, brainstorm, "
            "'make sure', 'plan the'. 'Ensure aftercare and safety' is REJECTED. "
            "Each slot gets a different, specific task. Never schedule the same conversation "
            "topic on separate days. Do not schedule anything the chat notes say is already "
            "done. "
            "If a slot is on an evening and the next day is free, that slot is THE EVENT "
            "NIGHT itself — name what the night actually is, using the chat notes' decisions "
            "(people, chosen items, agreed times). Every title is a physical, concrete action "
            "the user does in that slot, not a lecture and not a vibe."
        )
        res = None
        try:
            res = get_manager(db).generate(
                sys, user_prompt, timeout=timeout, prefer_cloud=prefer_cloud,
                max_tokens=_MAX_PLAN_TOKENS,
            )
        except Exception as exc:
            logger.warning("plan slice %s-%s failed: %r", first, last, exc)
            _state.failed_slices += 1
            _state.slots_total += len(chunk)
            continue
        if not res.ok and chat_line:
            logger.warning("plan slice %s-%s: model error %s, retrying lean", first, last, res.error)
            try:
                res = get_manager(db).generate(
                    sys, user_prompt.replace(chat_line, "").replace(links_line, ""), timeout=timeout,
                    prefer_cloud=prefer_cloud, max_tokens=_MAX_PLAN_TOKENS)
            except Exception as exc:
                logger.warning("plan slice %s-%s lean retry failed: %r", first, last, exc)
                _state.failed_slices += 1
                _state.slots_total += len(chunk)
                continue
        if not res.ok:
            _state.last_error = (res.error or "no reply")[:160]
            logger.warning("plan slice %s-%s: model error %s", first, last, res.error)
            _state.failed_slices += 1
            _state.slots_total += len(chunk)
            continue
        _state.last_provider = res.provider or "llm"
        title_map = _parse_title_map(res.text)
        missing = [e for e in chunk if not _title_ok(title_map.get((e["date"], e["start_time"])), goal_ref=title)]
        if missing:
            try:
                sub_json = "[\n" + ",\n".join(_slot_json(e) for e in missing) + "\n]"
                res2 = get_manager(db).generate(
                    sys, user_prompt.replace(slots_json, sub_json) +
                    "\nTitles for these slots were missing or rejected. Start each with a concrete verb "
                    "(Solve, Write, Build, Call, Buy, Run, Draft...), name the exact material, give a quantity "
                    "and a finish-test.", timeout=timeout, prefer_cloud=prefer_cloud, max_tokens=_MAX_PLAN_TOKENS)
                if res2 is not None and getattr(res2, "ok", False):
                    wanted = {(e["date"], e["start_time"]) for e in missing}
                    title_map.update({k: v for k, v in _parse_title_map(res2.text).items() if k in wanted})
            except Exception as exc:
                logger.warning("plan slice %s-%s re-ask failed: %r", first, last, exc)
        _state.slots_total += len(chunk)
        for e in chunk:
            t = title_map.get((e["date"], e["start_time"]))
            if not _title_ok(t, goal_ref=title):
                continue
            _state.slots_titled += 1
            out.append({**e, "title": t})
    return out or None


_WEAK_STARTERS = {
    "ensure", "confirm", "consider", "review", "research", "prepare", "prep",
    "discuss", "debate", "brainstorm", "decide", "plan", "think", "brain-storm",
}


def _title_ok(title, goal_ref=None):
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
    if any(re.search(rf"\b{re.escape(v)}\b", low) for v in (
            "research", "polish", "prepare", "discuss the plan", "work on it",
            "brainstorm topics", "review the plan", "review the design")):
        return False
    if re.search(r"\bday\s+\d+\b.{0,24}\bpart\s+\d+\b", low):
        return False
    if re.search(r"\bpart\s+\d+\b.{0,24}\bday\s+\d+\b", low):
        return False
    if goal_ref and stripped.lower().rstrip(".").strip() == goal_ref.strip().lower():
        return False
    if goal_ref and re.match(re.escape(goal_ref.strip().lower()) + r"\s*[—–\-]?\s*(day|part)", low):
        return False
    if any(v in low for v in ("start the next phase", "move to the next phase",
                              "go to the next phase", "next phase", "make progress on")):
        return False
    if any(p in low for p in (
            "study materials", "study guide", "study plan", "study timetable",
            "gather resources", "collect resources", "gather materials",
            "build a schedule", "make a schedule", "create a schedule",
            "make a timetable", "create a timetable", "build a timetable",
            "organize notes", "organise notes", "compile notes")):
        return False
    first = re.split(r"[\s:]+", stripped, maxsplit=1)[0].lower().rstrip(".")
    if first in _WEAK_STARTERS:
        return False
    return True


def _parse_title_map(text):
    def _add(item):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except Exception:
                return
        if not isinstance(item, dict):
            return
        d = item.get("date")
        t = item.get("title")
        if not d or not t:
            return
        st = normalize_time(item.get("start_time") or item.get("start")) or ""
        if st:
            out[(d, st)] = str(t).strip()[:200]
            if item.get("too_small") is True:
                _state.too_small = getattr(_state, "too_small", 0) + 1

    out = {}
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = None
        if isinstance(data, list):
            for item in data:
                _add(item)
            return out
    for item in re.findall(r"\{[^{}]*\}", text):
        _add(item)
    return out


def completion_detected(message):
    text = (message or "").strip()
    if not text or len(text.split()) > 16:
        return False
    lowered = " " + re.sub(r"[^a-z0-9\s]", " ", text.lower()) + " "
    if "?" in text:
        return False
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


def progress_stats(goal, actions, today=None):
    total_planned = sum((a.get("duration_min") or 60) for a in actions) / 60.0
    completed = sum((a.get("duration_min") or 60) for a in actions if a.get("status") == "done") / 60.0
    missed = sum((a.get("duration_min") or 60) for a in actions if a.get("status") == "missed") / 60.0
    total_days = 1
    today = today if isinstance(today, date) else date.today()
    ddiff = days_remaining(goal.get("deadline", today.isoformat()), today.isoformat())
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


def pressure_rating(goal, actions, open_blockers=0, missed_days=0, today=None):
    today_iso = (today.isoformat() if isinstance(today, date) else date.today().isoformat())
    dl = days_remaining(goal.get("deadline", today_iso), today_iso)
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
        "everything else is cancelled" if score >= 8 else
        "lights are on" if score >= 6 else
        "manageable" if score >= 4 else
        "coasting"
    )
    return {"score": score, "label": label, "days_left": dl, "remaining_hours": round(remaining, 1)}


def momentum_stats(user_id, db=None, today=None):
    from core.database import get_connection
    from core.clock import today_for
    conn = db or get_connection()
    if not isinstance(today, date):
        try:
            today = today_for(conn.execute("SELECT timezone FROM users WHERE id=?", (user_id,)).fetchone())
        except Exception:
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
    current = 0
    probe = today
    if probe not in done_days:
        probe = today - timedelta(days=1)
    while probe in done_days:
        current += 1
        probe -= timedelta(days=1)
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
    """Map an Eloise reply to a display mood for the console"""
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