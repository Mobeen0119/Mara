import os
import requests
from core.persona import (
    SYSTEM_PROMPT, build_prompt, fallback_message,
    CHAT_SYSTEM_PROMPT, build_chat_prompt, fallback_chat_reply,
    GLOBAL_SYSTEM_PROMPT, build_global_chat_prompt, fallback_global_chat_reply,
    build_opening_prompt, fallback_opening_message, fallback_nudge_message,
    PLAN_SYSTEM_PROMPT, build_plan_prompt, fallback_plan, parse_plan_entries,
    CANCEL_SYSTEM_PROMPT, build_cancel_prompt, fallback_cancel_roast,
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "dolphin-mistral:latest")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free")

LAST_ERRORS = {"ollama": None, "openrouter": None}


def _model_tag_matches(installed_name, wanted):
    installed = installed_name.split(":")[0]
    target = wanted.split(":")[0]
    return installed == target


def _try_ollama(system_prompt, user_prompt, timeout):
    payload = {"model": OLLAMA_MODEL, "system": system_prompt, "prompt": user_prompt, "stream": False}
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        if response.status_code == 404:
            LAST_ERRORS["ollama"] = f"model '{OLLAMA_MODEL}' not found — run: ollama pull {OLLAMA_MODEL}"
            return None
        response.raise_for_status()
        text = response.json().get("response", "").strip()
        if not text:
            LAST_ERRORS["ollama"] = "reached Ollama but got an empty response"
            return None
        LAST_ERRORS["ollama"] = None
        return text
    except requests.exceptions.ConnectionError:
        LAST_ERRORS["ollama"] = "connection refused — is `ollama serve` running and reachable at " + OLLAMA_URL
        return None
    except requests.exceptions.Timeout:
        LAST_ERRORS["ollama"] = f"timed out after {timeout}s — model may be loading, or your machine is too slow for it"
        return None
    except Exception as exc:
        LAST_ERRORS["ollama"] = str(exc)
        return None


def _try_openrouter(system_prompt, user_prompt, timeout):
    if not OPENROUTER_API_KEY:
        LAST_ERRORS["openrouter"] = "no OPENROUTER_API_KEY set"
        return None
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
        if response.status_code == 429:
            LAST_ERRORS["openrouter"] = "rate-limited — free tier quota hit, wait a bit or use a paid model"
            return None
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            LAST_ERRORS["openrouter"] = "reached OpenRouter but got an empty response"
            return None
        LAST_ERRORS["openrouter"] = None
        return text
    except requests.exceptions.Timeout:
        LAST_ERRORS["openrouter"] = f"timed out after {timeout}s"
        return None
    except Exception as exc:
        LAST_ERRORS["openrouter"] = str(exc)
        return None


def _call_llm(system_prompt, user_prompt, timeout=30):
    text = _try_ollama(system_prompt, user_prompt, timeout)
    if text:
        return text, "ollama"
    text = _try_openrouter(system_prompt, user_prompt, min(timeout, 25))
    if text:
        return text, "openrouter"
    return None, "fallback"


def ollama_status():
    local_up = False
    model_available = False
    installed_models = []
    try:
        base = OLLAMA_URL.split("/api/")[0]
        r = requests.get(base + "/api/tags", timeout=3)
        local_up = r.status_code == 200
        if local_up:
            installed_models = [m.get("name", "") for m in r.json().get("models", [])]
            model_available = any(_model_tag_matches(m, OLLAMA_MODEL) for m in installed_models)
    except Exception as exc:
        LAST_ERRORS["ollama"] = str(exc)
    if local_up and not model_available:
        LAST_ERRORS["ollama"] = f"Ollama is running but '{OLLAMA_MODEL}' isn't pulled — run: ollama pull {OLLAMA_MODEL}"
    return {
        "local_reachable": local_up,
        "local_model_available": model_available,
        "ollama_model": OLLAMA_MODEL,
        "cloud_configured": bool(OPENROUTER_API_KEY),
        "openrouter_model": OPENROUTER_MODEL,
        "any_reachable": (local_up and model_available) or bool(OPENROUTER_API_KEY),
        "ollama_error": LAST_ERRORS["ollama"],
        "openrouter_error": LAST_ERRORS["openrouter"],
    }


def generate_message(name, goal, deadline, constraints, days_left, is_overdue, is_first=False):
    prompt = build_prompt(name, goal, deadline, constraints, days_left, is_overdue, is_first)
    text, source = _call_llm(SYSTEM_PROMPT, prompt)
    if text:
        return text, source
    return fallback_message(name, goal, deadline, constraints, days_left, is_overdue, is_first), "fallback"


def generate_chat_reply(name, goal, deadline, constraints, days_left, is_overdue, history, user_message, notes_context=None):
    prompt = build_chat_prompt(name, goal, deadline, constraints, days_left, is_overdue, history, user_message, notes_context)
    text, source = _call_llm(CHAT_SYSTEM_PROMPT, prompt)
    if text:
        return text, source
    return fallback_chat_reply(user_message, goal, deadline, is_overdue, history), "fallback"


def generate_global_chat_reply(name, tasks_summary, history, user_message):
    prompt = build_global_chat_prompt(name, tasks_summary, history, user_message)
    text, source = _call_llm(GLOBAL_SYSTEM_PROMPT, prompt)
    if text:
        return text, source
    return fallback_global_chat_reply(user_message, tasks_summary, history), "fallback"


def generate_opening_message(name, tasks_summary):
    prompt = build_opening_prompt(name, tasks_summary)
    text, source = _call_llm(GLOBAL_SYSTEM_PROMPT, prompt)
    if text:
        return text, source
    return fallback_opening_message(tasks_summary), "fallback"


def generate_nudge(name, tasks_summary, nearest_days_left=None):
    prompt = (
        f"User: {name}\nOpen tasks:\n{tasks_summary or 'none'}\n"
        f"The user has gone quiet on this chat for a while. Speak first, unprompted, and push them on the nearest task."
    )
    text, source = _call_llm(GLOBAL_SYSTEM_PROMPT, prompt)
    if text:
        return text, source
    return fallback_nudge_message(tasks_summary, nearest_days_left), "fallback"


def generate_plan(name, goal, deadline, constraints, days_left, reminder_time="08:00", chat_context=None, notes_context=None, all_tasks_context=None, global_chat_context=None, other_windows_by_date=None):
    prompt = build_plan_prompt(name, goal, deadline, constraints, days_left, reminder_time, chat_context, notes_context, all_tasks_context, global_chat_context)
    text, source = _call_llm(PLAN_SYSTEM_PROMPT, prompt, timeout=90)
    if text and parse_plan_entries(text):
        return text, source
    return fallback_plan(goal, deadline, constraints, days_left, reminder_time, other_windows_by_date), "fallback"


def generate_cancel_roast(name, goal, deadline, reason):
    prompt = build_cancel_prompt(name, goal, deadline, reason)
    text, source = _call_llm(CANCEL_SYSTEM_PROMPT, prompt, timeout=20)
    if text:
        return text, source
    return fallback_cancel_roast(goal, reason), "fallback"
