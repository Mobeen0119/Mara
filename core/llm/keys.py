"""'Paste any key and go'. Work out which provider a key belongs to, prove it
works, and pick a sensible chat model from what that account can actually use.

Order of evidence:
  1. an explicit base_url / provider from the caller,
  2. the key's own prefix (sk-ant-, sk-or-, AIza, gsk_, ...) -> one candidate,
  3. the key's shape (64 hex = Together, sk-+32 hex = DeepSeek/OpenAI, 32 alnum = Mistral),
  4. otherwise probe every known provider in parallel and take the one that accepts the key.
The persona/system prompt is NOT part of this: it is applied identically for every
provider by the manager, so switching provider never changes Eloise's voice."""
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from core.llm.anthropic_provider import API_VERSION
from core.llm.openai_provider import (
    PRESET_BY_ID, PROVIDER_PRESETS, clean_base_url, preset_for_api_key, preset_for_base_url,
)
from core.security import UnsafeURL, validate_outbound_url

PROBE_TIMEOUT = 8

_NOT_CHAT = re.compile(
    r"embed|whisper|tts|speech|transcri|audio|image|dall|sora|moderation|guard|rerank|"
    r"realtime|vision-preview|imagen|veo|lyria|aqa|safety|ocr|diffusion|flux|stable|"
    r"computer-use|search-preview|davinci|babbage|instruct-0", re.I)
# Cheapest/fastest first: Eloise sends short chat turns and many small plan slices.
_PREFER = [
    r"flash-lite", r"haiku", r"nano", r"mini", r"flash", r"instant", r"small", r"lite", r"8b", r"70b",
    r"sonnet", r"turbo", r"chat",
]


def pick_model(preset_id, models):
    """The preset's vetted default when the account has it, else the best-looking chat model."""
    preset = PRESET_BY_ID.get(preset_id) or {}
    default = preset.get("model")
    names = [m for m in (models or []) if m]
    if default and default in names:
        return default
    chat = [m for m in names if not _NOT_CHAT.search(m)]
    if not chat:
        return default or (names[0] if names else None)
    for pat in _PREFER:
        hits = sorted(m for m in chat if re.search(pat, m, re.I))
        if hits:
            return hits[-1]          # highest version string sorts last
    return sorted(chat)[-1]


def candidates_for_key(api_key):
    """Provider ids worth probing for this key, most likely first."""
    key = api_key.strip()
    by_prefix = preset_for_api_key(key)
    if by_prefix and by_prefix != "openai":
        return [by_prefix]
    if re.fullmatch(r"sk-proj-[\w-]+", key) or re.fullmatch(r"sk-svcacct-[\w-]+", key):
        return ["openai"]
    if re.fullmatch(r"sk-[0-9a-f]{32}", key):
        return ["deepseek", "openai"]
    if key.startswith("sk-"):
        return ["openai", "deepseek"]
    if re.fullmatch(r"[0-9a-f]{64}", key):
        return ["together"]
    if re.fullmatch(r"[A-Za-z0-9]{32}", key):
        return ["mistral", "fireworks", "huggingface"]
    # unknown shape: try everything that is not claimed by a distinctive prefix
    return [p["id"] for p in PROVIDER_PRESETS]


def _probe(preset, api_key, base_url=None, protocol=None):
    """-> dict(status, models, error). status: 'ok' | 'auth' | 'unknown' | 'network'"""
    base = (base_url or preset["url"]).rstrip("/")
    protocol = protocol or preset.get("protocol", "openai")
    try:
        if protocol == "anthropic":
            r = requests.get(f"{base}/v1/models?limit=100", timeout=PROBE_TIMEOUT, allow_redirects=False,
                             headers={"x-api-key": api_key, "anthropic-version": API_VERSION})
        elif preset.get("auth_check"):
            r = requests.get(base + preset["auth_check"], timeout=PROBE_TIMEOUT, allow_redirects=False,
                             headers={"Authorization": f"Bearer {api_key}"})
        else:
            r = requests.get(f"{base}/models", timeout=PROBE_TIMEOUT, allow_redirects=False,
                             headers={"Authorization": f"Bearer {api_key}"})
    except requests.exceptions.RequestException as exc:
        return {"status": "network", "models": [], "error": f"{type(exc).__name__}"}
    if r.status_code in (401, 403):
        return {"status": "auth", "models": [], "error": f"HTTP {r.status_code}: key rejected"}
    if r.status_code == 200:
        models = []
        try:
            if preset.get("auth_check"):  # key validated; model list is a separate public call
                m = requests.get(f"{base}/models", timeout=PROBE_TIMEOUT, allow_redirects=False)
                data = m.json() if m.ok else {}
            else:
                data = r.json()
            items = data.get("data") if isinstance(data, dict) else data
            models = [str((x or {}).get("id", "")).replace("models/", "", 1) for x in (items or []) if x]
        except ValueError:
            pass
        return {"status": "ok", "models": [m for m in models if m], "error": ""}
    # 404/405 etc: endpoint has no model list (e.g. Perplexity) - not proof of a bad key
    return {"status": "unknown", "models": [], "error": f"HTTP {r.status_code}"}


def autoconfigure(api_key, base_url=None, provider=None, model=None):
    """Return {ok, provider, label, protocol, base_url, model, models, verified, tried[], error}."""
    api_key = (api_key or "").strip()
    tried = []
    if not api_key:
        return {"ok": False, "error": "no API key", "tried": tried}

    cands = []   # (preset-like dict, base_url, protocol)
    if base_url and base_url.strip():
        url = clean_base_url(base_url)
        try:
            validate_outbound_url(url)
        except UnsafeURL as exc:
            return {"ok": False, "error": str(exc), "tried": tried}
        pid = provider if provider in PRESET_BY_ID else preset_for_base_url(url)
        preset = dict(PRESET_BY_ID[pid]) if pid else {"id": pid or "custom", "label": "Custom endpoint",
                                                       "protocol": "openai", "url": url, "model": model or ""}
        cands.append((preset, url, preset.get("protocol", "openai")))
    elif provider in PRESET_BY_ID:
        p = PRESET_BY_ID[provider]
        cands.append((p, p["url"], p.get("protocol", "openai")))
    else:
        for pid in candidates_for_key(api_key):
            p = PRESET_BY_ID[pid]
            cands.append((p, p["url"], p.get("protocol", "openai")))

    with ThreadPoolExecutor(max_workers=min(8, len(cands))) as pool:
        results = list(pool.map(lambda c: _probe(c[0], api_key, c[1], c[2]), cands))

    for (preset, url, protocol), res in zip(cands, results):
        tried.append({"provider": preset["id"], "status": res["status"], "error": res["error"]})

    unambiguous = len(cands) == 1
    chosen = None
    for (preset, url, protocol), res in zip(cands, results):          # priority order
        if res["status"] == "ok":
            chosen = (preset, url, protocol, res, True)
            break
    if chosen is None:
        for (preset, url, protocol), res in zip(cands, results):
            # the key's prefix named this provider and it answered 'no model list' or
            # was unreachable: trust the prefix, flag as unverified
            if unambiguous and res["status"] in ("unknown", "network"):
                chosen = (preset, url, protocol, res, False)
                break
    if chosen is None:
        if all(t["status"] == "auth" for t in tried):
            who = ", ".join(t["provider"] for t in tried[:6])
            return {"ok": False, "error": f"key rejected by every provider tried ({who})", "tried": tried}
        return {"ok": False, "error": "could not reach a provider to verify this key (network?), "
                                      "and its shape does not identify one - pick a provider manually",
                "tried": tried}

    preset, url, protocol, res, verified = chosen
    chosen_model = (model or "").strip() or pick_model(preset["id"], res["models"]) or preset.get("model") or ""
    return {
        "ok": True, "verified": verified, "provider": preset["id"], "label": preset.get("label", preset["id"]),
        "protocol": protocol, "base_url": url, "model": chosen_model,
        "models": res["models"][:60], "tried": tried,
    }
