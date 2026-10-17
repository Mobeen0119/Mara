import json
import time

import requests

from core.llm.base import GenerationResult, LLMProvider, ProviderState, ProviderStatus

# Any model that speaks the OpenAI chat-completions wire format works through this
# one provider: OpenAI, Groq, OpenRouter, Together, DeepSeek, Mistral, xAI, a local
# vLLM, LM Studio, etc. The operator just supplies a key, an endpoint and a model.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_CLOUD_MODEL = "meta-llama/llama-3.1-8b-instruct:free"


def clean_base_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    # If someone pastes a full /chat/completions endpoint, strip to the base.
    raw = raw.rstrip("/")
    for suffix in ("/chat/completions", "/v1/chat/completions", "/v1", "/api/v1"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.rstrip("/") or DEFAULT_BASE_URL


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
            headers["HTTP-Referer"] = "https://github.com/Mobeen0119/Mara"
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