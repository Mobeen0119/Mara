"""Bugs and half-wired features from the audit: each fails on the original code."""
import json
from datetime import date, timedelta

import pytest

from tests.conftest import auth, signup


@pytest.fixture()
def user(client, monkeypatch):
    monkeypatch.setattr("core.routes.goal_routes.submit_plan", lambda *a, **k: True)
    monkeypatch.setattr("core.routes.settings_routes.submit_plan", lambda *a, **k: True)
    monkeypatch.setattr("core.routes.checkin_routes.submit_plan", lambda *a, **k: True)
    s = signup(client)
    s["h"] = auth(s["token"])
    return s


def _tomorrow(n=1):
    return (date.today() + timedelta(days=n)).isoformat()


def mk(client, user, title="learn guitar", days=5):
    r = client.post("/api/goals", headers=user["h"], json={"title": title, "deadline": _tomorrow(days)})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ----- validation: 'banana' deadlines used to poison /scoreboard with a 500 ---------
@pytest.mark.parametrize("bad", ["banana", "2026-13-45", "", "26-10-2026", "1999-01-01"])
def test_invalid_deadline_rejected(client, user, bad):
    r = client.post("/api/goals", headers=user["h"], json={"title": "x", "deadline": bad})
    assert r.status_code == 422


def test_past_deadline_rejected_and_scoreboard_survives(client, user):
    r = client.post("/api/goals", headers=user["h"], json={"title": "x", "deadline": "2020-01-01"})
    assert r.status_code == 400
    mk(client, user)
    for path in ("/api/scoreboard", "/api/today", "/api/goals", "/api/schedule", "/api/momentum", "/api/eloise"):
        assert client.get(path, headers=user["h"]).status_code == 200, path


def test_legacy_non_date_deadline_row_does_not_500_the_scoreboard(client, user):
    from core.database import get_connection
    gid = mk(client, user)
    conn = get_connection()
    conn.execute("UPDATE goals SET deadline='banana' WHERE id=?", (gid,))
    conn.commit()
    assert client.get("/api/scoreboard", headers=user["h"]).status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/api/settings/checkin-time", {"checkin_time": "25:99"}),
])
def test_time_fields_validated(client, user, path, body):
    assert client.put(path, headers=user["h"], json=body).status_code in (400, 422)


def test_reminder_time_validated(client, user):
    gid = mk(client, user)
    assert client.post(f"/api/goals/{gid}/reminder", headers=user["h"], json={"reminder_time": "soon"}).status_code == 422


# ----- the content guardrail applies on EVERY path, not just create ------------------
def test_guardrail_applies_on_edit_and_on_questionnaire_answers(client, user):
    gid = mk(client, user)
    bad = "force her to have sex"
    assert client.post("/api/goals", headers=user["h"], json={"title": bad, "deadline": _tomorrow()}).status_code == 400
    assert client.put(f"/api/goals/{gid}", headers=user["h"], json={"title": bad}).status_code == 400
    assert client.post(f"/api/goals/{gid}/details", headers=user["h"], json={"note": bad}).status_code == 400
    assert client.get(f"/api/goals/{gid}", headers=user["h"]).json()["title"] == "learn guitar"


def test_goal_details_are_bounded_and_flat(client, user):
    gid = mk(client, user)
    assert client.post(f"/api/goals/{gid}/details", headers=user["h"], json={"k" * 41: "x"}).status_code == 400
    assert client.post(f"/api/goals/{gid}/details", headers=user["h"], json={"a": {"nested": 1}}).status_code == 400
    assert client.post(f"/api/goals/{gid}/details", headers=user["h"], json={f"k{i}": "x" for i in range(31)}).status_code == 400
    assert client.post(f"/api/goals/{gid}/details", headers=user["h"], json={"time_per_day": "2 hours"}).status_code == 200


# ----- ownership ------------------------------------------------------------------------
def test_users_cannot_touch_each_others_goals(client, user):
    gid = mk(client, user)
    other = signup(client, "other@example.com", "Other")
    h = auth(other["token"])
    assert client.get(f"/api/goals/{gid}", headers=h).status_code == 404
    assert client.put(f"/api/goals/{gid}", headers=h, json={"title": "pwn"}).status_code == 404
    assert client.post(f"/api/goals/{gid}/delete", headers=h, json={}).status_code == 404
    assert client.post("/api/chat", headers=h, json={"goal_id": gid, "message": "hi"}).status_code == 404
    assert client.get(f"/api/chat/goals/{gid}", headers=h).status_code == 404


# ----- the missing `import os` made ELOISE_TEST_TODAY silently ignored ------------------
def test_test_today_override_works(monkeypatch):
    monkeypatch.setenv("ELOISE_TEST_TODAY", "2031-02-03")
    from core.routes import goal_routes
    assert goal_routes._get_today().isoformat() == "2031-02-03"


# ----- timezones: 'today' is the USER's today --------------------------------------------
def test_today_follows_the_users_timezone(client, user):
    r = client.put("/api/settings/timezone", headers=user["h"], json={"timezone": "Pacific/Kiritimati"})   # UTC+14
    assert r.status_code == 200
    r2 = client.put("/api/settings/timezone", headers=user["h"], json={"timezone": "Etc/GMT+12"})           # UTC-12
    assert r2.status_code == 200
    assert client.put("/api/settings/timezone", headers=user["h"], json={"timezone": "Mars/Base"}).status_code == 400
    from core.clock import now_for
    from core.database import get_connection
    u = get_connection().execute("SELECT * FROM users WHERE id=1").fetchone()
    assert now_for(u).date() <= date.today() + timedelta(days=1)
    assert client.get("/api/day", headers=user["h"]).json()["timezone"] == "Etc/GMT+12"


# ----- check-ins: once per local day, from the check-in time ------------------------------
def test_checkin_due_once_a_day(client, user, monkeypatch):
    mk(client, user)
    monkeypatch.setenv("ELOISE_TEST_TODAY", date.today().isoformat())
    from core.database import get_connection
    conn = get_connection()
    conn.execute("UPDATE users SET checkin_time='00:00', timezone='UTC'")
    conn.commit()
    assert client.get("/api/checkin", headers=user["h"]).json()["due"] is True
    client.post("/api/checkin/respond", headers=user["h"], json={"done": True})
    assert client.get("/api/checkin", headers=user["h"]).json()["due"] is False


def test_no_goals_means_no_checkin(client, user):
    assert client.get("/api/checkin", headers=user["h"]).json()["due"] is False


# ----- scheduler: reminders, sweep, digest fire once per local day -------------------------
def test_scheduler_sweeps_reminds_and_never_double_fires(client, user):
    from datetime import datetime
    from core.database import get_connection
    from core.scheduler import eloise_scheduler
    gid = mk(client, user)
    conn = get_connection()
    today, yday = date.today().isoformat(), (date.today() - timedelta(days=1)).isoformat()
    for d, s in ((yday, "pending"), (today, "pending")):
        conn.execute("INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,status) VALUES (?,1,?,?,?,?,?)",
                     (gid, d, "block", "09:00", "10:00", s))
    conn.execute("UPDATE goals SET reminder_time='06:00'")
    conn.commit()
    noon = lambda u: datetime.combine(date.today(), datetime.min.time()).replace(hour=12)
    eloise_scheduler.tick(now_fn=noon)
    eloise_scheduler.tick(now_fn=noon)
    eloise_scheduler.tick(now_fn=noon)
    assert conn.execute("SELECT status FROM actions WHERE date=?", (yday,)).fetchone()[0] == "missed"
    assert conn.execute("SELECT status FROM actions WHERE date=?", (today,)).fetchone()[0] == "pending"
    rem = conn.execute("SELECT message FROM interventions WHERE type='reminder'").fetchall()
    assert len(rem) == 1 and "first at 09:00" in rem[0][0]
    # before the reminder time: nothing
    conn.execute("DELETE FROM interventions"); conn.execute("DELETE FROM job_runs"); conn.commit()
    early = lambda u: datetime.combine(date.today(), datetime.min.time()).replace(hour=5)
    eloise_scheduler.tick(now_fn=early)
    assert conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0] == 0


def test_digest_is_mailed_once_a_day_and_only_to_confirmed_addresses(client, user, monkeypatch):
    from datetime import datetime
    from core.database import get_connection
    from core.scheduler import eloise_scheduler
    sent = []
    monkeypatch.setenv("SMTP_HOST", "h"); monkeypatch.setenv("SMTP_USER", "u"); monkeypatch.setenv("SMTP_PASS", "p")
    monkeypatch.setattr("core.mailer.send_email_detailed", lambda to, s, t="", h=None: sent.append((to, h)) or (True, ""))
    gid = mk(client, user)
    conn = get_connection()
    conn.execute("INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time) VALUES (?,1,?,?,?,?)",
                 (gid, date.today().isoformat(), "<b>x</b>", "09:00", "10:00"))
    conn.execute("UPDATE users SET verified=0"); conn.commit()
    noon = lambda u: datetime.combine(date.today(), datetime.min.time()).replace(hour=12)
    eloise_scheduler.tick(now_fn=noon)
    assert sent == []                                  # unconfirmed address: no mail
    conn.execute("UPDATE users SET verified=1"); conn.commit()
    eloise_scheduler.tick(now_fn=noon); eloise_scheduler.tick(now_fn=noon)
    assert len(sent) == 1
    assert "<b>x</b>" not in sent[0][1] and "&lt;b&gt;x" in sent[0][1]      # email HTML is escaped


# ----- stakes can't be collected on a goal that isn't actually lost ------------------------
def test_stake_enforcement_requires_a_missed_deadline(client, user):
    gid = mk(client, user)
    assert client.post(f"/api/goals/{gid}/stake", headers=user["h"], json={"goal_id": gid, "punishment": "no coffee"}).status_code == 200
    assert client.post(f"/api/goals/{gid}/stake/enforce", headers=user["h"]).status_code == 400
    from core.database import get_connection
    conn = get_connection()
    conn.execute("UPDATE goals SET deadline=?", ((date.today() - timedelta(days=2)).isoformat(),)); conn.commit()
    assert client.post(f"/api/goals/{gid}/stake/enforce", headers=user["h"]).status_code == 200


def test_delete_goal_leaves_nothing_behind(client, user):
    from core.database import get_connection
    gid = mk(client, user)
    client.post(f"/api/goals/{gid}/stake", headers=user["h"], json={"goal_id": gid, "punishment": "x"})
    client.post("/api/chat/nudge", headers=user["h"], json={"goal_id": gid})
    assert client.post(f"/api/goals/{gid}/delete", headers=user["h"], json={}).status_code == 200
    conn = get_connection()
    for t in ("stakes", "interventions", "actions", "chat_messages"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t} WHERE goal_id=?", (gid,)).fetchone()[0] == 0, t


def test_reschedule_quick_command_never_pushes_past_deadline(client, user):
    from core.database import get_connection
    gid = mk(client, user, days=2)
    conn = get_connection()
    dl = conn.execute("SELECT deadline FROM goals").fetchone()[0]
    conn.execute("INSERT INTO actions (goal_id,user_id,date,title) VALUES (?,1,?,?)", (gid, dl, "on deadline"))
    conn.commit()
    client.post("/api/chat/quick", headers=user["h"], json={"command": "reschedule"})
    assert conn.execute("SELECT date FROM actions").fetchone()[0] == dl


# ----- plan jobs: bounded and de-duplicated ----------------------------------------------------
def test_plan_submission_dedupes_and_reruns_once(monkeypatch):
    import threading
    from core.routes import goal_routes as g
    gate, calls = threading.Event(), []
    done = threading.Event()

    def slow(user, goal_id, prefer_cloud=False):
        calls.append(goal_id)
        if len(calls) == 1:
            gate.wait(5)
        else:
            done.set()
    monkeypatch.setattr(g, "_regenerate_plan_bg", slow)
    u = {"id": 1}
    assert g.submit_plan(u, 77) is True
    while not calls:
        pass
    assert g.submit_plan(u, 77) is False and g.submit_plan(u, 77) is False     # collapsed into ONE rerun
    gate.set()
    assert done.wait(5)
    assert calls == [77, 77]


def test_healthz_and_security_headers(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True
    h = client.get("/").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code in (200, 404) and "swagger" not in client.get("/docs").text.lower()


def test_oversized_body_rejected(client):
    r = client.post("/api/guest", content=b"x" * 1_100_000, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_constraint_in_one_goal_redraws_the_others(client, monkeypatch):
    calls = []
    monkeypatch.setattr("core.routes.goal_routes.submit_plan", lambda u, gid, **k: calls.append(gid) or True)
    u = signup(client)
    h = auth(u["token"])
    a = client.post("/api/goals", headers=h, json={"title": "A goal", "deadline": _tomorrow(5)}).json()["id"]
    calls.clear()
    b = client.post("/api/goals", headers=h, json={"title": "B goal", "deadline": _tomorrow(5), "constraints": ["uni 9 to 1"]}).json()["id"]
    assert set(calls) == {a, b}                       # B drawn, and A redrawn around B's uni hours
    calls.clear()
    client.post(f"/api/goals/{b}/constraints", headers=h, json={"text": "gym 5-7pm"})
    assert set(calls) == {a, b}
    calls.clear()
    client.post(f"/api/goals/{a}/constraints", headers=h, json={"removeIndex": 0})
    assert set(calls) == {a, b}


def test_clock_never_crashes_without_a_timezone_database(monkeypatch):
    """Windows without `tzdata`: ZoneInfo() raises for every name, even UTC."""
    from core import clock
    def boom(name):
        raise clock.ZoneInfoNotFoundError(name)
    monkeypatch.setattr(clock, "ZoneInfo", boom)
    assert clock.now_for({"timezone": "Asia/Karachi"}).year >= 2026
    assert clock.today_for(None)
    assert clock.valid_tz("Asia/Karachi") is False