import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from core.llm.base import GenerationResult, ProviderState
from core.llm.ollama_provider import DEFAULT_MODEL as DEFAULT_OLLAMA_MODEL
from core.llm.ollama_provider import OllamaProvider
from core.llm.openrouter_provider import OpenRouterProvider

DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.1-8b-instruct"

# OpenRouter free-tier can echo a moderation verdict instead of a reply. Never treat that as a win.
_VERDICT_ECHO = re.compile(r"user safety\s*[:.\-]", re.I)


def _strip_prompt_echo(text):
    """Post-process LLM output: strip any prompt template the model echoed back."""
    if not text:
        return text
    for marker in ["User just said:", "Reply:", "Respond as Eloise.", "Context:",
                    "Conversation history:", "Recent conversation:", "Current board:"]:
        idx = text.find(marker)
        if idx >= 0:
            before = text[:idx].strip()
            if before:
                return before
    return text.strip()


def _ollama_base_url(raw=None):
    raw = raw if raw is not None else os.environ.get("OLLAMA_URL", "http://localhost:11434")
    raw = (raw or "").strip() or "http://localhost:11434"
    # If someone pastes a full endpoint like http://host:11435/api/generate, strip the path
    # so our own /api/tags and /api/generate calls land correctly.
    from urllib.parse import urlparse
    p = urlparse(raw)
    if p.scheme and p.hostname:
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{p.hostname}{port}"
    return raw


def build_providers(db=None, config=None):
    config = config or {}
    ollama = OllamaProvider(
        base_url=_ollama_base_url(),
        model=config.get("ollama_model") or os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
    )
    or_key = config.get("openrouter_key") or os.environ.get("OPENROUTER_API_KEY", "")
    or_model = config.get("openrouter_model") or os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    openrouter = OpenRouterProvider(api_key=or_key, model=or_model)
    return [ollama, openrouter]


def provider_config(db):
    merged = {}
    if db is not None:
        try:
            cur = db.execute("SELECT value FROM app_settings WHERE key='llm'")
            row = cur.fetchone()
            if row and row[0]:
                import json
                merged.update(json.loads(row[0]))
        except Exception:
            pass
    return merged


class LLMManager:
    def __init__(self, db=None):
        self._db = db
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._by_name = {}
        self._lock = threading.Lock()

    def _providers(self):
        cfg = provider_config(self._db if self._db is not None else None)
        with self._lock:
            by_name = {}
            for p in build_providers(config=cfg):
                by_name[p.name] = p
            self._by_name = by_name
            return by_name

    def any_usable(self):
        """Fast check: is any provider immediately usable? Avoids slow probes when none is up."""
        for p in self._providers().values():
            try:
                if p.name == "ollama":
                    if p.probe_fast():
                        return True
                elif p.status().usable:
                    return True
            except Exception:
                continue
        return False

    def generate(self, system_prompt, user_prompt, timeout=30) -> GenerationResult:
        import logging
        logger = logging.getLogger("eloise.llm")
        providers = self._providers()
        ordered = [providers.get("ollama"), providers.get("openrouter")]
        ordered = [p for p in ordered if p is not None]
        # Try every provider — even if status says non-usable, attempt generation anyway.
        # Status can be wrong (slow probe, transient failure), and a generate call is the
        # real test. Only skip providers that are plainly missing (e.g. no API key at all).
        for p in ordered:
            # Skip OpenRouter if no API key
            if p.name == "openrouter" and not getattr(p, "api_key", ""):
                logger.debug("skipping openrouter: no API key")
                continue
            logger.info("trying provider: %s (model: %s)", p.name, p.model)
            future = self._executor.submit(p.generate, system_prompt, user_prompt, timeout)
            try:
                result = future.result(timeout=timeout + 5)
            except Exception as exc:
                result = GenerationResult(ok=False, provider=p.name, model=p.model, error=str(exc))
            logger.info("provider %s: ok=%s error=%s latency=%s", p.name, result.ok, result.error, result.latency_ms)
            if result.ok:
                return result
        return GenerationResult(
            ok=False, provider="none", model="none",
            error="no usable provider (start local model or add an API key)",
        )

    def generate_with_fallback(self, system_prompt, user_prompt, fallback_fn, timeout=90):
        result = self.generate(system_prompt, user_prompt, timeout=timeout)
        if result.ok and not _VERDICT_ECHO.search(result.text[:80]):
            # Strip any prompt template the model echoed back
            cleaned = _strip_prompt_echo(result.text)
            return cleaned, result.provider or "llm"
        if result.ok:
            result = GenerationResult(ok=False, provider=result.provider, model=result.model,
                                      error="provider echoed a moderation verdict")
        fallback_text = fallback_fn()
        return fallback_text, "fallback"

    def statuses(self):
        providers = self._providers()
        out = []
        for p in providers.values():
            try:
                out.append(p.status())
            except Exception as exc:
                from core.llm.base import ProviderStatus
                out.append(ProviderStatus(name=p.name, state=ProviderState.UNREACHABLE, detail=str(exc)))
        return out

    def status_dict(self):
        statuses = self.statuses()
        by_name = {s.name: s for s in statuses}
        ollama = by_name.get("ollama")
        openrouter = by_name.get("openrouter")
        return {
            "local_reachable": ollama is not None and ollama.state != ProviderState.UNREACHABLE,
            "local_model_available": ollama is not None and ollama.state == ProviderState.READY,
            "ollama_model": (ollama.model if ollama else None),
            "ollama_url": _ollama_base_url(),
            "ollama_error": (None if ollama and ollama.usable else (ollama.detail if ollama else None)),
            "cloud_configured": openrouter is not None and openrouter.state == ProviderState.READY,
            "openrouter_model": (openrouter.model if openrouter else None),
            "openrouter_error": (openrouter.detail if openrouter and not openrouter.usable else None),
        }