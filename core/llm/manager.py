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
# generation per loaded model at a time. Every chat request and every plan draw shares
# ONE lock per MODEL so a busy box becomes a clean queue instead of a stampede of
# competing calls that slow each other down and then time out. Crucially, chat and plans
# can run on DIFFERENT local models (OLLAMA_CHAT_MODEL=1.7b vs the 4b used for plans),
# and Ollama serves them concurrently — so a slow plan draw on the big model must NOT
# starve an interactive chat into OpenRouter. Callers wait on their model's lock with a
# cap (lock_wait): a plan draw knows its own job is background work and can queue longer,
# but an interactive CHAT must never stare at a spinner for a minute+ behind a plan —
# past its wait cap it falls back to OpenRouter and answers now.
OLLAMA_LOCK = threading.Lock()
_ollama_model_locks = {}
_ollama_locks_guard = threading.Lock()


def ollama_lock_for(effective_model=None):
    """The app-wide lock for the given local model name. Models key on their own lock, so
    a chat on the small fast model is served in parallel with a plan draw on the big
    model (Ollama keeps one runner per loaded model). Falls back to the legacy shared
    lock when the model is unknown."""
    if not effective_model:
        return OLLAMA_LOCK
    with _ollama_locks_guard:
        return _ollama_model_locks.setdefault(effective_model, threading.Lock())


def _strip_prompt_echo(text, user_message=None, user_lines=None):
    """Post-process LLM output: remove any prompt boilerplate the model echoed back,
    drop a leading role label it parroted from history ('Eloise: ...'), refuse a
    verbatim echo of the user's own question, delete verbatim re-quotes of the
    user's earlier lines, and strip sycophancy openers ('You're right—', 'I
    understand...') that weak models sprinkle in pretending to agree. Weak local
    models do all of these; without this the UI shows the user their own words
    played back at them with a smiley agree-first wrapper ('echoing')."""
    if not text:
        return ""
    t = text
    # Cut everything up to the LAST prompt/echo marker and keep only the tail — that's
    # where the actual reply lives after the model re-prints the instructions/history.
    markers = [
        "Reply as Eloise.", "Respond as Eloise.", "Only output your reply.",
        "do NOT echo this prompt", "Do NOT echo this prompt",
        "=== GOAL ===", "=== CONVERSATION ===", "=== BOARD STATE ===",
        "=== THE PLAN", "=== ", "User just said:", "Context:",
        "Conversation history:", "Recent conversation:", "Current board:",
    ]
    cut = 0
    for marker in markers:
        idx = t.rfind(marker)
        if idx >= 0:
            cut = max(cut, idx + len(marker))
    if cut:
        t = t[cut:]
    lines = t.splitlines()
    for i in range(len(lines)):
        m = re.match(r"^(eloise|assistant|user)\s*[:|]\s*(.*)$", lines[i].strip(), re.I)
        if m and (m.group(2) or "").strip():
            lines[i] = m.group(2).strip()
        else:
            break
    lines = [ln for ln in lines if ln.strip() != ""]
    t = "\n".join(lines).strip()
    if not t:
        return ""
    # Strip verbatim re-quotes of the user's EARLIER lines ("'Nice. Let's move
    # forward.'" played back in the reply). The excerpt can be wrapped in quotes or
    # markdown bold/italic, and a weak model often quotes just the closing TAIL of
    # the line, so try every suffix too (e.g. the imperative "Let's move forward.").
    for ul in (user_lines or []):
        ul = (ul or "").strip()
        if not ul:
            continue
        tokens = ul.split()
        variants = []
        for i in range(len(tokens)):
            variants.append(" ".join(tokens[i:]))
        variants.append(ul)
        for variant in variants:
            if len(variant) < 6:
                continue
            padded = " " + variant.strip("?!. ") + " "
            for wrapped in (variant, f'"{variant}"', f"'{variant}'", f"**{variant}**", f"*{variant}*"):
                if wrapped in t:
                    t = t.replace(wrapped, " ").strip()
                    break
            if not t:
                return ""
    # Refuse a verbatim/near-verbatim copy of the question the user just asked.
    if user_message:
        def norm(s):
            return re.sub(r"\s+", " ", (s or "").strip().lower()).strip(".:?! \"'")

        body = re.sub(r"^(eloise|assistant|user)\s*[:|]\s*", " ", t, flags=re.I)
        if norm(body) == norm(user_message) or norm(body).startswith(norm(user_message)):
            return ""
    # Strip sycophancy openers: the weak-model answer to "i don't like X" is not a
    # real reply — it's "You're right—..." + a rephrase of the user's own words.
    _SYC_OPENER = re.compile(
        r"^\s*(?:you['’]?re\s+right|you\s+are\s+right|i\s+understand|"
        r"great\s+(?:point|question|stuff)|that['’]?s\s+(?:a\s+)?great|"
        r"what\s+a\s+great|thanks\s+for\s+(?:saying|asking|sharing|the)|"
        r"i\s+hear\s+you|you\s+make\s+(?:a\s+)?(?:good|great)|"
        r"you\s+said\s+it|that['’]?s\s+(?:a\s+)?fair)\s*[^\w]*\s*",
        re.I,
    )
    t2 = _SYC_OPENER.sub("", t, count=1).strip(" .,:;-—*_\"'\n\t").strip()
    if t2:
        t = t2
    if not t:
        return ""
    return t


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
                # The local box runs one generation per loaded model at a time. Wait
                # HERE (before the timeout starts counting) so a queued call still gets
                # its FULL timeout instead of burning its budget sitting behind a busy
                # predecessor — but only for up to lock_wait seconds. Chat and plans
                # each hold their OWN model's lock (e.g. 1.7b vs 4b), so they run
                # concurrently. If the configured CHAT model is missing (404) or errors,
                # retry on the PLAN model — still local — before ever opening the cloud.
                if model_override is None:
                    attempts = [None]
                else:
                    attempts = [model_override, p.model]
                result = None
                for attempt in attempts:
                    eff = attempt or p.model
                    lock = ollama_lock_for(eff)
                    if not lock.acquire(timeout=lock_wait):
                        logger.warning("ollama busy >%.0fs (lock) for %s", lock_wait, eff)
                        continue
                    try:
                        result = self._attempt(p, system_prompt, user_prompt, timeout,
                                               max_tokens, attempt)
                    finally:
                        lock.release()
                    if result.ok:
                        break
                if result is None or not result.ok:
                    continue
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
                    # The CPU box runs one generation per loaded model; hold THIS
                    # model's lock for the whole stream so later callers queue behind
                    # it, but only up to lock_wait. Chat (1.7b) and plans (4b) hold
                    # different per-model locks so they run concurrently. If the
                    # configured CHAT model is missing or a stream dies, retry on the
                    # PLAN model (still local) before ever falling back to the cloud —
                    # the user wants local chat, not OpenRouter output.
                    if model_override is None:
                        attempts = [None]
                    else:
                        attempts = [model_override, p.model]
                    last = None
                    for attempt in attempts:
                        eff = attempt or p.model
                        lock = ollama_lock_for(eff)
                        if not lock.acquire(timeout=lock_wait):
                            logger.warning("ollama busy >%.0fs (chat lock) for %s", lock_wait, eff)
                            last = RuntimeError(f"ollama busy ({eff})")
                            continue
                        try:
                            for chunk in stream(system_prompt, user_prompt, timeout, max_tokens, attempt):
                                if chunk is None:
                                    yield "", p.name
                                    return
                                yield chunk, p.name
                            yield "", p.name
                            return
                        except Exception as exc:
                            logger.warning("ollama stream failed on %s: %s", eff, exc)
                            last = exc
                        finally:
                            lock.release()
                    if last is not None:
                        continue
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