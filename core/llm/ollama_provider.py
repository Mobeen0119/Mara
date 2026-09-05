import json
import os
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
        try:
            self.keep_alive = int(os.environ.get("OLLAMA_KEEP_ALIVE", "1800"))
        except ValueError:
            self.keep_alive = 1800

    def _candidates(self):
        from urllib.parse import urlparse
        base = self.base_url.rstrip("/")
        port = 11434
        try:
            p = urlparse(base)
            if p.port:
                port = p.port
        except Exception:
            pass
        alt_port = 11434 if port != 11434 else 11435
        wsl_ips = _wsl_host_ip()
        # Order matters: localhost variants first (fail fast, and the common case),
        # host-IP variants last (they can hang for seconds when unroutable).
        hosts = [base]
        if base != f"http://localhost:{port}":
            hosts.append(f"http://localhost:{port}")
        if base != f"http://localhost:{alt_port}":
            hosts.append(f"http://localhost:{alt_port}")
        hosts += [f"http://{ip}:{port}" for ip in wsl_ips]
        # If the configured port is wrong (e.g. .env says 11435 but Ollama listens on
        # the default 11434), probing only the configured port makes everything 503.
        # Also try the other common Ollama port on the WSL host IPs.
        hosts += [f"http://{ip}:{alt_port}" for ip in wsl_ips]
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
        """Quick readiness check: try primary URL, then WSL host IPs. Returns True
        only when reachable AND the model is installed. Capped so it can't hang."""
        import socket
        deadline = time.monotonic() + timeout
        connected_url = None
        # Try all candidate URLs (primary + WSL host IPs)
        for url in self._candidates():
            try:
                host = url.replace("http://", "").replace("https://", "")
                if ":" in host:
                    h, p = host.rsplit(":", 1)
                    port = int(p) if p.isdigit() else 11434
                else:
                    h, port = host, 11434
                with socket.create_connection((h, port), timeout=min(timeout, 1)):
                    connected_url = url
                    break
            except Exception:
                continue
        if not connected_url:
            return False
        if time.monotonic() > deadline:
            return False
        try:
            installed = requests.get(f"{connected_url}/api/tags", timeout=timeout)
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

    def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None) -> GenerationResult:
        payload = {
            "model": self.model, "system": system_prompt, "prompt": user_prompt,
            "stream": False, "keep_alive": self.keep_alive,
        }
        if max_tokens:
            payload["options"] = {"num_predict": int(max_tokens)}
        last_err = None
        # Ollama can take 30-60s to load a large model from disk on first call.
        # Use the full timeout per candidate instead of capping at 8s.
        per_try = max(int(timeout), 60)
        for url in self._candidates():
            started = time.monotonic()
            try:
                resp = requests.post(
                    f"{url}/api/generate", json=payload,
                    timeout=(min(self.connect_timeout, 1.5), per_try),
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

    def generate_stream(self, system_prompt, user_prompt, timeout=30, max_tokens=None):
        """Yield text chunks as the model generates (NDJSON streaming). Yields an empty
        string when done so callers know generation finished cleanly."""
        payload = {
            "model": self.model, "system": system_prompt, "prompt": user_prompt,
            "stream": True, "keep_alive": self.keep_alive,
        }
        if max_tokens:
            payload["options"] = {"num_predict": int(max_tokens)}
        last_err = None
        per_try = max(int(timeout), 60)
        for url in self._candidates():
            try:
                resp = requests.post(
                    f"{url}/api/generate", json=payload, stream=True,
                    timeout=(min(self.connect_timeout, 1.5), per_try),
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

            if resp.status_code == 404:
                raise RuntimeError(f"model '{self.model}' not found — run: ollama pull {self.model}")
            if not resp.ok:
                last_err = f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            try:
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise RuntimeError(data["error"])
                    chunk = (data.get("response") or "")
                    if chunk:
                        yield chunk
                    if data.get("done"):
                        yield ""
                        return
            except RuntimeError:
                raise
            except Exception as exc:
                last_err = str(exc)
                continue
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
        raise RuntimeError(last_err or "no endpoint reachable")