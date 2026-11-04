import json
import time

import requests

from core.llm.base import GenerationResult, LLMProvider, ProviderState, ProviderStatus

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-5-5"
API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 1024  # the Messages API requires max_tokens


class AnthropicProvider(LLMProvider):
    """Native Claude Messages API. Same contract as the OpenAI-compatible provider:
    the persona goes in as the top-level `system` field, byte-for-byte."""

    name = "anthropic"
    protocol = "anthropic"
    requires_key = True

    def __init__(self, api_key="", model=None, base_url=None, name="anthropic"):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or DEFAULT_MODEL
        base = (base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
        if base.endswith("/v1"):
            base = base[:-3]
        self.base_url = base
        self.name = name or "anthropic"

    def _headers(self):
        return {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def _payload(self, system_prompt, user_prompt, max_tokens, stream, model=None):
        payload = {
            "model": model or self.model,
            "max_tokens": int(max_tokens or _DEFAULT_MAX_TOKENS),
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if stream:
            payload["stream"] = True
        return payload

    def status(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(name=self.name, state=ProviderState.UNREACHABLE,
                                  detail="no API key configured", model=self.model)
        return ProviderStatus(name=self.name, state=ProviderState.READY, detail="configured", model=self.model)

    def list_models(self, timeout=10):
        resp = requests.get(f"{self.base_url}/v1/models?limit=100", headers=self._headers(),
                            timeout=timeout, allow_redirects=False)
        resp.raise_for_status()
        return [m.get("id", "") for m in resp.json().get("data", []) if m.get("id")]

    def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None,
                 model_override=None) -> GenerationResult:
        started = time.monotonic()
        try:
            resp = requests.post(
                f"{self.base_url}/v1/messages",
                json=self._payload(system_prompt, user_prompt, max_tokens, False, model_override),
                headers=self._headers(), timeout=timeout, allow_redirects=False,
            )
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
            blocks = resp.json()["content"]
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (ValueError, KeyError, TypeError) as exc:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"unexpected response from {self.name}: {exc}",
                                    latency_ms=latency_ms)
        if not text.strip():
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error="empty response", latency_ms=latency_ms)
        return GenerationResult(ok=True, provider=self.name, model=self.model,
                                text=text.strip(), latency_ms=latency_ms)

    def generate_stream(self, system_prompt, user_prompt, timeout=30, max_tokens=None,
                        model_override=None):
        if not self.api_key:
            raise RuntimeError("no API key configured")
        try:
            resp = requests.post(
                f"{self.base_url}/v1/messages",
                json=self._payload(system_prompt, user_prompt, max_tokens, True, model_override),
                headers=self._headers(), timeout=timeout, stream=True, allow_redirects=False,
            )
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
                    continue
                try:
                    data = json.loads(line[len("data:"):].strip())
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "content_block_delta":
                    chunk = (data.get("delta") or {}).get("text")
                    if chunk:
                        yield chunk
                elif kind == "message_stop":
                    yield None
                    return
                elif kind == "error":
                    raise RuntimeError(f"{self.name} stream error: {(data.get('error') or {}).get('message')}")
        finally:
            try:
                resp.close()
            except Exception:
                pass
