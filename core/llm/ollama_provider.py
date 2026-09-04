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

    def _candidates(self):
        base = self.base_url.rstrip("/")
        hosts = [
            base,
        ] + [f"http://{ip}:11434" for ip in _wsl_host_ip()]
        out = []
        for h in hosts:
            if h not in out:
                out.append(h)
        return out

    def _tags(self, url=None):
        resp = requests.get(f"{(url or self.base_url)}/api/tags", timeout=self.connect_timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def probe_fast(self, timeout=1.5) -> bool:
        """Quick readiness check against the primary URL only (no WSL host sweeping).
        Returns True only when reachable AND the model is installed. Capped so it can't hang."""
        import socket
        deadline = time.monotonic() + timeout
        try:
            socket.setdefaulttimeout(timeout)
            host = self.base_url.replace("http://", "").replace("https://", "")
            if ":" in host:
                h, p = host.rsplit(":", 1)
                port = int(p) if p.isdigit() else 11434
            else:
                h, port = host, 11434
            with socket.create_connection((h, port), timeout=timeout):
                pass
        except Exception:
            return False
        if time.monotonic() > deadline:
            return False
        try:
            installed = requests.get(f"{self.base_url}/api/tags", timeout=timeout)
            self._last_models = [m.get("name", "") for m in installed.json().get("models", [])]
            return any(_tag_matches(m, self.model) for m in self._last_models)
        except Exception:
            return False

    def status(self) -> ProviderStatus:
        last_err = None
        for url in self._candidates():
            try:
                installed = self._tags(url)
                if any(_tag_matches(m, self.model) for m in installed):
                    return ProviderStatus(
                        name=self.name, state=ProviderState.READY,
                        detail=f"reachable at {url}, model installed",
                        model=self.model, available_models=installed,
                    )
                return ProviderStatus(
                    name=self.name, state=ProviderState.MODEL_MISSING,
                    detail=f"Ollama running at {url} but '{self.model}' isn't pulled — run: ollama pull {self.model}",
                    model=self.model, available_models=installed,
                )
            except requests.exceptions.ConnectionError:
                last_err = f"connection refused at {url} — is `ollama serve` running?"
            except requests.exceptions.Timeout:
                last_err = f"timed out reaching {url} after {self.connect_timeout}s"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc} at {url}"
        return ProviderStatus(
            name=self.name, state=ProviderState.UNREACHABLE,
            detail=last_err or "no Ollama endpoint reachable", model=self.model,
        )

    def generate(self, system_prompt, user_prompt, timeout=30) -> GenerationResult:
        payload = {"model": self.model, "system": system_prompt, "prompt": user_prompt, "stream": False}
        last_err = None
        # Per-endpoint budget: connection-refused fails instantly; but a host that accepts
        # yet stalls should not cost the full `timeout` per candidate. Cap each try short.
        per_try = min(int(timeout), 8)
        for url in self._candidates():
            started = time.monotonic()
            try:
                resp = requests.post(
                    f"{url}/api/generate", json=payload,
                    timeout=(min(self.connect_timeout, 3), per_try),
                )
            except requests.exceptions.ConnectionError:
                last_err = f"connection refused — is `ollama serve` running at {url}?"
                continue
            except requests.exceptions.Timeout:
                last_err = f"timed out after {per_try}s at {url} — model may still be loading"
                continue
            except Exception as exc:
                last_err = str(exc)
                continue

            latency_ms = int((time.monotonic() - started) * 1000)

            if resp.status_code == 404:
                return GenerationResult(
                    ok=False, provider=self.name, model=self.model,
                    error=f"model '{self.model}' not found — run: ollama pull {self.model}",
                    latency_ms=latency_ms,
                )
            if not resp.ok:
                last_err = f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            try:
                text = resp.json().get("response", "")
            except ValueError:
                last_err = "Ollama response wasn't valid JSON"
                continue
            if not _looks_valid(text):
                last_err = "model returned an empty or degenerate response"
                continue
            return GenerationResult(
                ok=True, provider=self.name, model=self.model, text=text.strip(), latency_ms=latency_ms,
            )
        return GenerationResult(ok=False, provider=self.name, model=self.model, error=last_err or "no endpoint")