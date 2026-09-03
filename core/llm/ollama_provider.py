import subprocess
import time

import requests

from core.llm.base import GenerationResult, LLMProvider, ProviderState, ProviderStatus

DEFAULT_MODEL = "huihui_ai/dolphin3-abliterated:latest"


def _wsl_host_ip():
    """Best-effort discovery of a peer host that runs Ollama (WSL on Windows)."""
    candidates = []
    import re
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"], capture_output=True, text=True, timeout=3
        ).stdout
        m = re.search(r"via (\S+)", out)
        if m:
            candidates.append(m.group(1))
    except Exception:
        pass
    try:
        for line in subprocess.run(
                ["ip", "route", "show"], capture_output=True, text=True, timeout=3).stdout.splitlines():
            if "default" in line or "eth0" in line:
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    candidates.append(m.group(1))
    except Exception:
        pass
    seen = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    return seen


def _tag_matches(installed_name, wanted):
    return installed_name.split(":")[0] == wanted.split(":")[0]


def _looks_valid(text):
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if not any(ch.isalnum() for ch in stripped):
        return False
    return True


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url, model, connect_timeout=3):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.connect_timeout = connect_timeout

    def _tags(self):
        resp = requests.get(f"{self.base_url}/api/tags", timeout=self.connect_timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def status(self) -> ProviderStatus:
        try:
            installed = self._tags()
        except requests.exceptions.ConnectionError:
            return ProviderStatus(
                name=self.name, state=ProviderState.UNREACHABLE,
                detail=f"connection refused at {self.base_url} — is `ollama serve` running?",
                model=self.model,
            )
        except requests.exceptions.Timeout:
            return ProviderStatus(
                name=self.name, state=ProviderState.UNREACHABLE,
                detail=f"timed out reaching {self.base_url} after {self.connect_timeout}s",
                model=self.model,
            )
        except Exception as exc:
            return ProviderStatus(
                name=self.name, state=ProviderState.UNREACHABLE,
                detail=f"{type(exc).__name__}: {exc}", model=self.model,
            )

        if any(_tag_matches(m, self.model) for m in installed):
            return ProviderStatus(
                name=self.name, state=ProviderState.READY,
                detail="reachable, model installed", model=self.model,
                available_models=installed,
            )
        return ProviderStatus(
            name=self.name, state=ProviderState.MODEL_MISSING,
            detail=f"Ollama is running but '{self.model}' isn't pulled — run: ollama pull {self.model}",
            model=self.model, available_models=installed,
        )

    def generate(self, system_prompt, user_prompt, timeout=30) -> GenerationResult:
        payload = {"model": self.model, "system": system_prompt, "prompt": user_prompt, "stream": False}
        started = time.monotonic()
        try:
            resp = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError:
            return GenerationResult(
                ok=False, provider=self.name, model=self.model,
                error=f"connection refused — is `ollama serve` running at {self.base_url}?",
            )
        except requests.exceptions.Timeout:
            return GenerationResult(
                ok=False, provider=self.name, model=self.model,
                error=f"timed out after {timeout}s — model may still be loading",
            )
        except Exception as exc:
            return GenerationResult(ok=False, provider=self.name, model=self.model, error=str(exc))

        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code == 404:
            return GenerationResult(
                ok=False, provider=self.name, model=self.model,
                error=f"model '{self.model}' not found — run: ollama pull {self.model}",
                latency_ms=latency_ms,
            )
        if not resp.ok:
            return GenerationResult(
                ok=False, provider=self.name, model=self.model,
                error=f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}",
                latency_ms=latency_ms,
            )
        try:
            text = resp.json().get("response", "")
        except ValueError:
            return GenerationResult(
                ok=False, provider=self.name, model=self.model,
                error="Ollama response wasn't valid JSON", latency_ms=latency_ms,
            )
        if not _looks_valid(text):
            return GenerationResult(
                ok=False, provider=self.name, model=self.model,
                error="model returned an empty or degenerate response", latency_ms=latency_ms,
            )
        return GenerationResult(
            ok=True, provider=self.name, model=self.model, text=text.strip(), latency_ms=latency_ms,
        )