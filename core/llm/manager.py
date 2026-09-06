import logging
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

# Ollama (the local model) is a SINGLE CPU-bound process: it can only run one
# generation at a time. Every chat request and every plan draw shares this one lock,
# so a busy box becomes a clean queue instead of a stampede of competing calls that
# slow each other down and then time out. Callers wait on the lock with a cap
# (lock_wait): a plan draw knows its own job is background work and can queue longer,
# but an interactive CHAT must never stare at a spinner for a minute+ behind a plan —
# past its wait cap it falls back to OpenRouter and answers now.
OLLAMA_LOCK = threading.Lock()


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

    def _ordered(self, prefer_cloud=False):
        """Provider attempt order, per use case:
        - Plans (prefer_cloud=True): OpenRouter FIRST — cloud runs in parallel with the
          local chat model, so a schedule draw never blocks (or is blocked by) chat.
          Falls back to the local model when no key is set or the cloud fails.
        - Chat (default/prefer_cloud=False): the LOCAL model FIRST (the user explicitly
          wants local output for his conversations); OpenRouter only as a fallback."""
        providers = self._providers()
        o, l = providers.get("openrouter"), providers.get("ollama")
        if prefer_cloud:
            return [p for p in (o, l) if p is not None]
        return [p for p in (l, o) if p is not None]

    def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None,
                 prefer_cloud=False, model_override=None, lock_wait=60) -> GenerationResult:
        import logging
        logger = logging.getLogger("eloise.llm")
        ordered = self._ordered(prefer_cloud=prefer_cloud)
        # Try every provider — even if status says non-usable, attempt generation anyway.
        # Status can be wrong (slow probe, transient failure), and a generate call is the
        # real test. Only skip providers that are plainly missing (e.g. no API key at all).
        for p in ordered:
            # Skip OpenRouter if no API key
            if p.name == "openrouter" and not getattr(p, "api_key", ""):
                logger.debug("skipping openrouter: no API key")
                continue
            logger.info("trying provider: %s (model: %s)", p.name, p.model)
            if p.name == "ollama":
                # The local box runs one generation at a time. Wait HERE (before the
                # timeout starts counting) so a queued call still gets its FULL timeout
                # instead of burning its budget sitting behind a busy predecessor —
                # but only for up to lock_wait seconds. A long plan draw holding the
                # lock must not freeze a chat beyond that; then we move on to the
                # next provider.
                if not OLLAMA_LOCK.acquire(timeout=lock_wait):
                    logger.warning("ollama busy >%.0fs (lock); skipping to next provider", lock_wait)
                    continue
                try:
                    result = self._attempt(p, system_prompt, user_prompt, timeout, max_tokens,
                                           model_override)
                finally:
                    OLLAMA_LOCK.release()
            else:
                result = self._attempt(p, system_prompt, user_prompt, timeout, max_tokens,
                                       model_override)
            logger.info("provider %s: ok=%s error=%s latency=%s", p.name, result.ok, result.error, result.latency_ms)
            if result.ok:
                return result
        return GenerationResult(
            ok=False, provider="none", model="none",
            error="no usable provider (start local model or add an API key)",
        )

    def _attempt(self, p, system_prompt, user_prompt, timeout, max_tokens, model_override=None):
        if model_override is None:
            future = self._executor.submit(
                p.generate, system_prompt, user_prompt, timeout, max_tokens
            )
        else:
            future = self._executor.submit(
                p.generate, system_prompt, user_prompt, timeout, max_tokens, model_override
            )
        try:
            return future.result(timeout=timeout + 5)
        except Exception as exc:
            return GenerationResult(ok=False, provider=p.name, model=p.model, error=str(exc))

    def generate_with_fallback(self, system_prompt, user_prompt, fallback_fn, timeout=90,
                               max_tokens=None, prefer_cloud=False, model_override=None,
                               lock_wait=60):
        result = self.generate(system_prompt, user_prompt, timeout=timeout,
                               max_tokens=max_tokens, prefer_cloud=prefer_cloud,
                               model_override=model_override, lock_wait=lock_wait)
        if result.ok and not _VERDICT_ECHO.search(result.text[:80]):
            # Strip any prompt template the model echoed back
            cleaned = _strip_prompt_echo(result.text)
            return cleaned, result.provider or "llm"
        if result.ok:
            result = GenerationResult(ok=False, provider=result.provider, model=result.model,
                                      error="provider echoed a moderation verdict")
        fallback_text = fallback_fn()
        return fallback_text, "fallback"

    def stream_generate(self, system_prompt, user_prompt, timeout=90, max_tokens=None,
                        prefer_cloud=False, model_override=None, lock_wait=12):
        """Yield (chunk_text, provider_name) as the first usable provider streams.
        After the last chunk, yields ('', provider_name) to signal clean completion.
        Raises RuntimeError if no provider can stream. Interactive chat caps the
        lock wait hard (lock_wait=12s): a background plan draw holding the local box
        must not freeze a conversation, so past the cap we stream from OpenRouter."""
        ordered = self._ordered(prefer_cloud=prefer_cloud)
        logger = logging.getLogger("eloise.llm")
        for p in ordered:
            if p.name == "openrouter" and not getattr(p, "api_key", ""):
                continue
            stream = getattr(p, "generate_stream", None)
            if stream is None:
                continue
            logger.info("streaming from provider: %s (model: %s)", p.name, p.model)
            try:
                if p.name == "ollama":
                    # The CPU box is busy until this stream is DONE, so the app-wide
                    # lock is held for the whole stream; later callers queue behind it,
                    # but only up to their own lock_wait cap.
                    if not OLLAMA_LOCK.acquire(timeout=lock_wait):
                        logger.warning("ollama busy >%.0fs (chat lock); streaming from next provider", lock_wait)
                        continue
                    try:
                        chunk = ""
                        for chunk in stream(system_prompt, user_prompt, timeout, max_tokens, model_override):
                            if chunk is None:
                                yield "", p.name
                                return
                            yield chunk, p.name
                        yield "", p.name
                        return
                    finally:
                        OLLAMA_LOCK.release()
                chunk = ""
                for chunk in stream(system_prompt, user_prompt, timeout, max_tokens, model_override):
                    if chunk is None:
                        yield "", p.name
                        return
                    yield chunk, p.name
                yield "", p.name
                return
            except RuntimeError as exc:
                logger.warning("provider %s stream failed: %s", p.name, exc)
                continue
            except Exception as exc:
                logger.warning("provider %s stream errored: %s", p.name, exc)
                continue
        raise RuntimeError("no usable provider (start local model or add an API key)")

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