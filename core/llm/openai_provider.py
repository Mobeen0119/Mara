import json
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
        "id": "openrouter", "label": "OpenRouter",
        "url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "match": ["openrouter.ai"], "key_prefix": "sk-or-",
    },
    {
        "id": "gemini", "label": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.5-flash-lite",
        "match": ["generativelanguage.googleapis.com"], "key_prefix": "AIza",
    },
    {
        "id": "openai", "label": "OpenAI",
        "url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
        "match": ["api.openai.com"], "key_prefix": "sk-",
    },
    {
        "id": "groq", "label": "Groq",
        "url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
        "match": ["groq.com"], "key_prefix": "gsk_",
    },
    {
        "id": "cerebras", "label": "Cerebras",
        "url": "https://api.cerebras.ai/v1", "model": "llama-3.3-70b",
        "match": ["cerebras.ai"], "key_prefix": "csk-",
    },
    {
        "id": "deepseek", "label": "DeepSeek",
        "url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
        "match": ["deepseek.com"], "key_prefix": "",
    },
    {
        "id": "together", "label": "Together AI",
        "url": "https://api.together.xyz/v1",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "match": ["together.xyz"], "key_prefix": "",
    },
    {
        "id": "mistral", "label": "Mistral",
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
    """The preset id whose key prefix matches this API key, else None. Used to
    auto-select the provider the moment a key is pasted (sk-or- = OpenRouter,
    AIza = Gemini, gsk_ = Groq, csk- = Cerebras, sk- = OpenAI)."""
    key = (api_key or "").strip()
    if not key:
        return None
    # OpenAI keys are also sk-*, so check the longer OpenRouter prefix first.
    for preset in PROVIDER_PRESETS:
        prefix = preset.get("key_prefix") or ""
        if prefix and key.startswith(prefix):
            return preset["id"]
    return None


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


class OpenAICompatProvider(LLMProvider):
    """Any OpenAI-compatible endpoint. `name` is a short label ('groq', 'openai',
    'openrouter'...) surfaced in the UI and provider receipts."""

    name = "cloud"
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
            headers["HTTP-Referer"] = "https://github.com/Mobeen0119/Eloise"
            headers["X-Title"] = "Eloise"
        return headers

    def _payload(self, system_prompt, user_prompt, max_tokens, stream):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if stream:
            payload["stream"] = True
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
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

    def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None,
                 model_override=None) -> GenerationResult:
        started = time.monotonic()
        payload = self._payload(system_prompt, user_prompt, max_tokens, stream=False)
        if model_override:
            payload["model"] = model_override
        try:
            resp = requests.post(self._url(), json=payload, headers=self._headers(), timeout=timeout)
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
        except (ValueError, KeyError, IndexError) as exc:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"unexpected response from {self.name}: {exc}",
                                    latency_ms=latency_ms)
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
        payload = self._payload(system_prompt, user_prompt, max_tokens, stream=True)
        if model_override:
            payload["model"] = model_override
        try:
            resp = requests.post(self._url(), json=payload, headers=self._headers(),
                                 timeout=timeout, stream=True)
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
                if not line or not line.startswith("data:"):
                    if line and '"error"' in line:
                        try:
                            err = json.loads(line).get("error", {}).get("message") or line
                            raise RuntimeError(f"{self.name} stream error: {err}")
                        except ValueError:
                            pass
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    yield None
                    return
                try:
                    data = json.loads(data_str)
                    choices = data.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        chunk = delta.get("content")
                        if chunk:
                            yield chunk
                except ValueError:
                    continue
        finally:
            try:
                resp.close()
            except Exception:
                pass