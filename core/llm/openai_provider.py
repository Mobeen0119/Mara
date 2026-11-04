import json
import threading
import time

import requests

from core.llm.base import GenerationResult, LLMProvider, ProviderState, ProviderStatus

# Any model that speaks the OpenAI chat-completions wire format works through
# this one provider: OpenAI, Groq, OpenRouter, Together, DeepSeek, Mistral,
# xAI, a local vLLM, LM Studio, etc. The operator just supplies a key, an
# endpoint and a model.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_CLOUD_MODEL = "meta-llama/llama-3.1-8b-instruct:free"

# Provider presets shared by the API and the Settings UI: pick a provider and
# the endpoint + a sane default model come with it, so "paste a key and go"
# never leaves a stale base URL or model from the previous provider behind.
PROVIDER_PRESETS = [
    {
        "id": "openrouter", "label": "OpenRouter", "protocol": "openai",
        "url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "match": ["openrouter.ai"], "key_prefix": "sk-or-",
        "auth_check": "/auth/key",
    },
    {
        "id": "anthropic", "label": "Anthropic (Claude)", "protocol": "anthropic",
        "url": "https://api.anthropic.com", "model": "claude-haiku-5-5",
        "match": ["anthropic.com"], "key_prefix": "sk-ant-",
    },
    {
        "id": "gemini", "label": "Google Gemini", "protocol": "openai",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.5-flash-lite",
        "match": ["generativelanguage.googleapis.com"], "key_prefix": "AIza",
    },
    {
        "id": "openai", "label": "OpenAI", "protocol": "openai",
        "url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
        "match": ["api.openai.com"], "key_prefix": "sk-",
    },
    {
        "id": "groq", "label": "Groq", "protocol": "openai",
        "url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
        "match": ["groq.com"], "key_prefix": "gsk_",
    },
    {
        "id": "cerebras", "label": "Cerebras", "protocol": "openai",
        "url": "https://api.cerebras.ai/v1", "model": "llama-3.3-70b",
        "match": ["cerebras.ai"], "key_prefix": "csk-",
    },
    {
        "id": "xai", "label": "xAI (Grok)", "protocol": "openai",
        "url": "https://api.x.ai/v1", "model": "grok-3-mini",
        "match": ["api.x.ai"], "key_prefix": "xai-",
    },
    {
        "id": "perplexity", "label": "Perplexity", "protocol": "openai",
        "url": "https://api.perplexity.ai", "model": "sonar",
        "match": ["perplexity.ai"], "key_prefix": "pplx-", "no_models_endpoint": True,
    },
    {
        "id": "fireworks", "label": "Fireworks AI", "protocol": "openai",
        "url": "https://api.fireworks.ai/inference/v1",
        "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "match": ["fireworks.ai"], "key_prefix": "fw_",
    },
    {
        "id": "nvidia", "label": "NVIDIA NIM", "protocol": "openai",
        "url": "https://integrate.api.nvidia.com/v1", "model": "meta/llama-3.3-70b-instruct",
        "match": ["nvidia.com"], "key_prefix": "nvapi-",
    },
    {
        "id": "huggingface", "label": "Hugging Face", "protocol": "openai",
        "url": "https://router.huggingface.co/v1", "model": "meta-llama/Llama-3.3-70B-Instruct",
        "match": ["huggingface.co"], "key_prefix": "hf_",
    },
    {
        "id": "deepseek", "label": "DeepSeek", "protocol": "openai",
        "url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
        "match": ["deepseek.com"], "key_prefix": "",
    },
    {
        "id": "together", "label": "Together AI", "protocol": "openai",
        "url": "https://api.together.xyz/v1",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "match": ["together.xyz"], "key_prefix": "",
    },
    {
        "id": "mistral", "label": "Mistral", "protocol": "openai",
        "url": "https://api.mistral.ai/v1", "model": "mistral-small-latest",
        "match": ["api.mistral.ai"], "key_prefix": "",
    },
]
PRESET_BY_ID = {p["id"]: p for p in PROVIDER_PRESETS}


def preset_for_base_url(base_url):
    """The preset id whose endpoint matches this base URL, else None."""
    raw = (base_url or "").lower()
    if not raw:
        return None
    for preset in PROVIDER_PRESETS:
        if any(needle in raw for needle in preset["match"]):
            return preset["id"]
    return None


def preset_for_api_key(api_key):
    """The preset id whose key prefix matches this API key, else None. The LONGEST
    matching prefix wins (sk-ant- = Anthropic and sk-or- = OpenRouter beat the
    generic OpenAI sk-). Prefix-less providers are resolved by probing, see keys.py."""
    key = (api_key or "").strip()
    if not key:
        return None
    best, best_len = None, 0
    for preset in PROVIDER_PRESETS:
        prefix = preset.get("key_prefix") or ""
        if prefix and key.startswith(prefix) and len(prefix) > best_len:
            best, best_len = preset["id"], len(prefix)
    return best


def clean_base_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    # Reduce a pasted full endpoint to its base ("/v1" is part of the base and
    # must survive — the request URL is base + /chat/completions). Only a
    # trailing /chat/completions (or /v1/chat/completions) gets removed.
    raw = raw.rstrip("/")
    if raw.endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")].rstrip("/")
    return raw or DEFAULT_BASE_URL


# Per-(endpoint, model) quirks learned at runtime, so the retry dance happens
# once per process, not once per request.
_QUIRKS = {}
_QUIRK_LOCK = threading.Lock()


def _quirks(base_url, model):
    with _QUIRK_LOCK:
        return dict(_QUIRKS.get((base_url, model), {}))


def _learn(base_url, model, **kw):
    with _QUIRK_LOCK:
        _QUIRKS.setdefault((base_url, model), {}).update(kw)


def _wants_completion_tokens(err_text):
    t = (err_text or "").lower()
    return "max_completion_tokens" in t or ("max_tokens" in t and ("unsupported" in t or "not supported" in t))


def _rejects_system_role(err_text):
    t = (err_text or "").lower()
    return ("developer instruction" in t or "system message" in t or "system role" in t
            or "system prompt" in t and "not" in t or "instruction is not enabled" in t)


class OpenAICompatProvider(LLMProvider):
    """Any OpenAI-compatible endpoint. `name` is a short label ('groq', 'openai',
    'openrouter'...) surfaced in the UI and provider receipts. The persona travels
    as the system message verbatim; for the few models that refuse a system role
    it is folded into the first user turn UNCHANGED, never dropped."""

    name = "cloud"
    protocol = "openai"
    requires_key = True

    def __init__(self, api_key="", model=None, base_url=None, name="cloud"):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or DEFAULT_CLOUD_MODEL
        self.base_url = clean_base_url(base_url)
        self.name = name or "cloud"

    def _url(self):
        return f"{self.base_url}/chat/completions"

    def _headers(self):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = "https://github.com/Mobeen0119/Mara"
            headers["X-Title"] = "Eloise"
        return headers

    def _payload(self, system_prompt, user_prompt, max_tokens, stream, model=None):
        model = model or self.model
        q = _quirks(self.base_url, model)
        if q.get("no_system"):
            messages = [{"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"}]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        payload = {"model": model, "messages": messages}
        if stream:
            payload["stream"] = True
        if max_tokens:
            key = "max_completion_tokens" if q.get("completion_tokens") else "max_tokens"
            payload[key] = int(max_tokens)
        return payload

    def status(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(
                name=self.name, state=ProviderState.UNREACHABLE,
                detail="no API key configured", model=self.model,
            )
        return ProviderStatus(
            name=self.name, state=ProviderState.READY,
            detail="configured", model=self.model,
        )

    def list_models(self, timeout=10):
        resp = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=timeout,
                            allow_redirects=False)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        return [str((m or {}).get("id", "")).replace("models/", "", 1) for m in (items or []) if m]

    def _post(self, system_prompt, user_prompt, max_tokens, timeout, stream, model):
        """POST once; if the provider says 'wrong parameter name' or 'no system role',
        adapt the payload and retry (at most twice). Returns the requests response."""
        resp = None
        for _ in range(3):
            payload = self._payload(system_prompt, user_prompt, max_tokens, stream, model)
            resp = requests.post(self._url(), json=payload, headers=self._headers(),
                                 timeout=timeout, stream=stream, allow_redirects=False)
            if resp.status_code != 400:
                return resp
            body = resp.text[:600]
            q = _quirks(self.base_url, payload["model"])
            if max_tokens and not q.get("completion_tokens") and _wants_completion_tokens(body):
                _learn(self.base_url, payload["model"], completion_tokens=True)
                continue
            if not q.get("no_system") and _rejects_system_role(body):
                _learn(self.base_url, payload["model"], no_system=True)
                continue
            return resp
        return resp

    def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None,
                 model_override=None) -> GenerationResult:
        started = time.monotonic()
        try:
            resp = self._post(system_prompt, user_prompt, max_tokens, timeout, False, model_override)
        except requests.exceptions.ConnectionError:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"connection error reaching {self.base_url}")
        except requests.exceptions.Timeout:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"timed out after {timeout}s")
        except Exception as exc:
            return GenerationResult(ok=False, provider=self.name, model=self.model, error=str(exc))
        latency_ms = int((time.monotonic() - started) * 1000)
        if not resp.ok:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}",
                                    latency_ms=latency_ms)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"unexpected response from {self.name}: {exc}",
                                    latency_ms=latency_ms)
        if isinstance(text, list):  # some gateways return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if not text or not text.strip():
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error="empty response", latency_ms=latency_ms)
        return GenerationResult(ok=True, provider=self.name, model=self.model,
                                text=text.strip(), latency_ms=latency_ms)

    def generate_stream(self, system_prompt, user_prompt, timeout=30, max_tokens=None,
                        model_override=None):
        """Yield text chunks as they arrive (SSE streaming). None when done."""
        if not self.api_key:
            raise RuntimeError("no API key configured")
        try:
            resp = self._post(system_prompt, user_prompt, max_tokens, timeout, True, model_override)
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(f"connection error reaching {self.base_url}") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"timed out after {timeout}s") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        if not resp.ok:
            raise RuntimeError(f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if not line.startswith("data:"):
                    if '"error"' in line:
                        try:
                            err = json.loads(line).get("error", {}).get("message") or line
                        except ValueError:
                            continue
                        raise RuntimeError(f"{self.name} stream error: {err}")
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    yield None
                    return
                try:
                    data = json.loads(data_str)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("error"):
                    err = data["error"]
                    raise RuntimeError(f"{self.name} stream error: {err.get('message') if isinstance(err, dict) else err}")
                choices = data.get("choices") or []
                if choices:
                    chunk = (choices[0].get("delta") or {}).get("content")
                    if chunk:
                        yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass
