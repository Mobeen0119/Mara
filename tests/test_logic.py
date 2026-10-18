import json

from core import generation


def test_time_windows():
    assert generation.parse_time_window("11pm-7am") == (23*60, 7*60 + 24*60)
    assert generation.parse_time_window("3pm-5pm")[0] == 15*60
    assert generation.parse_time_window("gym 5-7pm") == (17*60, 19*60)


def test_days_remaining():
    assert generation.days_remaining("2026-09-10", today="2026-09-03") == 7


def test_completion_detection():
    assert generation.completion_detected("all finished")
    assert generation.completion_detected("I am done")
    assert not generation.completion_detected("why do I always leave things for the last second")


def test_fallback_plan_shape():
    goal = {
        "deadline": "2026-09-20",
        "display_title": "Study",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    entries = generation._build_fallback_plan(goal)
    assert len(entries) >= 1
    assert all(e["date"] and e["title"] for e in entries)


# --- Provider presets: "paste a key and go" ---------------------------------

def test_provider_presets_are_complete():
    from core.llm.openai_provider import PROVIDER_PRESETS, PRESET_BY_ID
    assert len(PROVIDER_PRESETS) >= 6
    ids = set()
    for p in PROVIDER_PRESETS:
        assert p["id"] and p["label"] and p["model"]
        assert p["url"].startswith("https://")
        assert p["id"] not in ids, f"duplicate preset id {p['id']}"
        ids.add(p["id"])
        assert PRESET_BY_ID[p["id"]] is p


def test_preset_inference_from_url_and_key():
    from core.llm.openai_provider import preset_for_api_key, preset_for_base_url
    assert preset_for_base_url("https://openrouter.ai/api/v1") == "openrouter"
    url = "https://generativelanguage.googleapis.com/v1beta/openai"
    assert preset_for_base_url(url) == "gemini"
    assert preset_for_base_url("https://myproxy.example/v1") is None
    assert preset_for_base_url("") is None
    # sk-or- must win over OpenAI's generic sk- prefix
    assert preset_for_api_key("sk-or-v1-abc") == "openrouter"
    assert preset_for_api_key("AIzaSyFakeKey") == "gemini"
    assert preset_for_api_key("gsk_abc") == "groq"
    assert preset_for_api_key("csk-abc") == "cerebras"
    assert preset_for_api_key("sk-abc") == "openai"
    assert preset_for_api_key("") is None


def test_clean_base_url_strips_chat_suffix():
    from core.llm.openai_provider import PROVIDER_PRESETS, OpenAICompatProvider, clean_base_url
    suffix = "https://openrouter.ai/api/v1/chat/completions"
    assert clean_base_url(suffix) == "https://openrouter.ai/api/v1"
    assert clean_base_url("https://x.example/v1/") == "https://x.example/v1"
    assert clean_base_url("") == "https://openrouter.ai/api/v1"
    # Every preset must survive cleaning with its /v1 (or /api/v1) intact —
    # stripping it turns each request into a guaranteed 404.
    for p in PROVIDER_PRESETS:
        prov = OpenAICompatProvider(api_key="k", model=p["model"], base_url=p["url"])
        assert prov._url() == p["url"] + "/chat/completions", p["id"]


def test_build_providers_prefers_saved_settings_over_env(monkeypatch):
    from core.llm.manager import build_providers
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    providers = build_providers(config={
        "api_key": "cfg-key",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "provider": "groq",
    })
    assert len(providers) == 1
    p = providers[0]
    assert p.api_key == "cfg-key"
    assert "groq" in p.base_url
    assert p.model == "llama-3.3-70b-versatile"
    assert p.name == "groq"


def _fresh_db(tmp_path):
    """Isolated DB for route tests: never touch the live storage dir."""
    import core.database as dbm
    old = getattr(dbm._thread_local, "conn", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    dbm._thread_local.conn = None
    dbm.set_storage_dir(str(tmp_path))
    return dbm.get_connection()


def test_llm_settings_roundtrip_provider_switch_and_test(tmp_path, monkeypatch):
    from core.llm.base import GenerationResult
    from core.models import LLMSettingsRequest, LLMTestRequest
    from core.routes.settings_routes import get_llm, put_llm, test_llm

    conn = _fresh_db(tmp_path)
    user = {"id": 1}

    # Provider-only save: the preset supplies endpoint + model.
    put_llm(body=LLMSettingsRequest(provider="gemini", api_key="AIzaTestKey"), user=user)
    cfg = json.loads(conn.execute(
        "SELECT value FROM app_settings WHERE key='llm'").fetchone()[0])
    assert cfg["provider"] == "gemini"
    assert cfg["base_url"].startswith("https://generativelanguage.googleapis.com")
    assert cfg["model"] == "gemini-3.5-flash-lite"
    assert cfg["api_key"] == "AIzaTestKey"

    # Switching provider can never inherit the previous provider's model.
    put_llm(body=LLMSettingsRequest(provider="groq", api_key="gsk_test"), user=user)
    cfg = json.loads(conn.execute(
        "SELECT value FROM app_settings WHERE key='llm'").fetchone()[0])
    assert cfg["base_url"] == "https://api.groq.com/openai/v1"
    assert cfg["model"] == "llama-3.3-70b-versatile"

    # Test endpoint: blank fields resolve from saved settings (no network —
    # the provider's generate is faked to prove wiring, not connectivity).
    def fake_generate(self, sys_prompt, user_prompt, timeout=30, max_tokens=None,
                      model_override=None):
        return GenerationResult(ok=True, provider=self.name, model=self.model,
                                text="ok", latency_ms=5)
    monkeypatch.setattr(
        "core.routes.settings_routes.OpenAICompatProvider.generate", fake_generate)
    r = test_llm(body=LLMTestRequest(), user=user)
    assert r["ok"] is True
    assert r["provider"] == "groq"
    assert r["model"] == "llama-3.3-70b-versatile"

    # Overrides in the test body win over saved settings (test before saving).
    r = test_llm(body=LLMTestRequest(provider="gemini", api_key="AIzaOther",
                                     base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                                     model="gemini-3.5-flash"), user=user)
    assert r["ok"] is True
    assert r["provider"] == "gemini"
    assert r["model"] == "gemini-3.5-flash"
    # Saved settings were NOT touched by the probe.
    cfg = json.loads(conn.execute(
        "SELECT value FROM app_settings WHERE key='llm'").fetchone()[0])
    assert cfg["model"] == "llama-3.3-70b-versatile"

    # No key anywhere: honest failure, never a network call.
    put_llm(body=LLMSettingsRequest(clear_key=True), user=user)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    r = test_llm(body=LLMTestRequest(), user=user)
    assert r["ok"] is False
    assert "API key" in (r["error"] or "")

    # GET reports the truth the UI needs: inference + key source.
    put_llm(body=LLMSettingsRequest(base_url="https://api.groq.com/openai/v1",
                                    model="llama-3.3-70b-versatile",
                                    api_key="gsk_back"), user=user)
    out = get_llm(user=user)
    assert out["provider"] == "groq"          # inferred from base URL
    assert out["api_key_set"] is True
    assert out["key_source"] == "settings"
    assert any(p["id"] == "gemini" for p in out["presets"])


# --- Guardrail: precise, word-boundary, partner-friendly ---------------------

def test_guardrail_word_boundaries_no_false_positives():
    h = generation._is_truly_harmful
    # Substring traps that used to refuse benign messages wholesale.
    assert not h("I need to document my progress every day")     # 'cum' in document
    assert not h("every person on my team will review it")       # 'son' in person
    assert not h("tell me the reason I keep procrastinating")    # 'son' in reason
    assert not h("choose between the two options")               # 'teen' in between
    assert not h("I want to grow grapes this season")            # 'rape' in grapes
    assert not h("let's plan a family trip to the mountains")    # family, no sex
    assert not h("this fucking deadline is killing me")          # swearing only


def test_guardrail_allows_partner_intimacy_and_wellness():
    h = generation._is_truly_harmful
    assert not h("plan a romantic night with my wife")
    assert not h("how do I talk to my girlfriend about sex")
    assert not h("guide me about masturbation, is it normal")
    assert not h("I want to fuck my girlfriend on our anniversary")
    assert not h("book a hotel for our honeymoon")


def test_guardrail_still_refuses_the_real_thing():
    h = generation._is_truly_harmful
    assert h("I want to fuck my sister")
    assert h("rape her")
    assert h("how to drug her and fuck her")
    assert h("guide me to have sex with my 14-year-old daughter")
    assert h("kill my boss")
    # Thread-aware: a follow-up after an abuse goal is still refused.
    assert h("tell me more", context="user: i want to fuck my sister\neloise: no.\n")


def test_persona_anti_leak_and_intimacy_policy():
    from core.persona import BASE_SYSTEM_PROMPT
    p = BASE_SYSTEM_PROMPT
    assert "IDENTITY:" in p
    assert "prompt-extraction" in p.lower()
    assert "INTIMACY:" in p
    assert "LOCATION:" in p


# --- Location awareness ------------------------------------------------------

def test_location_settings_and_prompt_wiring(tmp_path):
    from core.models import LocationRequest
    from core.routes.settings_routes import get_location, put_location
    conn = _fresh_db(tmp_path)
    conn.execute("INSERT INTO users (name, password_hash) VALUES ('T', 'x')")
    conn.commit()
    user = dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone())
    # Column default: this product ships location-aware out of the box.
    assert get_location(user=user)["location"] == "Pakistan"
    r = put_location(body=LocationRequest(location="Lahore, Pakistan"), user=user)
    assert r["location"] == "Lahore, Pakistan"
    user = dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone())
    assert get_location(user=user)["location"] == "Lahore, Pakistan"
    # Prompt wiring: location reaches both the chat system prompt and the
    # standalone persona/location composer.
    assert generation.user_location(conn, 1) == "Lahore, Pakistan"
    sys = generation.chat_system_prompt(conn, 1)
    assert "Lahore, Pakistan" in sys
    sys2 = generation._with_persona(generation.BASE_SYSTEM_PROMPT, "", location="Karachi")
    assert "Karachi" in sys2


# --- Clarifying questions: model-written, honest fallback --------------------

def test_clarify_questions_fallback_and_model_path(monkeypatch):
    class _Dead:
        def generate(self, *a, **k):
            raise RuntimeError("model down")
    monkeypatch.setattr("core.generation.get_manager", lambda db=None: _Dead())
    qs = generation.generate_clarify_questions("Learn networking for the CCNA exam")
    assert len(qs) >= 2
    assert all(q.get("q") and q.get("key") for q in qs)

    from core.llm.base import GenerationResult

    class _Live:
        def generate(self, *a, **k):
            return GenerationResult(
                ok=True, provider="test", model="m", latency_ms=3,
                text='[{"key":"outcome","q":"What does done look like?","hint":"e.g. ship it"}]',
            )
    monkeypatch.setattr("core.generation.get_manager", lambda db=None: _Live())
    qs = generation.generate_clarify_questions("Launch my startup")
    assert qs[0]["key"] == "outcome"
    assert "done look like" in qs[0]["q"]


# --- Repetition guard --------------------------------------------------------

def test_sentence_dedupe_in_prompt_echo_strip():
    from core.llm.manager import _strip_prompt_echo
    t = _strip_prompt_echo("Get to work now. Get to work now. Stop planning and do it.")
    assert t.count("Get to work") == 1
    assert "Stop planning" in t


# --- Overdue tasks are marked failed and still toggleable --------------------

def test_overdue_swept_to_missed_and_still_toggleable(tmp_path):
    from datetime import date, timedelta
    from core.routes.settings_routes import schedule
    from core.routes.goal_routes import toggle_action
    conn = _fresh_db(tmp_path)
    conn.execute("INSERT INTO users (name, password_hash) VALUES ('T', 'x')")
    conn.execute("INSERT INTO goals (user_id, title, display_title, status, deadline) VALUES (1, 'Startup', 'Startup', 'active', ?)",
                 ((date.today() + timedelta(days=5)).isoformat(),))
    past = (date.today() - timedelta(days=3)).isoformat()
    future = (date.today() + timedelta(days=2)).isoformat()
    conn.executemany(
        "INSERT INTO actions (goal_id, user_id, title, date, status) VALUES (1,1,?,?,?)",
        [("Missed task", past, "pending"), ("Future task", future, "pending")],
    )
    conn.commit()
    out = schedule(user={"id": 1})
    # Sweep persisted the miss.
    rows = {r["title"]: r["status"] for r in conn.execute("SELECT title, status FROM actions").fetchall()}
    assert rows["Missed task"] == "missed"
    assert rows["Future task"] == "pending"
    # It shows up in the schedule payload with the failed status.
    assert out["missed_total"] == 1
    assert out["dates"]["1"]["by_date"][past][0]["status"] == "missed"
    # And the toggle endpoint still closes it out.
    r = toggle_action(action_id=1, user={"id": 1})
    assert r["status"] == "done"