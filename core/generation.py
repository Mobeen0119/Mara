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

# A chat reply is short by contract (at most 2 lines). Capping the model's
# output turns that contract into a hard guarantee AND makes replies fast
# (fewer generated tokens, especially on the CPU-bound local model). 120 cut
# replies off mid-sentence (which read as broken/repetitive); 400 finishes
# any honest 2-4 line answer without letting a rambler run.
_MAX_CHAT_TOKENS = 400

# Plan draws have their own output ceiling so a slow CPU box FINISHES instead
# of timing out mid-JSON. A single request CANNOT carry a whole month (60+
# slots blow the budget, the model truncates mid-JSON, the parse fails, and
# the entire plan collapses to empty + infinite retries). So one draw is split
# into small day-bucketed slices — each request is short enough for a
# slow/weak/free model to finish, and a failed slice only costs itself, never
# the whole schedule.
_MAX_PLAN_TOKENS = 1536
_PLAN_SLICE_TARGET_SLOTS = 8
_PLAN_SLICE_MAX_CALLS = 8

# Chat can be routed to a FASTER local model (e.g. a 1.7b instead of the 4b
# used for plans) so conversations feel instant while background plan draws
# keep the bigger model's quality. Set
# OLLAMA_CHAT_MODEL=huihui_ai/qwen3-abliterated:1.7b in .env. Empty/None =
# chat uses the same model as plans.
def _chat_model_override():
    try:
        return os.environ.get("OLLAMA_CHAT_MODEL") or None
    except Exception:
        return None


def _chat_lock_wait():
    """How long an interactive chat waits for its local CHAT model lock before giving
    up. Chat (a small fast model) holds its OWN per-model lock, so it no longer queues
    behind plan draws on the big model — a contended lock now means another chat is
    mid-reply, worth waiting for. Past the cap, and only then, it may fall back."""
    return 30

# Which provider drew the most recent plan (set by _llm_plan_call /
# _chat_llm), so the UI can honestly report "drawn by local / drawn by
# openrouter".
_state = threading.local()   # per-thread: concurrent users must never see each other's value

# When the user's own blocked windows consume every usable slot in a day, the
# honest truth is NOT "the model is down" — it's "your windows leave no free
# time", and the schedule must say exactly that instead of retrying a model
# that isn't the problem.
def plan_blocked_reason():
    return getattr(_state, "plan_blocked_reason", None)


def last_outline():
    return getattr(_state, "outline", None)


def last_provider():
    return getattr(_state, "last_provider", None)


def get_manager(db=None):
    global _manager
    if _manager is None:
        _manager = LLMManager()
    return _manager


# ---------------------------------------------------------------------------
# Personality contract
# ---------------------------------------------------------------------------
# The user can teach Eloise her personality in plain chat ("be sarcastic",
# "don't be a motivational speaker", "act like you'd abuse me if you could").
# Whoever says it ONCE expects it to stick forever, but a weak model forgets
# it next turn. So we sniff these directives out of chat messages, store them
# on the user, and inject them into EVERY system prompt as a standing
# override.
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
    """If the user just set Eloise's personality in plain chat, store it as a
    standing contract. Returns the (possibly new) stored persona, else ''."""
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
    """The user's country/city — used to keep suggestions to services that
    actually exist where they live (no DoorDash in Pakistan)."""
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


# ---------------------------------------------------------------------------
# Step completion vs goal completion
# ---------------------------------------------------------------------------
# The user reports finishing the CURRENT STEP ("done", "finished the layout")
# far more often than the WHOLE goal. The plan chat must treat those as
# different: a step-done means "name the NEXT task from the plan, do not
# re-instruct the one just finished". Closing the entire goal must stay rare
# and require real evidence.
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
    """True when the user implies the CURRENT task/step is finished and wants
    forward motion. Wider than goal completion on purpose: it only steers the
    prompt (name the NEXT task), it never writes to the DB."""
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
    """Auto-closing a whole goal is destructive, so it needs BOTH a completion
    phrase AND a goal-level framing. Bare step-done chatter ('done', 'i'm done')
    must never close the file."""
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
    """Call the real model with NO canned fallback. If the model can't be reached,
    returns ("", "offline") — the caller surfaces that as a genuine error, never as
    fabricated Eloise text. The user explicitly wants no hardcoded replies."""
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


# --- Content-safety guardrail
# ------------------------------------------------- Precise, not paranoid:
# Eloise plans REAL goals — including romantic / intimate ones with a partner
# (gf / wife / partner), and she answers sexual-wellness questions plainly.
# She refuses only sexual content involving a family member or a minor, and
# coercion / violence. Word-BOUNDARY matching only: substring scanning used to
# refuse "document my progress" ('cum' in document), "every person" ('son' in
# person), "choose between" ('teen' in between) and "grow grapes" ('rape' in
# grapes). Thread-aware so follow-ups like "sister" after an incest message are
# still refused.

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
    """Detect requests Eloise must refuse. Returns True for:
    - sexual content involving a family member or minor (thread-aware),
    - coercion / non-consent or real-world violence.
    Everything else — partner intimacy, masturbation questions, crude language,
    non-sexual family goals — is allowed. Returns (no tuple, just bool)."""
    t = (msg or "").lower()
    # Only the USER's own words carry intent. Eloise's replies must never be
    # scanned: her refusal text contains "minor"/"family member", and plan
    # lines quote goal titles — scanning them makes benign follow-ups look
    # like abuse and re-refuse forever.
    user_lines = [
        ln for ln in (context or "").split("\n")
        if not ln.strip().lower().startswith(("eloise:", "assistant:"))
    ]
    # Whole thread matters: "sister" or "consent" alone are harmless, but as a
    # follow-up to "i want to fuck my sister" they continue the same abuse
    # goal.
    full = ("\n".join(user_lines) + "\n" + t).lower()
    has_partner = bool(_PARTNER_RE.search(full)) or _has_phrase(full, _PARTNER_PHRASES)
    has_family = bool(_FAMILY_RE.search(full))
    has_minor = bool(_MINOR_RE.search(full))
    has_sex = bool(_SEXUAL_RE.search(t)) or _has_phrase(t, _SEXUAL_PHRASES) \
        or bool(_SEXUAL_RE.search(full)) or _has_phrase(full, _SEXUAL_PHRASES)

    # 1) Coercion / non-consent / violence in this message — always refuse.
    if _COERCION_RE.search(t) or _has_phrase(t, _COERCION_PHRASES):
        return True
    if _VIOLENCE_RE.search(t) or _has_phrase(t, _VIOLENCE_PHRASES):
        return True

    # 2) Any sexual content involving a minor — refuse.
    if has_minor and has_sex:
        return True

    # 3) Family + sexual in the thread, NOT a partner relationship — refuse.
    # A partner relationship clears it (girlfriend/wife are consenting adults).
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
        f"reason and number (e.g. 'Fuck the gym this week - the exam is 5 days out and 11 hours "
        f"are still open.'). Never hedge it with 'maybe' or 'you might consider'. If there is no "
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
    """Buffer a chat stream, strip any prompt echo / role-label / verbatim-question
    repetition a weak local model produces, and re-emit the cleaned reply ONCE, then a
    terminal ('', source) so the caller knows which provider answered. Yields ONLY the
    terminal when the model merely echoed — the route reports an honest 'offline'
    instead of printing the user's own words back at them."""
    buf = []
    source = None
    try:
        for chunk, src in stream_iter:
            if src and src != "offline":
                source = src
            if chunk:
                buf.append(chunk)
            elif src is not None:
                break  # '' marks the stream's clean completion
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
    """Same as generate_global_chat_reply but streams text chunks (echo-cleaned)."""
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
    """Same as generate_chat_reply but streams text chunks (echo-cleaned). Yields
    (chunk, source)."""
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
    """Pull the user's OWN prior lines out of the formatted history string so the
    stream cleaner can strip the model re-quoting them verbatim."""
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
    """Parse a window like '11pm-7am', '3pm-5pm', '5-7pm', '9:00-12:00', 'gym 5-7pm',
    'uni 6 to 2 pm' or 'work 9 to 5'.
    Returns (start_min, end_min) in minutes from midnight, handling overnight.
    The old parser applied the last explicit am/pm to ALL bare numbers before it,
    so 'uni 6 to 2 pm' became 6pm-2am (1080, 2280) instead of 6am-2pm (360, 840),
    and a window with no am/pm at all ('work 9 to 5') was silently dropped — both
    corrupt the user's 'always busy' silhouette and starve every schedule grid."""
    text = text or ""
    low = text.lower()
    m = re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", low)
    if len(m) < 2:
        return None
    entries = [(int(n), int(mm) if mm else 0, mer) for n, mm, mer in m]
    n = len(entries)
    has_any = any(mer for _, _, mer in entries)

    if not has_any:
        # No am/pm anywhere. Prefer a same-meridian block ("9:00-9:30",
        # "9:00-12:00") and only read it as start-am/end-pm when that would be
        # invalid backwards ("9 to 5" -> 9am-5pm, not 9am-5am).
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
    # Bare numbers inherit the nearest explicit meridian to their RIGHT, else
    # LEFT.
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
    # "X to Y pm" with a bare leading hour >= the trailing pm hour reads as X
    # am - Y pm ("uni 6 to 2 pm" = 6am-2pm), unless X is noon ("12-2pm").
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
    """Pull every clock time out of a string into minutes from midnight.
    '23:30 - 07:00' -> [1410, 420]; '08:00, 13:00, 20:00' -> [480, 780, 1200]."""
    out = []
    for hh, mm, mer in re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", (text or "").lower()):
        h, m = int(hh), int(mm) if mm else 0
        if mer:
            h = _hh_12_to_24(h, mer)
        if 0 <= h < 24 and 0 <= m < 60:
            out.append(h * 60 + m)
    return out


_ROUTINE_QUESTIONS = [
    {"key": "sleep_schedule", "q": "When do you usually sleep, and when do you wake up?",
     "hint": "e.g. 23:30 – 07:00"},
    {"key": "meal_times", "q": "When do you usually eat — breakfast, lunch, dinner?",
     "hint": "e.g. 08:00, 13:00, 20:00"},
    {"key": "movement_time", "q": "When do you usually move, gym or play?",
     "hint": "e.g. 18:00 (or type 'none')"},
    {"key": "peak_energy", "q": "When is your head clearest — morning, afternoon or night?",
     "hint": "e.g. morning"},
]


def _merge_routine(questions):
    """Always ask the basics — sleep, meals, movement — so the schedule is built
    around a real human day instead of 8am-to-11pm straight work."""
    seen = {q.get("key") for q in questions}
    out = list(questions)
    for rq in _ROUTINE_QUESTIONS:
        if rq["key"] not in seen:
            out.append(dict(rq))
    return out[:7]


def parse_routine_windows(details):
    """Turn the user's sleep / meal / movement answers into busy windows (minutes)."""
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


def generate_clarify_questions(title, db=None):
    """Ask the MODEL for 2-4 clarifying questions written for THIS specific goal
    (not the hardcoded category sets). Falls back to the built-in sets when no
    LLM can serve clean JSON in time, so goal creation never blocks on it."""
    sys = (
        "You are Eloise, a sharp scheduling chief. The user just filed a goal. Write "
        "3 short clarifying questions whose answers would let you build a real, "
        "concrete day-by-day schedule for exactly this goal. Cover: the concrete "
        "outcome (what 'done' looks like), the biggest constraint or blocker, and "
        "the specifics only they know (people involved, budget, tools, hours "
        "available, their country). Be direct and slightly dry — no fluff, no "
        "therapist voice. One line per question."
    )
    user_prompt = (
        f"Goal filed: {title}\n"
        "Return ONLY a JSON array of exactly 3 objects, each like "
        '{"key": "short_key", "q": "the question", "hint": "tiny example answer"}. '
        "No markdown, no commentary, nothing outside the array."
    )
    try:
        result = get_manager(db).generate(sys, user_prompt, timeout=30, max_tokens=250)
    except Exception:
        result = None
    if result is not None and getattr(result, "ok", False) and result.text:
        m = re.search(r"\[.*\]", result.text, re.S)
        if m:
            try:
                raw = json.loads(m.group(0))
                qs = []
                for i, item in enumerate(raw[:4]):
                    if not isinstance(item, dict):
                        continue
                    q = str(item.get("q") or "").strip()
                    if not q:
                        continue
                    key = re.sub(r"[^a-z0-9_]", "_", str(item.get("key") or f"q{i + 1}").lower()).strip("_")[:32]
                    qs.append({
                        "key": key or f"q{i + 1}",
                        "q": q[:220],
                        "hint": str(item.get("hint") or "").strip()[:140],
                    })
                if qs:
                    return _merge_routine(qs)
            except Exception:
                pass
    return _merge_routine(goal_questionnaire(title))


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


def user_calendar_busy(db, user_id, exclude_goal_id, start_date, end_date):
    """Map date -> [(start_min, end_min), ...] of this user's OTHER goals' pending
    actions in [start_date, end_date]. Lets a plan's slot grid avoid the user's real
    calendar (other goals already occupying time), not just declared busy windows."""
    busy = {}
    if db is None:
        return busy
    try:
        rows = db.execute(
            "SELECT date, start_time, end_time FROM actions "
            "WHERE user_id=? AND goal_id!=? AND status!='done' "
            "AND date>=? AND date<=?",
            (user_id, exclude_goal_id, start_date, end_date),
        ).fetchall()
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


_PERISHABLE_WORDS = (
    "flower", "bouquet", "cake", "grocer", "produce", "fresh", "meat",
    "milk", "dairy", "fruit", "vegetable", "ice cream", "ingredient",
    "dessert", "seafood", "bakery", "salad",
)
_EVENT_WORDS = (
    "date", "night", "dinner", "party", "wedding", "anniversary",
    "birthday", "gift", "surprise", "proposal", "valentine", "celebration",
    "getaway", "romantic", "hookup", "intimate",
)


def _is_perishable(text):
    low = (text or "").lower()
    return any(w in low for w in _PERISHABLE_WORDS)


# Tasks that only make sense on the day itself (cooking, serving, decorating,
# getting ready, picking up a fresh order). Treated like perishables when
# ordering an event arc so they land on the event day, not days early.
_SAME_DAY_WORDS = (
    "cook", "serve", "bake", "roast", "grill", "fry", "assemble", "decorate",
    "set up", "setup", "get ready", "pick up", "pickup", "collect", "deliver",
    "dress", "hair", "makeup", "thaw", "warm", "reheat", "plate", "chill",
)


def _is_same_day(text):
    low = (text or "").lower()
    return _is_perishable(text) or any(w in low for w in _SAME_DAY_WORDS)


def _is_event_goal(text):
    low = (text or "").lower()
    return any(w in low for w in _EVENT_WORDS)




# ---------------------------------------------------------------------------
# Effort model. A goal is not "fill every free hour until the deadline".
#   event   - one-off with a date (date night, party, proposal): the work is a few
#             hours, done in the LAST days; perishables (flowers, food) land on the
#             day; one early slot handles anything with lead time (bookings, orders).
#   habit   - a practice (gym, six-pack, run, diet): sessions live INSIDE the
#             user's own window for it (their 'gym 5-6'), plus short movement
#             snacks after meals - so the routine they already have is the plan.
#   project - everything else: the horizon planner above.
# ---------------------------------------------------------------------------
_HABIT_WORDS = ("gym", "workout", "work out", "six pack", "six-pack", "6 pack", "6-pack", "abs",
                "fitness", "get fit", "lose weight", "weight loss", "fat loss", "muscle", "bulk",
                "cut ", "run ", "running", "marathon", "yoga", "diet", "walk daily", "stretch",
                "pushup", "push-up", "pull-up", "body")
_WORKOUT_LABELS = ("gym", "workout", "work out", "run", "jog", "yoga", "swim", "training", "exercise", "sport")
_EVENT_BUDGET_MIN = {"hookup": 360, "romance": 300, "event": 420}
_EVENT_ACTIVE_DAYS = 3
_state_workout = threading.local()


def set_workout_windows(windows):
    """[(start_min, end_min), ...] - the user's own gym/exercise windows."""
    _state_workout.windows = list(windows or [])


def workout_windows():
    return list(getattr(_state_workout, "windows", []) or [])


def is_workout_label(text):
    low = (text or "").lower()
    return any(re.search(rf"\b{re.escape(w)}", low) for w in _WORKOUT_LABELS)


def effort_profile(goal, days_left):
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


def _apply_event_shape(horizon, today, deadline, budget_min, active_days, lead_days=5):
    """Keep only the final `active_days`, plus ONE early lead-time slot, within a time budget."""
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
    if lead:                                              # the lead slot is part of the budget
        budget_min -= min(lead[1][0][1] - lead[1][0][0], 60)
    for d in reversed(sorted(keep)):                      # nearest the event first
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


def _habit_horizon(today, deadline, windows, now_min, others_for, meal_windows, detail_days=None):
    """Sessions inside the user's workout windows (one per day, a rest day every 7th),
    plus a 5-minute movement snack right after each meal."""
    t0 = date.fromisoformat(today)
    end = date.fromisoformat(deadline)
    span = min((end - t0).days + 1, 121)
    if detail_days:
        span = min(span, detail_days)
    out = []
    for i in range(span):
        d = (t0 + timedelta(days=i)).isoformat()
        busy = list(others_for(d) or [])
        slots = []
        rest = (i % 7) == 6
        if not rest:
            for (ws, we) in windows[:1]:
                if we <= ws:
                    continue
                if i == 0 and now_min is not None and ws < now_min + LEAD_TODAY_MIN:
                    continue
                if not any(ws < be and bs < we for bs, be in busy):
                    slots.append((ws, we))
        for (ms, me) in meal_windows:
            a, b = me % 1440, me % 1440 + 5
            if i == 0 and now_min is not None and a < now_min + LEAD_TODAY_MIN:
                continue
            if b <= 23 * 60 and not any(a < be and bs < b for bs, be in busy) \
                    and not any(a < we and ws < b for ws, we in windows):
                slots.append((a, b))
        if slots:
            out.append((d, sorted(slots)))
    return out


LEAD_TODAY_MIN = 30

# ---------------------------------------------------------------------------
# Horizon-aware planning. Nobody plans day-by-day 4 months out: far goals get a
# monthly roadmap, mid-range goals a weekly one, and only the near days are
# drawn hour-by-hour. The detailed window rolls forward every day.
# ---------------------------------------------------------------------------
WEEKLY_AT = 56        # > 8 weeks out  -> weekly outline
MONTHLY_AT = 75       # > ~2.5 months  -> monthly outline
DETAIL_DAYS = 14      # hour-by-hour days kept ahead in weekly/monthly mode
_PHASES = ((0.25, "Foundations"), (0.60, "Build"), (0.85, "Pressure test"), (1.01, "Final push"))


def scope_for(days_left):
    """'daily' | 'weekly' | 'monthly' from how far away the deadline is."""
    if days_left > MONTHLY_AT:
        return "monthly"
    if days_left > WEEKLY_AT:
        return "weekly"
    return "daily"


def detail_days_for(days_left):
    """How many days from today get hour-by-hour tasks (None = all of them)."""
    return None if scope_for(days_left) == "daily" else DETAIL_DAYS


def _phase_at(frac):
    for limit, name in _PHASES:
        if frac < limit:
            return name
    return _PHASES[-1][1]


def build_outline_periods(today, deadline, scope, slot_minutes_by_date):
    """Deterministic roadmap skeleton: periods with a phase and a REAL hours budget
    (the sum of the free slots the planner actually found in that period)."""
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
    """One short model call fills each period's concrete objective. Failure leaves
    the objective empty; the phase + real hours budget still show. Nothing canned."""
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
    if kind == "habit":
        return (
            "THIS IS A PHYSICAL HABIT. Long slots (30+ min) are the person's own gym/exercise window: "
            "name the session - muscle group, 4-6 exercises, sets x reps - and rotate groups so no "
            "muscle is hit two days running. Slots of 10 minutes or less are MOVEMENT SNACKS right "
            "after a meal: one exact exercise and rep count (e.g. '25 push-ups, then 30s plank'). "
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
    # Cover every day from today THROUGH the deadline (inclusive). Using only
    # the raw gap silently dropped the deadline day itself — a "1 day left"
    # goal planned today and forgot tomorrow, which is the day that matters.
    span_days = min(max(days_remaining(deadline, today), 0) + 1, 121)

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

    # Sleep, meals and movement are not negotiable. Build around the times the
    # user told us when filing; fall back to a sane default day if they skipped.
    human_windows = parse_routine_windows(details) or _DEFAULT_HUMAN_WINDOWS
    for w in human_windows:
        if w:
            blocked.append(w)

    # Intimate-night goals get a real arc instead of the generic grid:
    # build-up days, THE EVENT EVENING the night before a free day (so she
    # stays over), and a return-home / morning-after day. That is what the
    # user actually asked for ("sex the night before, return home next day")
    # instead of random days.
    if _goal_kind(goal.get("title") or goal["display_title"]) == "hookup":
        return _hookup_arc(today, deadline, blocked, details,
                           goal.get("title") or goal["display_title"])

    def _day_bounds(day_date=None):
        # convert blocked windows into a busy silhouette over one day (minutes
        # 0-1439), splitting overnight windows across midnight, plus other
        # goals' booked blocks
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

    def _free_from(busy, start, taken, span=90):
        # earliest (s, e) window of `span` minutes starting >= start that
        # avoids every busy block and every slot already taken this day
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

    # --- how much, and when: the human-nature engine (core/planner.py) -----
    # Parse the user's own "hours per day I can commit" answer so the load
    # follows THEIR life, not a fixed guess.
    commit_min = 120
    tp = (details.get("time_per_day") or "").strip().lower()
    m = re.search(r"(\d+)", tp)
    if m:
        n = int(m.group(1))
        unit = tp[m.end():m.end() + 2]
        # "30m" / "45 min" / a bare number over 16 all mean minutes, not hours.
        is_minutes = ("m" in unit and "h" not in unit) or n > 16
        if is_minutes:
            commit_min = max(15, min(n, 16 * 60))
        elif 1 <= n <= 16:
            commit_min = n * 60

    wake, bed = 7 * 60, 23 * 60
    sleep_times = parse_clock_list(details.get("sleep_schedule") or "")
    if len(sleep_times) >= 2:
        b0, w0 = sleep_times[0], sleep_times[1]
        if w0 > 13 * 60 and b0 < w0:      # typed backwards ("7am to 11pm")
            b0, w0 = w0, b0
        nb = planner.normalize_sleep(b0, w0)
        if nb is not None:
            bed, wake = nb, w0
    prefs = planner.Prefs(
        wake=wake, bed=bed, commit_min=commit_min,
        chronotype=planner.infer_chronotype(details.get("peak_energy"), wake, bed),
    )
    others_for = (lambda d: list((busy_by_date or {}).get(d, [])))
    profile = effort_profile(goal, max(days_remaining(deadline, today), 0))
    wins = workout_windows()
    if profile["kind"] == "habit" and wins:
        meals = [w for w in (human_windows or []) if w and (w[1] - w[0]) % 1440 <= 120 and 6 * 60 <= w[0] % 1440 <= 21 * 60]
        # meal windows are the short ones in the routine (sleep is long); take their END
        meals = [w for w in meals if w not in wins]
        horizon = _habit_horizon(today, deadline, wins, now_min, others_for, meals, detail_days)
    else:
        horizon = planner.plan_horizon(
            today, deadline, prefs, _day_bounds, now_min=now_min, others_for=others_for,
        )
        if profile["kind"] == "event":
            horizon = _apply_event_shape(horizon, today, deadline, profile["budget_min"], profile["active_days"])
    _state.profile = profile
    if outline_out is not None:
        outline_out["slot_minutes"] = {d: sum(e - s0 for s0, e in sl) for d, sl in horizon}
    if detail_days:
        cut = (date.fromisoformat(today) + timedelta(days=detail_days - 1)).isoformat()
        horizon = [(d, sl) for d, sl in horizon if d <= cut]
    day_list = [d for d, _ in horizon]
    day_slots_map = {d: s for d, s in horizon}

    # Backwards packing: fill the LAST day first, walking back to today. If we
    # run out of slots (blocked days, tiny windows), the EARLY days get left
    # empty — never the day closest to the deadline, which is the one that
    # actually decides whether the goal lands.
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

    # Order tasks so anything that only makes sense ON the day (fresh flowers,
    # cooking, decorating, getting ready) sits at the END of the sequence and
    # lands on the event day — never days early where it would spoil or be
    # pointless.
    if split_topics and is_event:
        same = [t for t in split_topics if _is_same_day(t)]
        early = [t for t in split_topics if not _is_same_day(t)]
        split_topics = early + same

    packed = []
    for d in reversed(day_list):
        for (s, e) in reversed(day_slots_map.get(d, [])):
            packed.append((d, s, e))
    if is_event and split_topics and len(packed) >= len(split_topics):
        # An event is a short list of one-off jobs, not a syllabus you repeat.
        # Take only the slots closest to the deadline so the arc builds up to
        # the event, and schedule each task exactly once.
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
    # An intimate night is a 2-3 day arc (prep, the night, morning after), not a
    # 20-day grind: only the final days are planned.
    n = min(endd.toordinal() - day0.toordinal() + 1, 3)
    dates = [endd - timedelta(days=n - 1 - i) for i in range(n)]

    # Event evening = the night before a free day if one exists so she stays
    # over and the user returns home the following morning. Favors the LAST
    # such night.
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
            # the evening includes dinner: short meal windows must not break it up
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


def _local_now(db, goal, user=None):
    """The goal owner's wall-clock 'now' (their timezone, not the server's)."""
    from core.clock import now_for
    if user is None and db is not None and goal and goal.get("user_id"):
        try:
            user = db.execute("SELECT timezone FROM users WHERE id=?", (goal["user_id"],)).fetchone()
        except Exception:
            user = None
    return now_for(user)


def generate_plan(goal, user, db=None, extra_blocked=None, prefer_cloud=False, busy_by_date=None):
    """Generate a real schedule for a goal. Returns a list of action dicts from the LLM,
    or None when no LLM can serve one. NEVER fabricates a hardcoded fill-in plan — the
    user's hard rule: only real model-drawn tasks are ever shown on the board."""
    # No liveness pre-probe here. A busy local CPU reads as "down" to a fast
    # probe and that misreport used to spawn MORE competing calls instead of
    # backing off. We just attempt the real call — it serializes on the
    # app-wide ollama lock and gets its real timeout. A genuine failure
    # returns None; the caller keeps the old plan.
    try:
        return _llm_plan_call(goal, db, extra_blocked=extra_blocked, prefer_cloud=prefer_cloud,
                              busy_by_date=busy_by_date, user=user)
    except Exception:
        return None


def generate_plan_or_none(goal, user, db=None, extra_blocked=None, prefer_cloud=False,
                          busy_by_date=None):
    """Like generate_plan but returns None (not the fallback) when no LLM can serve a
    plan, so callers can keep a previously-drawn plan instead of overwriting it with the
    same deterministic fallback. Used for background schedule upgrades."""
    try:
        return _llm_plan_call(goal, db, extra_blocked=extra_blocked, prefer_cloud=prefer_cloud,
                              busy_by_date=busy_by_date, user=user)
    except Exception as exc:
        logger.warning("plan call failed: %r", exc)
        return None


def _goal_kind(title):
    """Classify the goal into 'hookup', 'romance', 'startup' or 'general' so the
    scheduler can give each a real structure instead of the same generic grid."""
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
    # Everything else — startup, exams, fitness, a trip, a project, whatever
    # the user names — gets the SAME scheduling behavior: real concrete
    # actions, right-sized to the deadline, one to two humanly-possible tasks
    # per day. No per-title special cases, no startup-specific templates.
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
    # Every other goal — startup, exams, fitness, a trip, a project — gets NO
    # kind-specific text here: the SAME scheduling behavior applies to all of
    # them (real concrete actions, right-sized to the deadline, as many slots
    # as that day realistically has). That universal rule lives in
    # _llm_plan_call so it reaches every goal the same way.
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


def _extract_links(text):
    """Pull any http(s) URLs the user shared in chat notes so the schedule can
    reference the user's own resources instead of making new ones up."""
    if not text:
        return []
    out = []
    for m in re.finditer(r"https?://[^\s)\]]+", text):
        url = m.group(0).rstrip(".,;:")
        if url not in out:
            out.append(url)
    return out[:8]


def _llm_plan_call(goal, db, timeout=180, extra_blocked=None, prefer_cloud=False, busy_by_date=None,
                   user=None):
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
            "at 5pm when the gym window is 5-7pm is a failed plan, not a judgment call."
        )
    try:
        _details = json.loads(goal.get("details") or "{}")
    except Exception:
        _details = {}
    _routine = parse_routine_windows(_details)
    if _routine:
        _routine_txt = ", ".join(
            f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in _routine
        )
        human_line = (
            "\nHUMAN BASICS ARE MANDATORY, NOT OPTIONAL: the user's own routine blocks are "
            f"{_routine_txt}. NEVER schedule a task inside them. Do not write tasks like "
            "'skip lunch', 'work through dinner', 'skip the gym', 'skip the workout', "
            "'stay up all night'. A person still eats, sleeps, moves and rests while "
            "finishing this. If the deadline is tight, use the free waking hours — never "
            "steal them from food, sleep or health."
        )
    else:
        human_line = (
            "\nHUMAN BASICS ARE MANDATORY, NOT OPTIONAL: never schedule a task during sleep "
            "(23:00-07:00), meals (breakfast 07:00-08:00, lunch 13:00-13:45, dinner 19:30-20:15), "
            "or the daily movement window (18:00-19:00). Do not write tasks like 'skip lunch', "
            "'work through dinner', 'skip the gym', 'skip the workout', 'stay up all night'. "
            "A person still eats, sleeps, moves and rests while finishing this. If the deadline "
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
    # The chat log is the user's working notes on this file. Read it so the
    # schedule does not re-schedule things the user already said they did, and
    # so it flows out of the actual conversation rather than a cold template.
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
    # Links the user shared in chat notes belong on their schedule: extract
    # them so the plan can reference the user's actual resources instead of
    # assuming new ones.
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
        # Zero usable slots, not a model failure. Say WHY, so the user fixes
        # the cause (their blocked windows eat every usable hour) instead of
        # the app retrying a model that was never the problem.
        _state.plan_blocked_reason = (
            "your available windows leave no usable free time between 8am and 11pm on any "
            "day before the deadline — set or relax the Always-Busy Windows once (Settings "
            "panel) and redraw. Ignoring whoever told me what you do at those hours would "
            "mean lying to you, and I don't lie to you."
        )
        return None
    _state.plan_blocked_reason = None

    # One request can't carry the whole horizon: a month-long goal is 40-60+
    # slots, the title budget blows past _MAX_PLAN_TOKENS, the model truncates
    # mid-JSON, the parse fails wholesale, and the ENTIRE plan collapses to
    # empty + infinite retries. Split the grid into small day-bucketed slices
    # instead. Each request is short enough for a slow, weak, or rate-limited
    # free model to FINISH, and a failed slice only costs itself — the
    # surviving slices still produce a real, partial, honest schedule. Nothing
    # is ever substituted or canned.
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
        # prefer_cloud controls WHICH box does the heavy lifting. Explicit
        # user redraws (prefer_cloud=True) run on OpenRouter so a schedule
        # draw is fast and parallel with chat. Auto/retry/checkin paths
        # (prefer_cloud=False) stay on the free local model by default and
        # only touch OpenRouter as a fallback when the local box genuinely
        # fails.
        res = None
        try:
            res = get_manager(db).generate(
                sys, user_prompt, timeout=timeout, prefer_cloud=prefer_cloud,
                max_tokens=_MAX_PLAN_TOKENS,
            )
        except Exception as exc:
            logger.warning("plan slice %s-%s failed: %r", first, last, exc)
            continue
        if not res.ok:
            logger.warning("plan slice %s-%s: model unavailable, skipping", first, last)
            continue
        _state.last_provider = res.provider or "llm"
        title_map = _parse_title_map(res.text)
        for e in chunk:
            t = title_map.get((e["date"], e["start_time"]))
            if not _title_ok(t, goal_ref=title):
                # Never write a placeholder ("<goal> — day 1 part 1") or an
                # unacceptable model title; that silent substitution is the
                # hardcoded-junk bug. The slot is dropped and the honest,
                # smaller plan is what the user sees.
                continue
            out.append({**e, "title": t})
    return out or None


_WEAK_STARTERS = {
    "ensure", "confirm", "consider", "review", "research", "prepare", "prep",
    "discuss", "debate", "brainstorm", "decide", "check", "plan", "remember",
    "think", "make", "brain-storm", "brainstorm",
}


def _title_ok(title, goal_ref=None):
    """Accept a model-written block title only if it's a real, concrete instruction
    of up to two short lines describing what to do in that slot. Otherwise reject it
    — the slot is DROPPED (never substituted with a canned placeholder: the user's
    hard rule is that no deterministic filler is ever shown on the board)."""
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
    # Generic filler verbs banned as WORDS (a real task like 'polish the
    # readme' or 'prepare the slides' is fine — but 'research x', 'polish y',
    # 'prepare for z' as a bare activity is filler). Word-boundary, so
    # 'polished deck' and 'research report' names aren't caught by a
    # substring.
    if any(re.search(rf"\b{re.escape(v)}\b", low) for v in (
            "research", "polish", "prepare", "discuss the plan", "work on it",
            "brainstorm topics", "review the plan", "review the design")):
        return False
    # The old grid placeholder ("<goal> — day 1 part 1") and recycled goal
    # echoes are exactly the "hardcoded junk" the user bans. Reject them
    # structurally so they can never be written, no matter what the weak model
    # spits back.
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
    # Exam/study meta-work is not studying. Reject plan-making and
    # material-gathering dressed up as tasks.
    if any(p in low for p in (
            "study materials", "study guide", "study plan", "study timetable",
            "gather resources", "collect resources", "gather materials",
            "build a schedule", "make a schedule", "create a schedule",
            "make a timetable", "create a timetable", "build a timetable",
            "organize notes", "organise notes", "compile notes")):
        return False
    # A title that STARTS with a weak planning verb ("Ensure aftercare and
    # safety", "Confirm way home") is a topic/vibe, not an action. Reject it
    # so the user's schedule is concrete, never a brainstorm.
    first = re.split(r"[\s:]+", stripped, maxsplit=1)[0].lower().rstrip(".")
    if first in _WEAK_STARTERS:
        return False
    return True


def _parse_title_map(text):
    """Read the model's [ {...} ] array (Ollama often wraps it in markdown fences)
    and map (date, start_time) -> title. Tolerates 'start'/'end' keys and missing times.
    If the array is truncated mid-output (a slow model hitting the token cap), fall
    back to parsing every complete object individually, so the slots the model DID
    finish naming still land in the plan instead of the whole slice dying."""
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
    # Best-effort: the wrap didn't parse (truncated/partial). Extract whatever
    # complete { } objects exist and rebuild the map from those that have all
    # keys.
    for item in re.findall(r"\{[^{}]*\}", text):
        _add(item)
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
    """A 1-10 'how much should you be panicking' score. Past 7 and Eloise starts shouting.
    Driven by days-left vs remaining workload, slippage, blockers, and missed days."""
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
        "gym is cancelled" if score >= 8 else
        "lights are on" if score >= 6 else
        "manageable" if score >= 4 else
        "coasting"
    )
    return {"score": score, "label": label, "days_left": dl, "remaining_hours": round(remaining, 1)}


def momentum_stats(user_id, db=None, today=None):
    """Streaks + burn rate derived from actual done work. Returns best streak, current
    streak, today's completed hours, and a 0-100 momentum meter."""
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