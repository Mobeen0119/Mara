import os
import threading
from concurrent.futures import ThreadPoolExecutor

from core.llm.base import GenerationResult, ProviderState
from core.llm.ollama_provider import DEFAULT_MODEL as DEFAULT_OLLAMA_MODEL
from core.llm.ollama_provider import OllamaProvider
from core.llm.openrouter_provider import OpenRouterProvider

DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.1-8b-instruct:free"


def _ollama_base_url(raw=None):
    raw = raw if raw is not None else os.environ.get("OLLAMA_URL", "http://localhost:11434")
    return raw.strip() or "http://localhost:11434"


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

    def generate(self, system_prompt, user_prompt, timeout=30) -> GenerationResult:
        providers = self._providers()
        ordered = [providers.get("ollama"), providers.get("openrouter")]
        ordered = [p for p in ordered if p is not None]
        for p in ordered:
            future = self._executor.submit(p.generate, system_prompt, user_prompt, timeout)
            try:
                result = future.result(timeout=timeout + 5)
            except Exception as exc:
                result = GenerationResult(ok=False, provider=p.name, model=p.model, error=str(exc))
            if result.ok:
                return result
        return result if 'result' in locals() else GenerationResult(
            ok=False, provider="none", model="none", error="no providers available")

    def generate_with_fallback(self, system_prompt, user_prompt, fallback_fn, timeout=30):
        result = self.generate(system_prompt, user_prompt, timeout=timeout)
        if result.ok:
            return result.text, "llm"
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