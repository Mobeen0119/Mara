import time

import requests

from core.llm.base import GenerationResult, LLMProvider, ProviderState, ProviderStatus


class OpenRouterProvider(LLMProvider):
    name = "openrouter"

    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

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

    def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None) -> GenerationResult:
        started = time.monotonic()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        url = "https://openrouter.ai/api/v1/chat/completions"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            latency_ms = int((time.monotonic() - started) * 1000)
        except requests.exceptions.ConnectionError:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error="connection error reaching OpenRouter")
        except requests.exceptions.Timeout:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"timed out after {timeout}s")
        except Exception as exc:
            return GenerationResult(ok=False, provider=self.name, model=self.model, error=str(exc))

        if not resp.ok:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}",
                                    latency_ms=latency_ms)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error=f"unexpected OpenRouter response: {exc}", latency_ms=latency_ms)
        if not text or not text.strip():
            return GenerationResult(ok=False, provider=self.name, model=self.model,
                                    error="empty response from OpenRouter", latency_ms=latency_ms)
        return GenerationResult(ok=True, provider=self.name, model=self.model,
                                text=text.strip(), latency_ms=latency_ms)

    def generate_stream(self, system_prompt, user_prompt, timeout=30, max_tokens=None):
        """Yield text chunks as they arrive (SSE streaming). None when done."""
        if not self.api_key:
            raise RuntimeError("no API key configured")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        url = "https://openrouter.ai/api/v1/chat/completions"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout, stream=True)
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError("connection error reaching OpenRouter") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"timed out after {timeout}s") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

        if not resp.ok:
            raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")
        import json as _json
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    if line and '"error"' in line:
                        try:
                            err = _json.loads(line).get("error", {}).get("message") or line
                            raise RuntimeError(f"OpenRouter stream error: {err}")
                        except ValueError:
                            pass
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    yield None
                    return
                try:
                    data = _json.loads(data_str)
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