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

    def generate(self, system_prompt, user_prompt, timeout=30) -> GenerationResult:
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