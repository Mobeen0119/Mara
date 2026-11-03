"""'Paste any key and go' + 'the persona never changes with the provider'.
Real providers are replaced by local mock servers that speak each wire format."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from core.persona import BASE_SYSTEM_PROMPT
from tests.conftest import auth, signup

GOOD = "mock-good-key-1234567890"


class Mock:
    def __init__(self, models=("text-embedding-x", "big-model-70b", "tiny-mini-8b"), quirks=()):
        self.requests, self.quirks, self.models = [], set(quirks), list(models)
        mock = self

        class H(BaseHTTPRequestHandler):
            def _auth_ok(self):
                key = self.headers.get("x-api-key") or (self.headers.get("Authorization") or "").replace("Bearer ", "")
                return key == GOOD

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code); self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                if not self._auth_ok():
                    return self._send(401, {"error": "bad key"})
                self._send(200, {"data": [{"id": m} for m in mock.models]})

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n))
                mock.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if not self._auth_ok():
                    return self._send(401, {"error": "bad key"})
                if self.path.endswith("/messages"):
                    return self._send(200, {"content": [{"type": "text", "text": "claude says hi"}]})
                if "max_completion" in mock.quirks and "max_tokens" in body:
                    return self._send(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}})
                if "no_system" in mock.quirks and any(m["role"] == "system" for m in body["messages"]):
                    return self._send(400, {"error": {"message": "Developer instruction is not enabled for this model"}})
                self._send(200, {"choices": [{"message": {"content": "hello from mock"}}]})

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.srv.server_port}"

    def stop(self):
        self.srv.shutdown()


@pytest.fixture()
def mock(monkeypatch):
    monkeypatch.setenv("ELOISE_ALLOW_PRIVATE_LLM", "1")
    m = Mock()
    yield m
    m.stop()


# ---------------------------------------------------------------- detection
@pytest.mark.parametrize("key,expected", [
    ("sk-ant-api03-abc", "anthropic"), ("sk-or-v1-abc", "openrouter"), ("AIzaSyABC", "gemini"),
    ("gsk_abc", "groq"), ("csk-abc", "cerebras"), ("xai-abc", "xai"), ("pplx-abc", "perplexity"),
    ("fw_abc", "fireworks"), ("nvapi-abc", "nvidia"), ("hf_abc", "huggingface"), ("sk-proj-abc", "openai"),
])
def test_key_prefix_identifies_provider(key, expected):
    from core.llm.keys import candidates_for_key
    assert candidates_for_key(key) == [expected]


def test_ambiguous_keys_are_probed_in_priority_order():
    from core.llm.keys import candidates_for_key
    assert candidates_for_key("sk-" + "a" * 32)[:2] == ["deepseek", "openai"]
    assert candidates_for_key("a" * 64) == ["together"]
    assert candidates_for_key("Z" * 32)[0] == "mistral"
    assert len(candidates_for_key("totally-unknown-shape")) >= 10        # probe everything


def test_pick_model_prefers_vetted_default_then_cheap_chat_models():
    from core.llm.keys import pick_model
    assert pick_model("groq", ["whisper-large", "llama-3.3-70b-versatile", "x"]) == "llama-3.3-70b-versatile"
    assert pick_model("custom", ["text-embedding-3", "whisper-1", "big-model-70b", "tiny-mini-8b"]) == "tiny-mini-8b"
    assert pick_model("openai", ["text-embedding-3", "dall-e-3"]) == "gpt-4o-mini"   # nothing chat-like: vetted default


def test_autoconfigure_verifies_key_and_picks_model_from_the_account(mock):
    from core.llm.keys import autoconfigure
    r = autoconfigure(GOOD, base_url=mock.url + "/v1")
    assert r["ok"] and r["verified"] and r["model"] == "tiny-mini-8b" and "text-embedding-x" in r["models"]
    bad = autoconfigure("wrong-key", base_url=mock.url + "/v1")
    assert not bad["ok"] and bad["tried"][0]["status"] == "auth"


def test_anthropic_protocol_selected_and_native_headers_used(mock):
    from core.llm.keys import autoconfigure
    from core.llm.manager import build_providers
    r = autoconfigure(GOOD, base_url=mock.url, provider="anthropic")
    assert r["ok"] and r["protocol"] == "anthropic"
    p = build_providers(config={"api_key": GOOD, "base_url": r["base_url"], "model": "claude-x",
                                "provider": "anthropic", "protocol": "anthropic"})[0]
    res = p.generate(BASE_SYSTEM_PROMPT, "hello", max_tokens=50)
    assert res.ok and res.text == "claude says hi"
    sent = mock.requests[-1]
    assert sent["path"] == "/v1/messages" and sent["headers"]["x-api-key"] == GOOD
    assert "anthropic-version" in {k.lower() for k in sent["headers"]}
    assert sent["body"]["system"] == BASE_SYSTEM_PROMPT and sent["body"]["max_tokens"] == 50


# ------------------------------------------------- one persona, every provider
def _system_text_sent(req):
    body = req["body"]
    if "system" in body:
        return body["system"]
    msgs = body["messages"]
    return msgs[0]["content"] if msgs[0]["role"] == "system" else msgs[0]["content"]


def test_identical_persona_reaches_openai_style_and_anthropic_style(mock):
    from core.llm.manager import build_providers
    oa = build_providers(config={"api_key": GOOD, "base_url": mock.url + "/v1", "model": "m"})[0]
    an = build_providers(config={"api_key": GOOD, "base_url": mock.url, "model": "m", "protocol": "anthropic"})[0]
    assert oa.generate(BASE_SYSTEM_PROMPT, "u", max_tokens=9).ok and an.generate(BASE_SYSTEM_PROMPT, "u", max_tokens=9).ok
    a, b = mock.requests[-2], mock.requests[-1]
    assert a["body"]["messages"][0] == {"role": "system", "content": BASE_SYSTEM_PROMPT}
    assert b["body"]["system"] == BASE_SYSTEM_PROMPT
    for needle in ("sharp-tongued", "mum whose patience expired", "swear lightly"):
        assert needle in a["body"]["messages"][0]["content"] and needle in b["body"]["system"]


def test_models_that_refuse_a_system_role_still_get_the_whole_persona(monkeypatch):
    monkeypatch.setenv("ELOISE_ALLOW_PRIVATE_LLM", "1")
    m = Mock(quirks={"no_system"})
    try:
        from core.llm.manager import build_providers
        p = build_providers(config={"api_key": GOOD, "base_url": m.url + "/v1", "model": "gemma-like"})[0]
        r = p.generate(BASE_SYSTEM_PROMPT, "what now", max_tokens=20)
        assert r.ok
        last = m.requests[-1]["body"]["messages"]
        assert len(last) == 1 and last[0]["role"] == "user"
        assert last[0]["content"].startswith(BASE_SYSTEM_PROMPT) and last[0]["content"].endswith("what now")
        n = len(m.requests)
        assert p.generate(BASE_SYSTEM_PROMPT, "again", max_tokens=20).ok
        assert len(m.requests) == n + 1, "quirk must be remembered, not re-discovered every call"
    finally:
        m.stop()


def test_models_that_want_max_completion_tokens_are_adapted(monkeypatch):
    monkeypatch.setenv("ELOISE_ALLOW_PRIVATE_LLM", "1")
    m = Mock(quirks={"max_completion"})
    try:
        from core.llm.manager import build_providers
        p = build_providers(config={"api_key": GOOD, "base_url": m.url + "/v1", "model": "o-series"})[0]
        assert p.generate("s", "u", max_tokens=33).ok
        assert m.requests[-1]["body"].get("max_completion_tokens") == 33 and "max_tokens" not in m.requests[-1]["body"]
    finally:
        m.stop()


# ----------------------------------------- end to end: paste key -> chat uses persona
@pytest.mark.parametrize("provider", [None, "anthropic"])
def test_paste_only_a_key_then_chat_with_full_persona(client, mock, provider):
    admin = signup(client, "admin@example.com", "Admin")
    h = auth(admin["token"])
    body = {"api_key": GOOD, "base_url": (mock.url if provider else mock.url + "/v1"), "model": "tiny-mini-8b"}
    if provider:
        body["provider"] = provider
    r = client.put("/api/settings/llm", headers=h, json=body)
    assert r.status_code == 200 and r.json()["verified"] is True, r.text
    assert r.json()["protocol"] == ("anthropic" if provider else "openai")
    assert client.post("/api/settings/llm/test", headers=h, json={}).json()["ok"] is True
    chat = client.post("/api/chat", headers=h, json={"message": "I keep procrastinating, help"})
    assert chat.status_code == 200, chat.text
    sent = [q for q in mock.requests if q["path"].endswith(("/chat/completions", "/messages"))][-1]
    assert _system_text_sent(sent).startswith(BASE_SYSTEM_PROMPT)
    # the key is never echoed back
    assert GOOD not in json.dumps(client.get("/api/settings/llm", headers=h).json())
    assert GOOD not in json.dumps(client.get("/api/settings", headers=h).json())


def test_rejected_key_is_not_saved(client, mock):
    admin = signup(client, "admin@example.com", "Admin")
    r = client.put("/api/settings/llm", headers=auth(admin["token"]), json={"api_key": "nope", "base_url": mock.url + "/v1"})
    assert r.status_code == 400
    assert client.get("/api/settings/llm", headers=auth(admin["token"])).json()["api_key_set"] is False


def test_manager_is_shared_so_settings_requests_do_not_leak_thread_pools():
    from core.llm.manager import LLMManager
    assert LLMManager()._executor is LLMManager()._executor
