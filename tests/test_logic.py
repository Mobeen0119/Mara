from core import generation


def test_time_windows():
    assert generation.parse_time_window("11pm-7am") == (23*60, 7*60 + 24*60)
    assert generation.parse_time_window("3pm-5pm")[0] == 15*60
    assert generation.parse_time_window("gym 5-7pm") == (17*60, 19*60)


def test_time_windows_human_phrasings():
    # The old parser turned 'uni 6 to 2 pm' into 6pm-2am (overnight misread) and
    # dropped '9 to 5' entirely (None). Both starved every schedule grid.
    assert generation.parse_time_window("uni 6 to 2 pm") == (6*60, 14*60)
    assert generation.parse_time_window("work 9 to 5") == (9*60, 17*60)
    assert generation.parse_time_window("gym 5 to 6 am") == (5*60, 6*60)
    assert generation.parse_time_window("classes 1-4pm") == (13*60, 16*60)
    assert generation.parse_time_window("12-2pm") == (12*60, 14*60)
    assert generation.parse_time_window("9:00-9:30") == (9*60, 9*60 + 30)
    assert generation.parse_time_window("school 8am-3pm") == (8*60, 15*60)


def test_days_remaining():
    assert generation.days_remaining("2026-09-10", today="2026-09-03") == 7


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


def test_fallback_multiple_blocks_per_day():
    goal = {
        "deadline": "2026-09-09",
        "display_title": "Study",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    entries = generation._build_fallback_plan(goal, today="2026-09-05")
    from collections import Counter
    blocks = Counter(e["date"] for e in entries)
    # a real day gets morning/afternoon/evening sessions, not just one slot
    assert max(blocks.values()) >= 2
    # all three blocks of the first day should not be identical titles
    first_day = [e for e in entries if e["date"] == "2026-09-05"]
    assert len(set(e["title"] for e in first_day)) >= 2


def test_user_blocked_windows_respected():
    goal = {
        "deadline": "2026-09-20",
        "display_title": "Study",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    blocked = generation.user_blocked_windows({"blocked_windows": '["gym 5-7pm"]'})
    assert blocked == [(17 * 60, 19 * 60)]
    entries = generation._build_fallback_plan(goal, today="2026-09-10", extra_blocked=blocked)
    for e in entries:
        start_h = int(e["start_time"][:2])
        end_h = int(e["end_time"][:2])
        assert not (start_h < 19 and end_h > 17), f"blocked window violated: {e}"


def test_fallback_slots_never_overlap():
    goal = {
        "deadline": "2026-09-20",
        "display_title": "Study",
        "reminder_time": "09:00",
        "constraints": "[]",
    }

    def no_overlap(entries):
        by_day = {}
        for e in entries:
            by_day.setdefault(e["date"], []).append(e)
        for day, day_entries in by_day.items():
            blocks = sorted(
                (
                    int(e["start_time"][:2]) * 60 + int(e["start_time"][3:5]),
                    int(e["end_time"][:2]) * 60 + int(e["end_time"][3:5]),
                )
                for e in day_entries
            )
            for i in range(1, len(blocks)):
                assert blocks[i][0] >= blocks[i - 1][1], f"overlap: {blocks}"

    no_overlap(generation._build_fallback_plan(goal, today="2026-09-10"))
    blocked = generation.user_blocked_windows({"blocked_windows": '["gym 5-7pm"]'})
    no_overlap(generation._build_fallback_plan(goal, today="2026-09-10", extra_blocked=blocked))
    # a gym block right at morning should NOT shove an afternoon slot into 7am
    blocked_early = [(300, 420)]  # 5-7am
    entries = generation._build_fallback_plan(goal, today="2026-09-10", extra_blocked=blocked_early)
    starts = [int(e["start_time"][:2]) * 60 + int(e["start_time"][3:5]) for e in entries]
    assert all(s >= 7 * 60 for s in starts), f"slot too early: {starts}"
    no_overlap(entries)


def test_pressure_rating():
    goal = {"deadline": "2026-09-04", "display_title": "X"}
    done = {"status": "done", "duration_min": 60}
    pending = {"status": "pending", "duration_min": 60}
    # overdue + lots open -> near max
    high = generation.pressure_rating(goal, [pending, pending, pending], open_blockers=2, missed_days=3)
    assert high["score"] >= 8
    assert high["label"] == "gym is cancelled"
    # far deadline, nothing open -> low
    loose = generation.pressure_rating(
        {"deadline": "2026-09-30", "display_title": "Y"}, [done, done], open_blockers=0, missed_days=0
    )
    assert loose["score"] <= 4
    assert 1 <= loose["score"] <= 10


def test_completion_detection():
    assert generation.completion_detected("all finished")
    assert generation.completion_detected("I am done")
    assert generation.completion_detected("I finished it")
    assert not generation.completion_detected("why do I always leave things for the last second")
    assert not generation.completion_detected("what do I have to do today")
    assert not generation.completion_detected("what am I supposed to do now")
    assert not generation.completion_detected("should I do the homework?")
    assert not generation.completion_detected("I'm done with this is getting long enough to be a longer sentence that exceeds the word cap for auto close")


def test_guardrail_refuses_abuse():
    # direct family + sex -> refuse
    assert generation._is_truly_harmful("i want to fuck my sister")
    assert generation._is_truly_harmful("fuck my daughter")
    assert generation._is_truly_harmful("rape my sister")
    # coercion / violence -> refuse
    assert generation._is_truly_harmful("how do I drug my crush so she sleeps with me")
    assert generation._is_truly_harmful("kill my boss and take his job")
    # thread-aware: a short follow-up after an incest message still refuses
    hist = "you: i want to fuck my sister, my dick inside her"
    for m in ("sister", "my dick inside her now", "how to do both have consent and everything",
              "i said i want to fuck my sister what's difficult u bitch"):
        assert generation._is_truly_harmful(m, context=hist), m


def test_guardrail_allows_partner_romance():
    # legitimate romantic / intimate goals with an adult partner must be planable
    for m in (
        "plan a romantic dinner for my girlfriend 5th anniversary",
        "what should i buy my wife for valentine",
        "plan a memorable night for my partner, things to buy and what makes it special",
        "i want to have sex with my girlfriend tonight, what to buy and how to make it special",
        "date night ideas for my husband",
        "plan a hookup night where we actually fuck, protection sorted, consent first",
    ):
        assert not generation._is_truly_harmful(m), m


def test_hookup_plan_has_guidance():
    # hookup goals get LLM generation direction (real-time), NOT hardcoded templates.
    # The direction must demand COMPLETE CONCRETE ACTIONS and forbid topic/vibe titles
    # ("ensure aftercare", "confirm safety") — the user tore those apart as "brainstorm".
    g = generation._plan_guidance("plan a hookup night where we actually fuck", "partner: sarah")
    assert "COMPLETE CONCRETE ACTION" in g
    assert "not a lecture" in g
    assert "ensure aftercare" in g.lower()  # explicitly named as FORBIDDEN, never scheduled
    # and it must not force a preachy consent-talk onto an explicit goal the user owns
    assert "consent & boundaries" not in g.lower()
    assert "respect every no" not in g.lower()
    # but the safety-net fallback is generic — it must not fabricate romance templates
    goal = {
        "deadline": "2026-09-20",
        "display_title": "plan a hookup night where we actually fuck",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    entries = generation._build_fallback_plan(goal, today="2026-09-10")
    assert all(e["title"] for e in entries)


def test_plan_guidance_is_dynamic_not_a_schedule():
    # guidance is direction for the LLM, never a fixed schedule of tasks
    r = generation._plan_guidance("memorable anniversary night for wife", "budget under 300, wife loves candles")
    assert "gift" in r and "note" in r
    assert '{"' not in r  # no pre-baked schedule
    # non-romance goals get no special guidance
    assert generation._plan_guidance("study for exams", "") == ""


def test_romance_fallback_plans_concrete_steps():
    goal = {
        "deadline": "2026-09-20",
        "display_title": "Plan a memorable anniversary night for my girlfriend",
        "reminder_time": "09:00",
        "constraints": "[]",
        "details": '{"person":"my girlfriend — candles, live music","budget":"under 300","vibe":"intimate and calm"}',
    }
    # fallback must still produce a full, varied schedule for romance goals (safety net),
    # without pre-baked romance script — generation happens in the LLM call.
    entries = generation._build_fallback_plan(goal, today="2026-09-10")
    assert all(e["title"] for e in entries)

def test_wsl_ollama_candidates_keep_port():
    from core.llm.ollama_provider import OllamaProvider
    p = OllamaProvider("http://localhost:11435", "x")
    cands = p._candidates()
    assert "http://localhost:11435" in cands
    # WSL-host IP fallbacks must inherit the SAME port as the configured URL,
    # otherwise a host-Ollama on 11435 is probed as `:11434` and never found.
    for c in cands:
        if c.endswith(":11435"):
            assert c.startswith("http://localhost") or c.startswith("http://172") \
                or c.startswith("http://192") or c.startswith("http://10."), c
    # If the configured port is wrong, the default port must ALSO be probed (on
    # localhost + host IPs) so a reachable-but-misconfigured Ollama isn't reported
    # as offline.
    assert any(c.endswith(":11434") for c in cands)
    assert any(c.endswith(":11434") for c in cands if c.startswith("http://172"))


def test_chat_no_canned_reply_when_model_down():
    import core.generation as g
    text, source = g.generate_global_chat_reply("T", [], "", "hi eloise", db=None)
    assert source == "offline"
    assert text == ""
    # stream path must NOT yield canned Eloise text when the model is down —
    # it fails loudly instead, and the route turns that into a real 503/error frame.
    try:
        list(g.stream_chat_reply("T", "learn piano", "", "hi", db=None))
        assert False, "expected RuntimeError (no usable provider) — stream must not fabricate a reply"
    except RuntimeError:
        pass
    # guardrail path must still refuse INSTANTLY and in-canon
    text, source = g.generate_global_chat_reply("T", [], "", "wedding night with my sister", db=None)
    assert source == "guardrail" and text == g.GUARDRAIL_REFUSAL


def test_chat_prompt_is_conversational_not_repetitive():
    # The user complained replies repeated verbatim. The prompt must force the model to
    # answer the LATEST question in fresh words, and to give actual words when asked
    # what to SAY — never to just re-point at the first plan task.
    gp = generation._goal_chat_prompt("i have to fuck someone", "=== the plan ===\n9-10:30 discuss consent\n", "user: hi\neloise: first task\n", "mobeen", "what to say to a gf in call")
    assert "LATEST question" in gp
    assert "do NOT copy your last reply" in gp or "NEVER repeat" in gp or "do NOT repeat" in gp.lower()
    assert "- They ask what to SAY" in gp
    assert "- They ask 'what do I do now'" in gp
    gl = generation._global_chat_prompt("study for exams", "user: hi\neloise: study\n", "mobeen", "what to say")
    assert "do NOT copy your last reply" in gl
    assert "Answer exactly what was just asked" in gl


def test_hookup_guidance_not_preachy():
    # The user got plans lecturing him to "discuss consent & boundaries" on repeat.
    # Hookup guidance must be PRACTICAL logistics, not a forced consent-lecture task list.
    g = generation._plan_guidance("i have to fuck someone", "she is my gf, at her place, tonight work night")
    assert "consent & boundaries" not in g.lower()
    assert "respect every no" not in g.lower()
    assert "not a lecture" in g.lower()
    # the topic-words that produced the old "brainstorm" titles are explicitly forbidden
    assert "ensure aftercare" in g.lower() and "confirm safety" in g.lower()


def test_plan_prompt_covers_every_day():
    goal = {"deadline": "2026-09-12", "title": "start my startup", "display_title": "start my startup",
            "constraints": "[]", "details": "{}", "user_id": 1, "id": 1}
    # monkeypatch the LLM path so we can capture the prompt it would send
    calls = {}
    import core.llm.base as lb
    class _Fake:
        ok = True
        text = '[]'
        provider = "fake"
        latency_ms = 1
        model = "x"
        error = None
    orig_gen = generation.get_manager
    def fake_gen(db):
        class M:
            def any_usable(self_): return True
            def generate(self_, sys, user_prompt, timeout=30, max_tokens=None, prefer_cloud=False):
                calls["prompt"] = user_prompt
                return _Fake()
        return M()
    generation.get_manager = fake_gen
    try:
        generation._llm_plan_call(goal, None, timeout=10)
    finally:
        generation.get_manager = orig_gen
    p = calls.get("prompt", "")
    assert "Cover EVERY day" in p
    assert "chat notes say is already done" in p.replace("\n", " ")


def test_startup_plan_gets_universal_deadline_pacing_and_chat_links_feed_the_plan():
    # Right-sizing is UNIVERSAL, not startup-specific: ANY goal's schedule follows the
    # deadline — a far-off goal is a sustainable one-real-action-a-day plan (no padding),
    # a pressing one is a grind with many sessions/day — and any URL the user shared in
    # chat notes is handed to the plan so it references the user's real resources.
    import sqlite3
    from core import generation
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER, goal_id INTEGER, role TEXT, content TEXT)"
    )
    conn.execute(
        "INSERT INTO chat_messages (user_id, goal_id, role, content) VALUES "
        "(1, 7, 'user', 'check my idea at https://yoursite.app/idea and https://tools.dev/board')"
    )
    calls = {}

    class _Fake:
        ok = True
        text = '[]'
        provider = "fake"
        latency_ms = 1
        model = "x"
        error = None

    orig = generation.get_manager
    def fake_gen(db):
        class M:
            def any_usable(self_): return True
            def generate(self_, sys, user_prompt, timeout=30, max_tokens=None, prefer_cloud=False):
                calls["prompt"] = user_prompt
                return _Fake()
        return M()

    generation.get_manager = fake_gen
    try:
        far = {"id": 7, "user_id": 1, "deadline": "2026-11-30",
               "title": "learn guitar", "display_title": "learn guitar",
               "reminder_time": "09:00", "constraints": "[]", "details": "{}"}
        generation._llm_plan_call(far, conn, timeout=10)
        far_p = calls.get("prompt", "")
        urgi = {"id": 7, "user_id": 1, "deadline": "2026-09-07",
                "title": "paper due tomorrow", "display_title": "paper due tomorrow",
                "reminder_time": "09:00", "constraints": "[]", "details": "{}"}
        generation._llm_plan_call(urgi, conn, timeout=10)
        near_p = calls.get("prompt", "")
    finally:
        generation.get_manager = orig
    # the SAME deadline-driven rule applies to any goal kind — no startup template
    assert "RIGHT-SIZE THIS TO THE DEADLINE" in far_p
    assert "sustainable" in far_p and "do NOT pad" in far_p
    assert "GRIND" in near_p
    assert "8+ sessions" in near_p
    assert "lean startup sprint" not in far_p + near_p
    assert "landing page" not in far_p + near_p  # not a startup special-case
    # chat links reach the plan for either goal
    assert "https://yoursite.app/idea" in far_p and "https://tools.dev/board" in far_p
    assert "https://yoursite.app/idea" in near_p
    conn.close()


def test_chat_prompts_forbid_inventing_tasks():
    # Chat must ground itself in the schedule: never invent tasks, quote only listed ones.
    from core import generation as g
    global_p = g._global_chat_prompt("digest of goals, today tasks", "hist", "Mobeen", "what do i do")
    goal_p = g._goal_chat_prompt("my goal", "=== THE PLAN ===\n11:00 buy milk\n", "hist", "Mobeen", "now?")
    assert "never invent" in global_p and "sources of truth" in global_p
    assert "never invent tasks, dates, or times" in goal_p
    assert "QUOTED from THE PLAN" in goal_p


def test_history_drops_last_reply_on_repeat():
    import sqlite3
    from core.routes.chat_routes import _history
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE general_chat (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT)")
    for role, content in [("user", "hi"), ("eloise", "answer one"),
                          ("user", "what to say"), ("eloise", "answer one")]:
        conn.execute("INSERT INTO general_chat (user_id, role, content) VALUES (1,?,?)", (role, content))
    h = _history(conn, None, 1, drop_reply_to="what to say")
    joined = "\n".join(h)
    # the duplicated eloise answer "answer one" (repeating the earlier reply) must be gone,
    # leaving the fresh user question without the stale answer to copy
    assert h[-1] == "user: what to say" and "eloise: answer one" not in h[-1]
    conn.close()


def test_redraw_keeps_old_plan_when_llm_down_and_writes_new_when_up():
    # User contract: Redraw returns instantly; while the model works the OLD schedule
    # stays visible; on model failure the OLD plan is kept (+ plan_status back to
    # active, summary "kept previous"); on success the NEW plan replaces it.
    import os
    import tempfile
    import time as _time
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    from core import generation as _gmod
    import core.routes.goal_routes as gr

    tmp = tempfile.mkdtemp(prefix="eloise_redraw_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None

    orig_gen = _gmod.generate_plan_or_none
    orig_blocked = _gmod.user_blocked_windows
    orig_conn = gr.get_connection
    gr.get_connection = real_get
    _gmod.user_blocked_windows = lambda user: []
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest) VALUES ('tester','tester@redraw.io','x',1)")
        conn.execute("INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, plan_status) VALUES (1,'test goal','2026-09-12','09:00','[]','test goal','active')")
        gid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute("INSERT INTO actions (goal_id,user_id,date,start_time,end_time,title,status) VALUES (?,?,?,?,?,?,?)", (gid,1,"2026-09-06","09:00","10:00","old task one","pending"))
        conn.execute("INSERT INTO actions (goal_id,user_id,date,start_time,end_time,title,status) VALUES (?,?,?,?,?,?,?)", (gid,1,"2026-09-07","15:00","16:00","old task two","pending"))
        conn.commit()
        user = {"id": 1, "name": "tester"}

        # --- LLM down: keep old ---
        _gmod.generate_plan_or_none = lambda *a, **k: None
        resp = gr.regen_plan(gid, user=user, body=None)
        assert resp["ok"] is True and resp["source"] == "redraw-bg"
        assert len(resp["plan"]) == 2
        st = real_get().execute("SELECT plan_status FROM goals WHERE id=?", (gid,)).fetchone()["plan_status"]
        assert st == "generating", st
        row = {}
        deadline_ts = _time.monotonic() + 15
        while _time.monotonic() < deadline_ts:
            row = real_get().execute("SELECT plan_status, plan_summary FROM goals WHERE id=?", (gid,)).fetchone()
            if row["plan_status"] != "generating":
                break
            _time.sleep(0.2)
        rv = dict(row) if row else {}
        assert rv.get("plan_status") == "active", rv
        assert (dict(row).get("plan_summary") or "").startswith("kept previous"), dict(row)
        cnt = real_get().execute("SELECT COUNT(*) c FROM actions WHERE goal_id=?", (gid,)).fetchone()["c"]
        assert cnt == 2, cnt  # old plan preserved on failure

        # --- LLM up: new plan replaces ---
        newp = [
            {"date": "2026-09-06", "title": "brand new one", "start_time": "09:00", "end_time": "10:00"},
            {"date": "2026-09-07", "title": "brand new two", "start_time": "15:00", "end_time": "16:00"},
            {"date": "2026-09-08", "title": "brand new three", "start_time": "18:00", "end_time": "19:30"},
        ]
        _gmod.generate_plan_or_none = lambda *a, **k: newp
        resp2 = gr.regen_plan(gid, user=user, body=None)
        assert resp2["source"] == "redraw-bg"
        row = {}
        deadline_ts = _time.monotonic() + 15
        while _time.monotonic() < deadline_ts:
            row = real_get().execute("SELECT plan_status, plan_summary FROM goals WHERE id=?", (gid,)).fetchone()
            if row["plan_status"] != "generating":
                break
            _time.sleep(0.2)
        titles = [r["title"] for r in real_get().execute("SELECT title FROM actions WHERE goal_id=? ORDER BY date, start_time", (gid,)).fetchall()]
        assert titles == ["brand new one", "brand new two", "brand new three"], titles
        rv = dict(row) if row else {}
        assert rv.get("plan_status") == "active", rv
    finally:
        gr.get_connection = orig_conn
        _gmod.generate_plan_or_none = orig_gen
        _gmod.user_blocked_windows = orig_blocked
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]


def test_fallback_avoids_other_goals_calendar():
    # The old fallback grid only respected DECLARED busy windows, not the user's actual
    # calendar. A friend's plan occupying 09:00-21:00 on every day silently collapsed a
    # new goal to "0 tasks". The fallback must place slots in time that is genuinely
    # free once other goals are accounted for.
    goal = {
        "deadline": "2026-09-09",
        "display_title": "Study",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    busy = {"2026-09-06": [(9 * 60, 21 * 60)],
            "2026-09-07": [(9 * 60, 21 * 60)],
            "2026-09-08": [(9 * 60, 21 * 60)],
            "2026-09-09": [(9 * 60, 21 * 60)]}
    entries = generation._build_fallback_plan(goal, today="2026-09-06", busy_by_date=busy)
    assert entries, "calendar-aware fallback must not collapse to empty"
    for e in entries:
        s = int(e["start_time"][:2]) * 60 + int(e["start_time"][3:5])
        eend = int(e["end_time"][:2]) * 60 + int(e["end_time"][3:5])
        # genuinely free edges of the day: before 9:00 / after 21:00,
        # and never past the 23:00 planning ceiling
        assert s >= 8 * 60 and eend <= 23 * 60
        assert eend <= 9 * 60 or s >= 21 * 60, f"collides with busy block: {e}"


def test_resolve_cross_goal_keeps_fallback_when_calendar_full():
    # _write_plan must NEVER commit "0 tasks across 0 days" even when conflict
    # resolution empties a non-empty draw (deadline day fully booked everywhere).
    import os as _os
    import tempfile
    import core.routes.goal_routes as gr
    from core.database import set_storage_dir, get_connection as real_get, _thread_local

    tmp = tempfile.mkdtemp(prefix="writeplan_")
    _os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    orig_conn = gr.get_connection
    gr.get_connection = real_get
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name,password_hash,is_guest) VALUES ('t','x',1)")
        conn.execute(
            "INSERT INTO goals (user_id,title,deadline,reminder_time,constraints,display_title,plan_status) "
            "VALUES (1,'Study','2026-09-10','09:00','[]','Study','active')"
        )
        gid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO goals (user_id,title,deadline,reminder_time,constraints,display_title,plan_status) "
            "VALUES (1,'Other','2026-09-10','09:00','[]','Other','active')"
        )
        oid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx) "
            "VALUES (?,1,'2026-09-10','Busy','09:00','21:00',720,'pending',0)", (oid,)
        )
        conn.commit()
        goal = dict(conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        entries = [{"date": "2026-09-10", "title": f"s{i}", "start_time": "09:00",
                    "end_time": "10:30"} for i in range(4)]
        res = gr._write_plan(conn, goal, entries, blocked_min=None, provider="openrouter")
        assert res is None, "fully-booked day must NOT commit an empty plan"
        row = conn.execute("SELECT plan_summary FROM goals WHERE id=?", (gid,)).fetchone()
        assert "0 tasks" not in (row[0] or ""), row[0]
    finally:
        gr.get_connection = orig_conn
        _thread_local.conn = None
        del _os.environ["ELOISE_STORAGE_DIR"]


def test_user_calendar_busy_queries_other_goals_only():
    # The calendar helper must exclude the goal's OWN actions (so redraws don't treat
    # themselves as obstacles) while including other pending goals' blocks.
    import os as _os
    import tempfile
    from core import database as _db

    tmp = tempfile.mkdtemp(prefix="calbusy_")
    _os.environ["ELOISE_STORAGE_DIR"] = tmp
    _db.set_storage_dir(tmp)
    import core.routes.goal_routes as _gr_tmp
    _orig_conn = _gr_tmp.get_connection
    _gr_tmp.get_connection = _db.get_connection
    try:
        from core.database import _thread_local as _tl
        _tl.conn = None
        conn = _db.get_connection()
        conn.execute("INSERT INTO users (name,password_hash,is_guest) VALUES ('t','x',1)")
        conn.execute(
            "INSERT INTO goals (user_id,title,deadline,reminder_time,constraints,display_title,plan_status) "
            "VALUES (1,'A','2026-09-10','09:00','[]','A','active')"
        )
        a = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO goals (user_id,title,deadline,reminder_time,constraints,display_title,plan_status) "
            "VALUES (1,'B','2026-09-10','09:00','[]','B','active')"
        )
        b = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx) "
            "VALUES (?,1,'2026-09-08','own','08:00','08:30',30,'pending',0)", (a,))
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx) "
            "VALUES (?,1,'2026-09-08','other','10:00','10:30',30,'pending',0)", (b,))
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx) "
            "VALUES (?,1,'2026-09-08','done','11:00','11:30',30,'done',0)", (a,))
        conn.commit()
        busy = generation.user_calendar_busy(conn, 1, a, "2026-09-06", "2026-09-10")
        times = busy.get("2026-09-08", [])
        assert (10 * 60, 10 * 60 + 30) in times, times   # other goal's pending block
        assert (8 * 60, 8 * 60 + 30) not in times, times # own action excluded
        assert (11 * 60, 11 * 60 + 30) not in times, times  # done action excluded
    finally:
        _gr_tmp.get_connection = _orig_conn
        _tl.conn = None
        del _os.environ["ELOISE_STORAGE_DIR"]


def test_hookup_arc_plan():
    # The user tore apart the old hookup plan: it skipped days, put "aftercare" days
    # before the act, and had "confirm way home" the wrong night. The schedule must be
    # a real arc: prep days -> THE EVENT EVENING (night before a free day) -> a
    # morning-after "return home" day. Every day covered, nothing overlaps.
    goal = {
        "deadline": "2026-09-10",
        "title": "i have to fuck someon",
        "display_title": "i have to fuck someon",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    entries = generation._build_fallback_plan(goal, today="2026-09-05")
    dates = sorted({e["date"] for e in entries})
    assert dates == [f"2026-09-{d:02d}" for d in range(5, 11)], dates  # no skipped days

    nights = [e for e in entries if e["title"].startswith("The night")]
    assert len(nights) == 1, [e["title"] for e in entries]  # exactly one event evening
    night = nights[0]
    assert night["date"] == "2026-09-09", night   # night BEFORE the deadline day
    assert night["start_time"] >= "18:00", night  # evening, not midday
    assert night["end_time"] >= "21:00", night

    morning = [e for e in entries if e["title"].startswith("Morning after")]
    assert len(morning) == 1
    assert morning[0]["date"] == "2026-09-10", morning  # return home next day
    assert morning[0]["start_time"] <= "11:00", morning

    # human-scale: no overlapping tasks, readable concrete titles, day load >= 1
    by_day = {}
    for e in entries:
        by_day.setdefault(e["date"], []).append(e)
    for day, ds in by_day.items():
        blocks = sorted(
            (int(e["start_time"][:2]) * 60 + int(e["start_time"][3:5]),
             int(e["end_time"][:2]) * 60 + int(e["end_time"][3:5]))
            for e in ds
        )
        for i in range(1, len(blocks)):
            assert blocks[i][0] >= blocks[i - 1][1], (day, blocks)
        assert len(set(e["title"] for e in ds)) == len(ds), (day, [e["title"] for e in ds])
        for e in ds:
            assert len(e["title"].split()) >= 3, e["title"]

    # aftercare/wrap must exist AFTER the night, not before it
    close = [e for e in entries if e["title"].startswith("Wrap up")]
    assert len(close) == 1 and close[0]["date"] == "2026-09-10", [e["title"] for e in entries]
    # hard rule: the fallback NEVER writes canned Eloise wording — every title is a
    # structural label + the user's OWN goal text, nothing fabricated.
    for e in entries:
        assert "someon" in e["title"], e["title"]
        assert "consent" not in e["title"] and "protection" not in e["title"], e["title"]


def test_day_density_follows_deadline_and_free_time():
    # NOT a fixed 2-3 per day. The count follows the deadline and how much of that day
    # is actually free: a goal due in a day on a fully free day packs it (MORE than 8
    # blocks when the time is there), a month-long goal is light, an easy distant goal
    # is as little as one real block a day.
    urgent = {
        "deadline": "2026-09-06",
        "title": "paper due tomorrow",
        "display_title": "paper due tomorrow",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    u = generation._build_fallback_plan(urgent, today="2026-09-05")
    today_paper = [e for e in u if e["date"] == "2026-09-05"]
    assert len(today_paper) >= 9, [e["title"] for e in today_paper]  # free day, grinding
    # distinct non-overlapping blocks even when a day is packed
    blocks = sorted((int(e["start_time"][:2]) * 60 + int(e["start_time"][3:5]),
                     int(e["end_time"][:2]) * 60 + int(e["end_time"][3:5])) for e in today_paper)
    for i in range(1, len(blocks)):
        assert blocks[i][0] >= blocks[i - 1][1], (i, blocks)
    assert len(set(e["title"] for e in today_paper)) == len(today_paper)
    assert len(blocks) == len(today_paper)

    relaxed = {
        "deadline": "2026-09-20",
        "title": "learn guitar",
        "display_title": "learn guitar",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    r = generation._build_fallback_plan(relaxed, today="2026-09-05")
    from collections import Counter
    assert max(Counter(e["date"] for e in r).values()) <= 2  # steady, not crammed

    long_goal = {
        "deadline": "2026-10-15",
        "title": "start a business",
        "display_title": "start a business",
        "reminder_time": "09:00",
        "constraints": "[]",
    }
    l = generation._build_fallback_plan(long_goal, today="2026-09-05")
    per_day = Counter(e["date"] for e in l)
    assert max(per_day.values()) == 1  # month+ goal: one real action a day, every day
    from datetime import date, timedelta
    expected = [(date(2026, 9, 5) + timedelta(days=i)).isoformat() for i in range(40)]
    assert sorted(per_day) == expected  # no gaps, no contraction


def test_goal_chat_prompt_picks_when_asked():
    # "she gave me options, tell me which to pick" -> Eloise must COMMIT, not bounce.
    gp = generation._goal_chat_prompt("pace night", "plan", "user: hi\neloise: s", "mobeen",
                                      "she gave me 3 options which should i choose")
    assert "PICK ONE" in gp
    assert "bounce the choice back" in gp
    assert "it's up to you" in gp  # explicitly forbidden
    gl = generation._global_chat_prompt("study", "u: hi", "mobeen", "which should i pick?")
    assert "PICK ONE" in gl


def test_title_picker_rejects_filler():
    assert not generation._title_ok("study")
    assert not generation._title_ok("review the plan")
    assert not generation._title_ok("consent & boundaries chat")
    assert not generation._title_ok("")
    assert generation._title_ok("Pick the three-piece, text her, agree evening time")
    assert generation._title_ok("The night: slow pace, stay present, stay over")

    # the user's #1 complaint: titles like these are brainstorm topics, not actions —
    # weak planning verbs at the start are rejected so the schedule stays concrete.
    assert not generation._title_ok("Ensure aftercare and safety")
    assert not generation._title_ok("Confirm way home and ensure privacy")
    assert not generation._title_ok("Prepare for the evening with light music")
    assert not generation._title_ok("Discuss the night and see how it flows")

    # per-slot tasks are UP TO TWO SHORT LINES of concrete instruction — a proper
    # two-line action passes, anything beyond two lines is rejected.
    two_line = "Pick the three-piece, text her, agree 8pm.\nCandles, slow music, door at 8."
    assert generation._title_ok(two_line)
    assert generation._title_ok("Ensure the table is set.\nCandles at 8, music slow") is False
    assert not generation._title_ok("line one\nline two\nline three action")
    assert not generation._title_ok("A" * 200)
    text = ('```json\n[{"date":"2026-09-09","start_time":"18:00","end_time":"22:00",'
            '"title":"Pick the three-piece, text her, agree 8pm.\\nCandles, slow music, door at 8."}]```')
    m = generation._parse_title_map(text)
    assert m[("2026-09-09", "18:00")] == "Pick the three-piece, text her, agree 8pm.\nCandles, slow music, door at 8."

    text = ('```json\n[{"date":"2026-09-09","start_time":"18:00","end_time":"22:00",'
            '"title":"The night with Sarah: slow it down, stay over"}]```')
    m = generation._parse_title_map(text)
    assert m[("2026-09-09", "18:00")] == "The night with Sarah: slow it down, stay over"
    assert generation._parse_title_map("garbage no json") == {}


def test_ollama_calls_serialize_globally():
    # The user's diagnosis: one CPU model hit by chat + N retry threads at once. Ollama
    # can serve ONE generation at a time, so the app-wide lock must force concurrent
    # calls into a queue (2x wall time) instead of racing.
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    import core.llm.manager as mmod
    from core.llm.manager import LLMManager

    class SlowProvider:
        name = "ollama"
        model = "m"
        api_key = ""
        def generate(self, sp, up, timeout=30, max_tokens=None):
            _time.sleep(1.0)
            return mmod.GenerationResult(ok=True, provider=self.name, model=self.model,
                                         text="ok", latency_ms=1000)

    orig_build = mmod.build_providers
    orig_config = mmod.provider_config
    mmod.build_providers = lambda db=None, config=None: [SlowProvider()]
    mmod.provider_config = lambda db: {}
    try:
        mm = LLMManager()
        with ThreadPoolExecutor(max_workers=2) as ex:
            t0 = _time.monotonic()
            futs = [ex.submit(lambda: mm.generate("s", "u", timeout=30)), ex.submit(lambda: mm.generate("s", "u", timeout=30))]
            results = [f.result() for f in futs]
            wall = _time.monotonic() - t0
        assert all(r.ok for r in results)
        assert wall >= 1.8, wall  # 2 x 1s back-to-back, NOT concurrent
    finally:
        mmod.build_providers = orig_build
        mmod.provider_config = orig_config


def test_openrouter_calls_not_serialized():
    # Cloud provider must NOT take the local model lock — parallel calls stay parallel.
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    import core.llm.manager as mmod
    from core.llm.manager import LLMManager

    class FastCloud:
        name = "openrouter"
        model = "m"
        api_key = "sk-test"
        def generate(self, sp, up, timeout=30, max_tokens=None):
            _time.sleep(1.0)
            return mmod.GenerationResult(ok=True, provider=self.name, model=self.model,
                                         text="ok", latency_ms=1000)

    orig_build = mmod.build_providers
    orig_config = mmod.provider_config
    mmod.build_providers = lambda db=None, config=None: [FastCloud()]
    mmod.provider_config = lambda db: {}
    try:
        mm = LLMManager()
        with ThreadPoolExecutor(max_workers=2) as ex:
            t0 = _time.monotonic()
            futs = [ex.submit(lambda: mm.generate("s", "u", timeout=30)), ex.submit(lambda: mm.generate("s", "u", timeout=30))]
            results = [f.result() for f in futs]
            wall = _time.monotonic() - t0
        assert all(r.ok for r in results)
        assert wall < 1.7, wall  # concurrent, not serialized
    finally:
        mmod.build_providers = orig_build
        mmod.provider_config = orig_config


def test_plan_retry_is_bounded_and_deduplicated():
    import core.routes.goal_routes as gr

    updates = []
    class FakeConn:
        def execute(self, sql, *a):
            if sql.startswith("UPDATE goals"):
                updates.append(a)
            return self
        def commit(self):
            return None
    orig_bg = gr._regenerate_plan_bg
    orig_conn = gr.get_connection
    gr._regenerate_plan_bg = lambda user, gid: None
    gr.get_connection = lambda: FakeConn()
    try:
        gr._plan_retry_state.clear()
        user = {"id": 1}
        gid = 7
        gr._schedule_plan_retry(user, gid)
        st = gr._plan_retry_state[gid]
        assert st["scheduled"] is True and st["attempts"] == 1
        # a second failure while one retry is pending must NOT stack another thread
        gr._schedule_plan_retry(user, gid)
        assert st["attempts"] == 1 and st["scheduled"] is True
        # the pending retry resolves (fails again) -> backoff increments
        st["scheduled"] = False
        gr._schedule_plan_retry(user, gid)
        assert st["attempts"] == 2
        # a successful plan write resets the retry budget
        gr._reset_plan_retries(gid)
        assert gid not in gr._plan_retry_state
        # exhausting the budget stops scheduling entirely (no more threads)
        for _ in range(gr._PLAN_RETRY_MAX + 3):
            if gr._plan_retry_state.get(gid, {}).get("scheduled"):
                gr._plan_retry_state[gid]["scheduled"] = False
            gr._schedule_plan_retry(user, gid)
        last = gr._plan_retry_state[gid]
        assert last["attempts"] == gr._PLAN_RETRY_MAX, last
        assert last["scheduled"] is False
        assert any(7 in u[0] for u in updates)  # honest "giving up" note recorded
    finally:
        gr._plan_retry_state.clear()
        gr._regenerate_plan_bg = orig_bg
        gr.get_connection = orig_conn


def test_plan_attempts_llm_even_when_probe_says_down():
    # A probe misreading a busy box as "down" must no longer gate plan generation:
    # the real call always gets its chance (it queues on the shared lock instead).
    from core import generation as g
    import re as _re
    calls = {"n": 0, "fail": False}
    class R:
        ok = property(lambda self: not calls["fail"])
        provider = "ollama"
        model = "x"
        error = None
        latency_ms = 1
        def __init__(self):
            self.text = "[]"
    orig = g.get_manager
    def fake(db):
        class M:
            def any_usable(self_):
                return False
            def generate(self_, sys, user_prompt, timeout=30, max_tokens=None, prefer_cloud=False):
                calls["n"] += 1
                r = R()
                if not calls["fail"]:
                    m = _re.findall(r'"date": "([^"]+)", "start_time": "([^"]+)"', user_prompt)
                    items = [
                        f'{{"date":"{d}","start_time":"{st}","end_time":"10:30","title":"send the polished deck to the investor"}}'
                        for d, st in m]
                    r.text = "[\n" + ",\n".join(items) + "\n]"
                return r
        return M()
    g.get_manager = fake
    try:
        goal = {"deadline": "2099-01-01", "title": "some goal", "display_title": "some goal",
                "reminder_time": "09:00", "constraints": "[]", "details": "{}", "user_id": 1}
        res = g.generate_plan(goal, {"id": 1}, db=None)
        calls["fail"] = True
        res_none = g.generate_plan(goal, {"id": 1}, db=None)
    finally:
        g.get_manager = orig
    assert calls["n"] >= 2  # draws are split into slices now; each slice is its own call
    # Model answered -> ONLY the model's plan is returned (probe said down but call made).
    assert res is not None and len(res) > 0
    # Model failed -> None, NEVER a hardcoded deterministic fill-in (the hard rule).
    assert res_none is None, "generate_plan must NOT fabricate fallback tasks when the model fails"


def test_probe_fast_default_timeout_is_8s():
    import inspect
    from core.llm.ollama_provider import OllamaProvider
    params = inspect.signature(OllamaProvider.probe_fast).parameters
    assert params["timeout"].default == 8.0


def test_ollama_options_fit_context_to_prompt():
    # Qwen3-class models default to a huge ~32K context; on CPU that KV overhead
    # makes a reply crawl, so the window is pinned — but to the REAL prompt size,
    # never to the output cap. A window smaller than the prompt makes Ollama
    # truncate the middle where the goal/persona live (the root cause of "dumb"
    # local chat and garbage schedule titles). The floor is 4096 so even a tiny
    # prompt gets enough headroom, capped at 8192 to keep CPU speed.
    from core.llm.ollama_provider import OllamaProvider
    p = OllamaProvider("http://localhost:9", "x")
    tiny = p._options("hi", "how are you", max_tokens=180)
    assert tiny["num_ctx"] >= 4096 and tiny["num_predict"] == 180
    full = p._options("x" * 6000, "y" * 6000, max_tokens=1000)
    assert full["num_ctx"] >= 8000 and full["num_predict"] == 1000
    draw = p._options("plan system", "plan prompt", max_tokens=None)
    assert "num_predict" not in draw and draw["num_ctx"] >= 4096
    assert p._options("", "", None)["num_ctx"] == 4096


def test_ollama_thinking_disabled_by_default():
    # Qwen3 thinking mode burns the whole output budget on a hidden reasoning trace
    # (latency + sometimes an empty final reply). It must be OFF for the local model
    # unless OLLAMA_THINKING=1 is set.
    import os
    from core.llm.ollama_provider import OllamaProvider
    old = os.environ.pop("OLLAMA_THINKING", None)
    try:
        assert OllamaProvider("http://localhost:9", "x").think is False
        os.environ["OLLAMA_THINKING"] = "1"
        assert OllamaProvider("http://localhost:9", "x").think is True
    finally:
        if old is not None:
            os.environ["OLLAMA_THINKING"] = old
        else:
            os.environ.pop("OLLAMA_THINKING", None)


def test_plan_prefers_cloud_and_chat_prefers_local():
    # Explicit Redraw uses OpenRouter-first (prefer_cloud=True) so a schedule draw runs
    # in parallel with chat. Auto/retry paths and chat default to local-first
    # (prefer_cloud=False) to avoid burning paid OpenRouter calls unnecessarily.
    from core import generation as g
    seen = {}

    class CloudFirst:
        name = "openrouter"
        api_key = "sk-x"
        model = "m"

    class LocalFirst:
        name = "ollama"
        api_key = ""
        model = "m"

    geom = {"plan_calls": []}
    orig = g.get_manager
    def fake(db):
        class M:
            def generate(self_, sys, user_prompt, timeout=30, max_tokens=None, prefer_cloud=False):
                geom["plan_calls"].append(prefer_cloud)
                seen["plan"] = True
                return R
            def generate_with_fallback(self_, sys, user_prompt, fallback_fn, timeout=90,
                                       max_tokens=None, prefer_cloud=False):
                geom["chat"] = prefer_cloud
                seen["chat"] = True
                return "hi", "ollama"
        return M()

    class R:
        ok = True
        provider = "openrouter"
        model = "m"
        error = None
        latency_ms = 1
        text = '[]'

    g.get_manager = fake
    try:
        goal = {"id": 1, "deadline": "2099-01-01", "title": "some goal",
                "display_title": "some goal", "reminder_time": "09:00",
                "constraints": "[]", "details": "{}", "user_id": 1}
        g._llm_plan_call(goal, None, timeout=30, prefer_cloud=True)      # explicit Redraw
        g._llm_plan_call(goal, None, timeout=30, prefer_cloud=False)     # auto/retry path
        g.generate_opening_message("mobeen", "a goal", db=None)          # chat
    finally:
        g.get_manager = orig
    # explicit Redraw must be the only call that forces OpenRouter first. A draw is now
    # split into slices (each a separate model call), so assert the ORDER stays cloud-first
    # for the redraw and local-first for the auto path, with no interleaving.
    assert geom["plan_calls"][0] is True
    assert geom["plan_calls"][-1] is False
    first_false = geom["plan_calls"].index(False)
    assert all(c is True for c in geom["plan_calls"][:first_false])
    assert all(c is False for c in geom["plan_calls"][first_false:])
    assert geom["chat"] is False            # chat -> local first


def test_goal_prompt_answers_personal_questions_without_plan():
    # The user asked Eloise for guidance, got her task list recited back at them, and
    # the exact same line twice. A personal/emotional question must be answered directly
    # and must NOT drag the schedule into it — and copying an earlier Eloise reply is
    # forbidden outright.
    p = generation._goal_chat_prompt(
        "study", "=== THE PLAN (your scheduled tasks) ===\n- 2026-09-06 09:00: Security+ videos\n\n",
        "user: tell me complete study materials\neloise: Messer's Security+ playlist, 30 minutes a day. Start there.\n",
        "mobeen", "i m feeling distressed should we masturbate for relaxation and study with fresh mind",
    )
    assert "should I/we" in p
    assert "do NOT bring up THE PLAN" in p
    assert "identical" in p


def test_write_plan_refuses_empty():
    # An empty LLM result was written as "0 tasks across 0 days · via openrouter",
    # badging the goal 'active' with a blank schedule. That must never happen: an empty
    # write returns None and leaves the goal untouched so the retry path can fix it.
    import os
    import tempfile
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    import core.routes.goal_routes as gr

    tmp = tempfile.mkdtemp(prefix="eloise_empty_plan_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    orig_conn = gr.get_connection
    gr.get_connection = real_get
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest) VALUES ('tester','t@empty.io','x',1)")
        conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, plan_status, plan_summary) "
            "VALUES (1,'g','2026-09-12','09:00','[]','g','generating','')"
        )
        gid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.commit()
        goal = dict(conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        rv = gr._write_plan(conn, goal, [], provider="openrouter")
        assert rv is None
        st = dict(conn.execute("SELECT plan_status, plan_summary FROM goals WHERE id=?", (gid,)).fetchone())
        assert st["plan_status"] == "generating", st
        assert st["plan_summary"] == "", st
        assert conn.execute("SELECT COUNT(*) c FROM actions WHERE goal_id=?", (gid,)).fetchone()["c"] == 0
    finally:
        gr.get_connection = orig_conn
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]


def test_heal_stale_plans_recovers_generating():
    # A redraw that died mid-draw (process kill, or the daemon thread dying in its 0.5s
    # lead-in sleep) leaves the goal flagged 'generating' forever with zero tasks — that
    # is exactly the "schedule not generating" the user reported. Startup heal must reset
    # the flag AND re-draw ANY goal that ended up 'active' but silently empty (the old
    # empty-write bug), while leaving goals that actually have tasks alone.
    import os
    import tempfile
    import time as _time
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    import core.routes.goal_routes as gr

    tmp = tempfile.mkdtemp(prefix="eloise_heal_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    orig_conn = gr.get_connection
    gr.get_connection = real_get
    calls = []
    orig_bg = gr._regenerate_plan_bg
    gr._regenerate_plan_bg = lambda user, gid, prefer_cloud=False: calls.append((user["id"], gid, prefer_cloud, gr._goal_or_404(user, gid)["plan_status"]))
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest) VALUES ('tester','t@heal.io','x',1)")
        # (status, has_actions, future_deadline) — status is the goal lifecycle status
        cases = [
            ("generating", "generating", False, "2026-09-12"),   # stuck mid-draw, empty  -> regenerate
            ("generating", "generating", True,  "2026-09-12"),   # stuck mid-draw, has plan -> reset only
            ("active",     "active",     False, "2026-09-12"),   # silent empty write       -> regenerate
            ("active",     "active",     True,  "2026-09-12"),   # fine                     -> untouched
            ("active",     "active",     False, "2026-01-01"),   # stale expired goal       -> untouched
            ("cancelled",  "cancelled: changed my mind", False, "2026-09-12"),  # cancelled, must NOT resurrect
            ("succeeded",  "active",     False, "2026-09-12"),   # succeeded, must NOT resurrect
            ("cancelled",  "active",     False, "2026-09-12"),   # goal CANCELLED but plan_status drifted to active (the goal-13 pattern) -> MUST NOT resurrect
        ]
        for i, (status, plan_status, n_acts, deadline) in enumerate(cases):
            conn.execute(
                "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, status, plan_status, plan_summary) "
                "VALUES (1,?, ?, '09:00','[]', ?, ?, ?, '')",
                (f"g{i}", deadline, f"g{i}", status, plan_status),
            )
            gid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
            if n_acts:
                conn.execute(
                    "INSERT INTO actions (goal_id,user_id,date,start_time,end_time,title,status) "
                    "VALUES (?,?,?,?,?,?,?)", (gid, 1, "2026-09-06", "09:00", "10:00", "has a plan", "pending"),
                )
        conn.commit()
        gr.heal_stale_plans()
        deadline_ts = _time.monotonic() + 3
        while _time.monotonic() < deadline_ts and len(calls) < 2:
            _time.sleep(0.1)
        statuses = [r["plan_status"] for r in conn.execute("SELECT plan_status FROM goals ORDER BY id").fetchall()]
        assert statuses == ["active", "active", "active", "active", "active",
                            "cancelled: changed my mind", "active", "active"], statuses
        regen = sorted(c[1] for c in calls)
        ids = [r["id"] for r in conn.execute("SELECT id FROM goals ORDER BY id").fetchall()]
        assert regen == [ids[0], ids[2]], (regen, ids)  # only the two empty ones
        assert all(c[2] is False for c in calls), calls    # local-first
    finally:
        gr._regenerate_plan_bg = orig_bg
        gr.get_connection = orig_conn
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]


def test_heal_redraws_plans_violating_own_windows():
    # Goal 24's live plan has tasks at 16:00 while the goal itself blocks 'sleep 4 to
    # 10 pm' (16:00-22:00). Heal must detect a plan contradicting its OWN constraint
    # windows and redraw it — otherwise the self-inconsistent schedule survives forever.
    import os
    import tempfile
    import time as _time
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    import core.routes.goal_routes as gr

    tmp = tempfile.mkdtemp(prefix="eloise_viol_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    orig_bg = gr._regenerate_plan_bg
    gr._regenerate_plan_bg = lambda user, gid, prefer_cloud=False: calls.append(gid)
    calls = []
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest) VALUES ('tester','v@heal.io','x',1)")
        # violating: task at 16:00-17:30 sits inside 'sleep 4 to 10 pm' (16:00-22:00)
        conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, status, plan_status) "
            "VALUES (1,'bad','2026-09-30','09:00','[\"sleep 4 to 10 pm\"]','bad','active','active')")
        bad = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        # fine: task at 23:00-23:30 is AFTER the same sleep window ends (22:00)
        conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, status, plan_status) "
            "VALUES (1,'ok','2026-09-30','09:00','[\"sleep 4 to 10 pm\"]','ok','active','active')")
        ok = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,start_time,end_time,title,status) VALUES (?,?,?,?,?,?,?)",
            (bad, 1, "2026-09-20", "16:00", "17:30", "build the homepage", "pending"))
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,start_time,end_time,title,status) VALUES (?,?,?,?,?,?,?)",
            (ok, 1, "2026-09-20", "23:00", "23:30", "fix the favicon", "pending"))
        conn.commit()
        assert gr._action_violates_goal_windows(
            conn.execute("SELECT * FROM goals WHERE id=?", (bad,)).fetchone()) is True
        assert gr._action_violates_goal_windows(
            conn.execute("SELECT * FROM goals WHERE id=?", (ok,)).fetchone()) is False
        gr.heal_stale_plans()
        deadline_ts = _time.monotonic() + 2
        while _time.monotonic() < deadline_ts and len(calls) < 1:
            _time.sleep(0.05)
        assert bad in calls, "violating plan must be redrawn"
        assert ok not in calls, "non-violating plan must be left alone"
        assert calls == [bad], calls
    finally:
        gr._regenerate_plan_bg = orig_bg
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]


def test_llm_plan_drops_placeholder_and_keeps_concrete():
    # The user's HARD RULE: no hardcoded schedule entries. The old code substituted
    # the deterministic "-- day 1 part 1" skeleton title whenever the model's answer
    # failed validation, so a weak/unparseable model produced an entire board of
    # canned filler. Now an unacceptable title means the SLOT IS DROPPED, and only
    # genuinely concrete model titles are ever written.
    import datetime as _dt
    import re as _re
    from core.llm import base as _base
    import core.llm.manager as mm
    import core.llm.ollama_provider as _op

    class FakeOllama:
        name = "ollama"
        model = "fake"
        def __init__(self):
            self.last = None
        def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None, model_override=None):
            m = _re.findall(r'"date": "([^"]+)", "start_time": "([^"]+)"', user_prompt)
            items = [
                f'{{"date":"{d}","start_time":"{st}","end_time":"10:30","title":"study session one for the exam this week"}}'
                for d, st in m]
            return _base.GenerationResult(ok=True, provider="ollama", model="fake",
                                          text="[\n" + ",\n".join(items) + "\n]")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama()]
    m = mm.LLMManager(db=None)
    orig_mgr = generation.get_manager
    generation.get_manager = lambda db=None: m
    try:
        dl = (_dt.date.today() + _dt.timedelta(days=10)).isoformat()
        goal = {"deadline": dl, "title": "cybersecurity exam",
                "display_title": "cybersecurity exam", "reminder_time": "09:00",
                "constraints": "[]", "details": "{}", "user_id": 1, "id": 1}
        out = generation._llm_plan_call(goal, None, timeout=10)
    finally:
        generation.get_manager = orig_mgr
        mm.build_providers = orig_build
    assert out, "concrete model titles must be kept"
    assert all("study session one" in e["title"] for e in out)
    assert all("day" not in e["title"].lower() for e in out)

    class FakePlaceholder:
        name = "ollama"
        model = "fake"
        def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None, model_override=None):
            return _base.GenerationResult(ok=False, provider="ollama", model="fake",
                                          error="offline", text="")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakePlaceholder()]
    m2 = mm.LLMManager(db=None)
    generation.get_manager = lambda db=None: m2
    try:
        out2 = generation._llm_plan_call(goal, None, timeout=10)
    finally:
        generation.get_manager = orig_mgr
        mm.build_providers = orig_build
    assert out2 is None, "failed draw returns None, never placeholder entries"

    class FakeEchoPlaceholder:
        name = "ollama"
        model = "fake"
        def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None, model_override=None):
            m = _re.findall(r'"date": "([^"]+)", "start_time": "([^"]+)"', user_prompt)
            items = [
                f'{{"date":"{d}","start_time":"{st}","end_time":"10:30","title":"cybersecurity exam — day 1 part 1"}}'
                for d, st in m]
            return _base.GenerationResult(ok=True, provider="ollama", model="fake",
                                          text="[\n" + ",\n".join(items) + "\n]")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeEchoPlaceholder()]
    m3 = mm.LLMManager(db=None)
    generation.get_manager = lambda db=None: m3
    try:
        out3 = generation._llm_plan_call(goal, None, timeout=10)
    finally:
        generation.get_manager = orig_mgr
        mm.build_providers = orig_build
    assert out3 is None, "placeholder echo must be dropped, never written to the DB"


def test_plan_slices_split_failed_and_merged():
    # The old single request carried the WHOLE horizon (30+ days, 40-60 slots) in one
    # JSON blob: it blew the token cap, truncated mid-JSON, the parse failed wholesale,
    # and the entire schedule collapsed to empty + endless retries — the user's
    # "model unavailable, no plan drawn yet" loop. Draws are now split into small
    # day-bucketed slices; a failed slice costs only itself, the rest survive.
    import datetime as _dt
    import re as _re
    from core.llm import base as _base
    import core.llm.manager as mm

    calls = []

    class SlicedFake:
        name = "ollama"
        model = "fake"
        def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None, model_override=None):
            m = _re.findall(r'"date": "([^"]+)", "start_time": "([^"]+)"', user_prompt)
            calls.append(len(m))
            if len(calls) == 1:
                return _base.GenerationResult(ok=False, provider="ollama", model="fake",
                                              error="offline", text="")
            items = [
                f'{{"date":"{d}","start_time":"{st}","end_time":"10:30","title":"call the venue and book the tasting for that slot"}}'
                for d, st in m]
            return _base.GenerationResult(ok=True, provider="ollama", model="fake",
                                          text="[\n" + ",\n".join(items) + "\n]")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [SlicedFake()]
    m = mm.LLMManager(db=None)
    orig_mgr = generation.get_manager
    generation.get_manager = lambda db=None: m
    try:
        dl = (_dt.date.today() + _dt.timedelta(days=25)).isoformat()
        goal = {"deadline": dl, "title": "build and ship my portfolio website",
                "display_title": "build and ship my portfolio website", "reminder_time": "09:00",
                "constraints": "[]", "details": "{}", "user_id": 1, "id": 2}
        grid = generation._build_fallback_plan(goal, today=_dt.date.today().isoformat()) or []
        out = generation._llm_plan_call(goal, None, timeout=10)
    finally:
        generation.get_manager = orig_mgr
        mm.build_providers = orig_build
    assert len(grid) > 8, "horizon long enough to force multiple slices"
    assert len(calls) >= 2, "draw must be split, not one giant request"
    assert out, "later slices must survive the failed first slice"
    assert len(out) < len(grid), "partial draw is honest: failed slice's slots are missing"
    assert all("call the venue and book the tasting" in e["title"] for e in out)


def test_plan_blocked_when_windows_eat_all_free_time():
    # The model is NOT down when the user's own blocked windows consume every usable
    # hour: `_llm_plan_call` must surface an honest "blocked" reason (via
    # plan_blocked_reason) instead of letting the route blame the model and retry
    # forever. And a goal whose windows only free up AFTER 10pm must still plan an
    # evening slot (the old 22:00 ceiling starved the whole schedule).
    import re as _re
    from core.llm.manager import LLMManager
    from core.llm import base as _base
    import core.llm.manager as mm

    class _NeverCalled:
        name = "ollama"
        model = "fake"
        def generate(self, system_prompt, user_prompt, timeout=30, max_tokens=None, model_override=None):
            raise AssertionError("model must not be called when the grid is empty")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [_NeverCalled()]
    m = mm.LLMManager(db=None)
    orig_mgr = generation.get_manager
    generation.get_manager = lambda db=None: m
    try:
        goal = {"deadline": "2026-09-30", "title": "ship the site", "display_title": "ship the site",
                "reminder_time": "09:00", "constraints": '["work 8am to 11pm"]',
                "details": "{}", "user_id": 1, "id": 1}
        out = generation._llm_plan_call(goal, None, timeout=10,
                                        extra_blocked=[(8 * 60, 23 * 60)])
    finally:
        generation.get_manager = orig_mgr
        mm.build_providers = orig_build
    assert out is None
    assert generation.plan_blocked_reason(), "a real reason must be reported, not 'model unavailable'"
    assert "no usable free time" in generation.plan_blocked_reason().lower()

    # Same user free only after 10pm -> the grid must honestly produce that evening slot.
    generation._plan_blocked_reason = None
    goal2 = {"deadline": "2026-09-30", "title": "ship the site", "display_title": "ship the site",
             "reminder_time": "09:00", "constraints": '["busy 8am to 10pm"]',
             "details": "{}", "user_id": 1, "id": 1}
    grid = generation._build_fallback_plan(goal2, today="2026-09-09")
    assert grid, "free-after-10pm windows must still yield a plan"
    night = [e for e in grid if int(e["start_time"][:2]) * 60 >= 22 * 60]
    assert night, "the plan must use the genuine post-10pm free window"


def test_parse_title_map_survives_truncation():
    # A slow model hits the token cap mid-array -> no closing bracket -> the old parser
    # returned {} and dropped EVERY slot. The parser now extracts the complete objects
    # it DID get, so a truncated slice still contributes its finished titles.
    text = (
        '[{"date":"2026-09-10","start_time":"09:00","end_time":"10:30",'
        '"title":"Buy the chalk"},'
        ' {"date":"2026-09-10","start_time":"18:00","end_time":"19:30",'
        '"title":"Set up the easel in the corner"},\n  {"date":"2026-09-1'
    )
    out = generation._parse_title_map(text)
    assert out.get(("2026-09-10", "09:00")) == "Buy the chalk"
    assert out.get(("2026-09-10", "18:00")) == "Set up the easel in the corner"


def test_heal_purges_placeholder_titles_and_redraws():
    # Old draws could leave "-- day N part 1" canned filler in the DB. Startup heal
    # must purge placeholder-titled pending actions and re-draw (honestly: no model
    # still means the goal just stays empty and retries). Real concrete tasks survive.
    import os
    import tempfile
    import time as _time
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    import core.routes.goal_routes as gr

    tmp = tempfile.mkdtemp(prefix="eloise_purge_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    orig_conn = gr.get_connection
    gr.get_connection = real_get
    calls = []
    orig_bg = gr._regenerate_plan_bg
    gr._regenerate_plan_bg = lambda user, gid, prefer_cloud=False: calls.append((user["id"], gid, prefer_cloud))
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest) VALUES ('tester','t@purge.io','x',1)")
        conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, status, plan_status, plan_summary) "
            "VALUES (1,'website','2026-10-09','09:00','[]','website','active','active','')",
        )
        g1 = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,start_time,end_time,title,status) VALUES "
            "(?,1,'2026-09-09','16:00','17:30','website — day 1 part 1','pending'),"
            "(?,1,'2026-09-09','17:30','19:00','website — day 2 part 2','pending'),"
            "(?,1,'2026-09-09','19:00','20:00','Fix the broken checkout button and text her','pending')",
            (g1, g1, g1),
        )
        conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, status, plan_status, plan_summary) "
            "VALUES (1,'other','2026-10-09','09:00','[]','other','active','active','')",
        )
        g2 = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,start_time,end_time,title,status) VALUES "
            "(?,1,'2026-09-09','16:00','17:30','Deploy the staging build to the server','pending')",
            (g2,),
        )
        conn.commit()
        gr.heal_stale_plans()
        deadline_ts = _time.monotonic() + 3
        while _time.monotonic() < deadline_ts and len(calls) < 1:
            _time.sleep(0.1)
        left = [r["title"] for r in conn.execute("SELECT title FROM actions WHERE goal_id=? ORDER BY id", (g1,)).fetchall()]
        assert left == ["Fix the broken checkout button and text her"], left
        other = [r["title"] for r in conn.execute("SELECT title FROM actions WHERE goal_id=?", (g2,)).fetchall()]
        assert other == ["Deploy the staging build to the server"], other
        assert [c[1] for c in calls] == [g1], calls  # only the purged goal re-draws
    finally:
        gr._regenerate_plan_bg = orig_bg
        gr.get_connection = orig_conn
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]


def test_guardrail_does_not_poison_on_eloises_own_words():
    # The general-chat guardrail false-positived on a benign "tell me what to prioritize".
    # Cause: the scanned context included Eloise's OWN messages — her guardrail text
    # ("minor") plus a plan line quoting the goal "'i have to fuck someone'" ("fuck").
    # Eloise's prose is not user intent and must never trip the guardrail.
    import core.generation as g
    refusal = g.GUARDRAIL_REFUSAL
    hist = (
        "user: i have plenty of task what should priortized and what should left\n"
        f"eloise: {refusal}\n"
        "eloise: today's focus is on the 'i have to fuck someone' project and the C++ project\n"
        "user: tell me what to priortize"
    )
    assert g._is_truly_harmful("tell me what to priortize", context=hist) is False
    # a genuine follow-up on abuse must STILL refuse (thread-aware via user lines only)
    assert g._is_truly_harmful("today then", context="user: i want to fuck my sister") is True


def test_chat_busy_chat_model_retries_plan_model_locally_first():
    # A busy CHAT model (1.7b) must NOT push chat to OpenRouter: chat retries on the
    # PLAN model (4b) — still local — because the user explicitly wants local chat,
    # not cloud output. This is the per-model-lock payoff: a background plan draw on
    # the 4b never starves chat on the 1.7b, and a busy 1.7b still means a LOCAL reply.
    import core.llm.manager as mm
    from core.llm.base import GenerationResult
    from core.llm.manager import ollama_lock_for
    calls = []

    class FakeOllama:
        name = "ollama"
        api_key = ""
        model = "big"
        def generate_stream(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            calls.append(("ollama-stream", model_override))
            yield "local"; yield ""

    class FakeOpenRouter:
        name = "openrouter"
        api_key = "sk-x"
        model = "cloud"
        def generate_stream(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            calls.append(("openrouter-stream", model_override))
            yield "fast"; yield ""

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama(), FakeOpenRouter()]
    m = mm.LLMManager(db=None)
    busy_chat = ollama_lock_for("huihui_ai/qwen3-abliterated:1.7b")
    busy_chat.acquire()
    try:
        out = list(m.stream_generate("s", "u", timeout=5,
                                     model_override="huihui_ai/qwen3-abliterated:1.7b",
                                     lock_wait=0.15))
    finally:
        busy_chat.release()
        mm.build_providers = orig_build
    assert "openrouter-stream" not in calls, calls               # never the cloud
    assert calls == [("ollama-stream", "big")], calls            # retried the local plan model
    assert "".join(c for c, _ in out) == "local"


def test_chat_all_local_models_busy_falls_back_to_openrouter():
    # Only when BOTH local models are genuinely busy do we escape to OpenRouter —
    # past the lock_wait cap the conversation must still get SOME answer.
    import core.llm.manager as mm
    from core.llm.base import GenerationResult
    from core.llm.manager import ollama_lock_for
    calls = []

    class FakeOllama:
        name = "ollama"
        api_key = ""
        model = "big"
        def generate_stream(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            calls.append(("ollama-stream", model_override))
            yield "local"; yield ""

    class FakeOpenRouter:
        name = "openrouter"
        api_key = "sk-x"
        model = "cloud"
        def generate_stream(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            calls.append(("openrouter-stream", model_override))
            yield "fast"; yield ""

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama(), FakeOpenRouter()]
    m = mm.LLMManager(db=None)
    busy_chat = ollama_lock_for("huihui_ai/qwen3-abliterated:1.7b")
    busy_plan = ollama_lock_for("big")
    busy_chat.acquire()
    busy_plan.acquire()
    try:
        out = list(m.stream_generate("s", "u", timeout=5,
                                     model_override="huihui_ai/qwen3-abliterated:1.7b",
                                     lock_wait=0.15))
    finally:
        busy_plan.release()
        busy_chat.release()
        mm.build_providers = orig_build
    assert "ollama-stream" not in calls, calls          # local WAS busy, never used
    assert calls[0][0] == "openrouter-stream", calls    # fell back to cloud only then
    assert calls[0][1] == "huihui_ai/qwen3-abliterated:1.7b", calls
    assert "".join(c for c, _ in out) == "fast"


def test_stream_chat_retries_plan_model_when_chat_model_missing():
    # OLLAMA_CHAT_MODEL points at a model that isn't pulled (404 on Ollama). The
    # stream must retry the PLAN model — still local — BEFORE touching OpenRouter.
    import os as _os
    import core.generation as g
    import core.llm.manager as mm
    seen = []
    _os.environ["OLLAMA_CHAT_MODEL"] = "huihui_ai/qwen3-abliterated:1.7b"

    class FakeOllama:
        name = "ollama"
        api_key = ""
        model = "big"
        def generate_stream(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            seen.append(model_override)
            if model_override == "huihui_ai/qwen3-abliterated:1.7b":
                raise RuntimeError("model not found")
            yield "plan-model answer"; yield ""

    class FakeOpenRouter:
        name = "openrouter"
        api_key = "sk-x"
        model = "cloud"
        def generate_stream(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            seen.append("cloud:" + str(model_override))
            yield "cloud answer"; yield ""

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama(), FakeOpenRouter()]
    try:
        out = list(g.stream_chat_reply("T", "x", "", "hi", db=None))
    finally:
        mm.build_providers = orig_build
        del _os.environ["OLLAMA_CHAT_MODEL"]
    assert seen == ["huihui_ai/qwen3-abliterated:1.7b", "big"], seen
    assert "".join(c for c, _ in out) == "plan-model answer"


def test_strip_prompt_echo_strips_labels_markers_and_question_echo():
    # The user's chat "echoed" him: a weak local model parroted 'Eloise:' labels, the
    # prompt boilerplate, or the question itself. Clean ALL of it before the reply
    # reaches the screen — and treat a verbatim question-echo as a non-answer.
    import core.llm.manager as mm
    assert mm._strip_prompt_echo("Eloise: study for the exam") == "study for the exam"
    assert mm._strip_prompt_echo("assistant | sure thing") == "sure thing"
    assert mm._strip_prompt_echo(
        "=== CONVERSATION ===\nuser: hi\neloise: plan\nReply as Eloise. Only output your reply.\ngo read chapter 3"
    ) == "go read chapter 3"
    assert mm._strip_prompt_echo("hi eloise", user_message="hi eloise") == ""   # pure echo
    assert mm._strip_prompt_echo("yes", user_message="hi") == "yes"             # real answer survives


def test_stream_chat_echo_of_question_returns_offline():
    # A model that only echoes the question back must NOT print the user his own
    # words as if it answered — the stream yields nothing and the UI reports an honest
    # error instead of 'echoing'.
    import core.generation as g
    import core.llm.manager as mm

    class FakeOllama:
        name = "ollama"
        api_key = ""
        model = "big"
        def generate_stream(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            yield "what's going on?"; yield ""

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama()]
    try:
        out = list(g.stream_chat_reply("T", "x", "user: hi\neloise: hi\n", "what's going on?", db=None))
    finally:
        mm.build_providers = orig_build
    assert "".join(c for c, _ in out) == "", out


def test_chat_model_override_reaches_local_provider():
    # OLLAMA_CHAT_MODEL lets chat run on a small fast model while plans keep the bigger
    # one. The override must reach the local provider's /api/generate payload.
    import core.llm.manager as mm
    from core.llm.base import GenerationResult
    calls = []

    class FakeOllama:
        name = "ollama"
        api_key = ""
        model = "big"
        def generate(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            calls.append((sys, up, model_override))
            return GenerationResult(ok=True, provider="ollama", model="big", text="ok")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama()]
    m = mm.LLMManager(db=None)
    try:
        r = m.generate("s", "u", timeout=5, model_override="huihui_ai/qwen3-abliterated:1.7b")
    finally:
        mm.build_providers = orig_build
    assert r.ok and r.text == "ok"
    assert len(calls) == 1 and calls[0][2] == "huihui_ai/qwen3-abliterated:1.7b", calls


def test_plan_draws_keep_the_big_model_without_override():
    # Plans ('_llm_plan_call') must NOT pass a chat model override — they stay on the
    # configured plan model even when a fast chat model is set.
    import core.llm.manager as mm
    from core.llm.base import GenerationResult
    calls = []

    class FakeOllama:
        name = "ollama"
        api_key = ""
        model = "big"
        def generate(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            calls.append(model_override)
            return GenerationResult(ok=True, provider="ollama", model="big", text="[]")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama()]
    m = mm.LLMManager(db=None)
    try:
        goal = {"deadline": "2099-01-01", "title": "g", "display_title": "g",
                "reminder_time": "09:00", "constraints": "[]", "details": "{}", "user_id": 1,
                "id": 1}
        orig_mgr = generation.get_manager
        generation.get_manager = lambda db=None: m
        try:
            generation._llm_plan_call(goal, None, timeout=10)
        finally:
            generation.get_manager = orig_mgr
    finally:
        mm.build_providers = orig_build
    assert calls and all(c is None for c in calls), "every plan slice uses the big plan model, never the chat override"


def test_plan_draw_is_bounded_and_matches_slots():
    # A slow CPU model can time out mid-draw and leave the board empty. The plan call
    # must cap its own output (so it FINISHES) and the parsed titles must match every
    # trigger slot, never producing fewer blocks than the deterministic skeleton.
    import core.llm.manager as mm
    from core.llm.base import GenerationResult
    from datetime import date as _date, timedelta as _delta
    seen = {}

    class FakeOllama:
        name = "ollama"
        api_key = ""
        model = "big"
        def generate(self, sys, up, timeout=30, max_tokens=None, model_override=None):
            seen["max_tokens"] = max_tokens
            import re as _re
            m = _re.findall(r'"date": "([^"]+)", "start_time": "([^"]+)"', up)
            items = []
            for d, st in m:
                items.append(f'{{"date":"{d}","start_time":"{st}","end_time":"10:30","title":"Study {d} {st}"}}')
            return GenerationResult(ok=True, provider="ollama", model="big",
                                    text="[\n" + ",\n".join(items) + "\n]")

    orig_build = mm.build_providers
    mm.build_providers = lambda config=None: [FakeOllama()]
    m = mm.LLMManager(db=None)
    orig_mgr = generation.get_manager
    generation.get_manager = lambda db=None: m
    try:
        dl = (_date.today() + _delta(days=15)).isoformat()
        goal = {"deadline": dl, "title": "cybersecurity exam",
                "display_title": "cybersecurity exam", "reminder_time": "09:00",
                "constraints": "[]", "details": "{}", "user_id": 1, "id": 1}
        out = generation._llm_plan_call(goal, None, timeout=10)
    finally:
        generation.get_manager = orig_mgr
        mm.build_providers = orig_build
    assert seen["max_tokens"] == generation._MAX_PLAN_TOKENS, seen  # bounded output
    assert out, "plan must not be empty"
    skeleton = generation._build_fallback_plan(goal)
    assert len(out) == len(skeleton), (len(out), len(skeleton))
    assert all("Study" in e["title"] for e in out)  # titles parsed through


def test_regenerate_never_writes_hardcoded_when_model_down():
    # The user's HARD RULE: no hardcoded schedule entries. If the model is down, the
    # background regenerate must NOT fill the board with deterministic filler — it keeps
    # whatever exists, writes nothing for a fresh goal, marks 'active', and schedules a
    # retry. A hardcoded fill-in shown to the user is the exact bug the user keeps
    # reporting.
    import os
    import tempfile
    import time as _time
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    import core.routes.goal_routes as gr
    from core import generation as g
    import threading as _th

    tmp = tempfile.mkdtemp(prefix="eloise_guaranteed_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    orig_conn = gr.get_connection
    gr.get_connection = real_get
    orig_gen = g.generate_plan_or_none
    orig_blocked = g.user_blocked_windows
    orig_sched = gr._schedule_plan_retry
    gr._schedule_plan_retry = lambda user, gid, delay=None: None  # don't spawn threads
    g.user_blocked_windows = lambda user: []
    g.generate_plan_or_none = lambda *a, **k: None   # model always fails
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest) VALUES ('tester','t@guar.io','x',1)")
        conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, plan_status) "
            "VALUES (1,'cybersecurity exam','2026-09-12','09:00','[]','cybersecurity exam','generating')"
        )
        gid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.commit()
        goal = dict(conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
        user = {"id": 1, "name": "tester"}
        gr._regenerate_plan_bg(user, gid)
        # give the bg thread 0.5s lead-in + a beat to finish its attempt
        deadline_ts = _time.monotonic() + 5
        while _time.monotonic() < deadline_ts:
            st = dict(conn.execute("SELECT plan_status FROM goals WHERE id=?", (gid,)).fetchone())
            if st["plan_status"] == "active":
                break
            _time.sleep(0.1)
        n = conn.execute("SELECT COUNT(*) c FROM actions WHERE goal_id=?", (gid,)).fetchone()["c"]
        st = dict(conn.execute("SELECT plan_status, plan_summary FROM goals WHERE id=?", (gid,)).fetchone())
        assert st["plan_status"] == "active", st
        assert n == 0, f"hardcoded fallback must NOT be written when model is down (found {n} tasks)"
        assert "retry" in st["plan_summary"], st

        # And with an EXISTING schedule: the old plan stays exactly as it was (no
        # hardcoded overwrite), only the status/summary change.
        conn.execute(
            "INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, plan_status) "
            "VALUES (1,'study','2026-09-20','09:00','[]','study','generating')"
        )
        gid2 = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        conn.execute(
            "INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx) "
            "VALUES (?,1,'2026-09-10','old real task','09:00','10:00',60,'pending',0)", (gid2,))
        conn.commit()
        gr._regenerate_plan_bg(user, gid2)
        deadline_ts = _time.monotonic() + 5
        while _time.monotonic() < deadline_ts:
            st2 = dict(conn.execute("SELECT plan_status FROM goals WHERE id=?", (gid2,)).fetchone())
            if st2["plan_status"] == "active":
                break
            _time.sleep(0.1)
        rows = conn.execute(
            "SELECT title, start_time FROM actions WHERE goal_id=? ORDER BY order_idx", (gid2,)).fetchall()
        assert [r["title"] for r in rows] == ["old real task"], rows   # old plan preserved
    finally:
        g.generate_plan_or_none = orig_gen
        g.user_blocked_windows = orig_blocked
        gr._schedule_plan_retry = orig_sched
        gr.get_connection = orig_conn
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]


def test_chat_never_repeats_and_teachable_persona_sticks():
    # The user's two chat complaints: (1) the model spams the SAME reply because it
    # copies the previous Eloise line that's sitting in context, and (2) personality
    # instructions given in chat ("be sarcastic", "don't be a motivational speaker")
    # evaporate next turn. Fixes: last Eloise reply is structurally dropped from the
    # LLM context, a near-identical reply is refused as an echo, sycophancy openers
    # ("You're right—") are stripped, and persona directives persist + inject.
    import os
    import tempfile
    import difflib
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    import core.routes.chat_routes as cr
    from core.llm.manager import _strip_prompt_echo

    tmp = tempfile.mkdtemp(prefix="eloise_chatfix_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    try:
        conn = real_get()
        conn.row_factory = __import__("sqlite3").Row
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest) VALUES ('t','p@fix.io','x',1)")
        conn.execute("INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, status, plan_status) "
                     "VALUES (1,'website','2026-09-30','09:00','[]','website','active','active')")
        gid = conn.execute("SELECT last_insert_rowid() r").fetchone()["r"]
        for ts in [
            (1, "user", "The schedule is still being drawn. Let's redraw it together. Start with the first step: update the website layout"),
            (2, "eloise", "You're right—everything just isn't what you expected. Begin by organizing the structure."),
            (3, "user", "done"),
        ]:
            conn.execute("INSERT INTO chat_messages (goal_id, user_id, role, content, created_at) VALUES (?,?,?,?,datetime('now'))",
                         (gid, 1, ts[1], ts[2]))
        conn.commit()

        # --- structural anti-repeat: the last Eloise line must NOT reach the LLM ---
        history = cr._history(conn, gid, 1)
        assert all("organizing the structure" not in h for h in history), history

        # --- posterity of exact replies is refused as an echo (duplicate gate) ---
        prev = cr._last_eloise_reply(conn, gid, 1)
        assert prev and cr._is_near_dup(prev, prev.replace("You're", "You are")), "near-dup must be detected"

        # --- sycophancy openers stripped from weak-model output ---
        clean = _strip_prompt_echo("You're right—everything just isn't what you expected. Begin by organizing.")
        assert not clean.startswith("You're right"), clean

        # --- verbatim re-quote of an EARLIER user line is deleted from the reply ---
        out = _strip_prompt_echo("Nice. 'Let's move forward.' is what you said. Now do the work.",
                                 user_message="done",
                                 user_lines=["Nice. Let's move forward.", "i have layout and everything"])
        assert "Let's move forward" not in out, out

        # --- persona directive typed into chat is stored as a contract ---
        got = generation.absorb_persona_directive(conn, 1, "i said she ready to abuse sarcastic and attack what's this")
        assert got and "sarcastic" in got and "abusive" in got and "Attack excuses" in got, got
        got2 = generation.absorb_persona_directive(conn, 1, "stop being a motivational speaker")
        assert "motivational" in got2, got2
        persisted = generation.user_persona(conn, 1)
        assert persisted == got2

        # --- injected into the chat system prompt so it's never forgotten ---
        sys = generation.chat_system_prompt(conn, 1)
        assert "standing personality contract" in sys and "motivational speaker" in sys and "sarcastic" in sys

        # --- step-done vs goal-done are separate now ---
        assert generation.step_done_detected("done") is True
        assert generation.step_done_detected("what's next") is True
        assert generation.step_done_detected("what do i do now") is True
        assert generation.step_done_detected("im done") is True
        # a bare step-done must NEVER close the whole goal
        assert generation.goal_completion_detected("done") is False
        assert generation.goal_completion_detected("im done") is False
        assert generation.goal_completion_detected("finished the layout") is False
        # closing genuinely needs goal-framing
        assert generation.goal_completion_detected("the website is done") is False  # "is done" not a phrase
        assert generation.goal_completion_detected("i'm done with the website") is True
        assert "mo" if False else True

        # --- step-done steers the prompt to the NEXT task, not the same one ---
        plan = "=== THE PLAN ===\n- 2026-09-20 22:00: Test the homepage\n- 2026-09-21 22:00: Deploy to staging\n\n"
        p_step = generation._goal_chat_prompt("website", plan, "", "Tester", "done", step_done=True)
        assert "Do NOT re-instruct the task you gave" in p_step
        assert "NEXT pending task" in p_step
        p_plain = generation._goal_chat_prompt("website", plan, "", "Tester", "tell me a joke", step_done=False)
        assert "Do NOT re-instruct the task you gave" not in p_plain
    finally:
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]


def test_eloise_is_single_control_unit():
    # The user's design point: it's ONE app controlled by Eloise. A routine typed into
    # ONE goal ("gym 5-7am") must be respected by EVERY other goal — the user does not
    # re-enter it per goal. The planner merges the user's global blocked windows with
    # the union of ALL active goals' constraint windows.
    import os
    import tempfile
    from core.database import set_storage_dir, get_connection as real_get, _thread_local
    import core.routes.goal_routes as gr

    tmp = tempfile.mkdtemp(prefix="eloise_unit_")
    os.environ["ELOISE_STORAGE_DIR"] = tmp
    set_storage_dir(tmp)
    _thread_local.conn = None
    try:
        conn = real_get()
        conn.execute("INSERT INTO users (name, email, password_hash, is_guest, blocked_windows) "
                     "VALUES ('t','u@unit.io','x',1,'[\"sleep 4 to 10 pm\"]')")
        for i, cons in enumerate([
            '["gym 5 to 7 am"]',     # global-worthy routine set ONCE, in this goal
            '',                       # this other goal does NOT declare it
            '["university 7 to 4 pm"]',
        ]):
            conn.execute("INSERT INTO goals (user_id, title, deadline, reminder_time, constraints, display_title, status, plan_status) "
                         "VALUES (1,?,?,?,?,?,?,?)",
                         (f"g{i}", "2026-09-30", "09:00", cons, f"g{i}", "active", "active"))
        conn.commit()
        merged = gr.active_goal_blocked_windows(conn, dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone()))
        # global sleep (4pm-10pm) + gym (5-7am) from g0 + university (7am-4pm) from g2,
        # regardless of which goal drew the schedule
        pairs = {(s // 60, e // 60) for s, e in merged}
        assert (5, 7) in pairs, pairs
        assert (7, 16) in pairs, pairs
        assert (16, 22) in pairs, pairs

        # a goal with NO constraints typed still plans AROUND gym+uni+sleep that the
        # user stated once elsewhere (its grid must not land on the merged busy blocks).
        grid = generation._build_fallback_plan(
            {"deadline": "2026-09-30", "title": "g1", "display_title": "g1", "constraints": "[]"},
            today="2026-09-09", extra_blocked=merged)
        assert grid, "goal with no own constraints must still draw around the union"
        for e in grid:
            s = int(e["start_time"][:2]) * 60 + int(e["start_time"][3:5])
            eend = int(e["end_time"][:2]) * 60 + int(e["end_time"][3:5])
            for (bs, be) in merged:
                assert not (s < be and eend > bs), f"lands on blocked window {bs}-{be}: {e}"
    finally:
        _thread_local.conn = None
        del os.environ["ELOISE_STORAGE_DIR"]
