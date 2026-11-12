from core import routine
from core.database import get_connection
from tests.conftest import signup, auth


def _user(client):
    h = auth(signup(client)["token"])
    return h, get_connection().execute("SELECT id FROM users").fetchone()["id"]


def test_defaults_are_reported_as_assumptions(client):
    _user(client)
    conn = get_connection()
    r = routine.merged(conn, dict(conn.execute("SELECT * FROM users").fetchone()))
    assert r["assumed"]
    assert r["sleep"] == routine.DEFAULT_SLEEP


def test_chat_statement_overrides_and_replans_all_goals(client):
    h, uid = _user(client)
    r = client.post("/api/chat", headers=h, json={"message": "I sleep 1am to 9am and gym at 7am"})
    assert r.json()["source"] == "routine"
    conn = get_connection()
    m = routine.merged(conn, dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()))
    assert m["sleep"][0] == 60 and m["sleep"][1] == 540
    assert m["workout"][0] == 420


def test_newest_statement_wins(client):
    h, uid = _user(client)
    client.post("/api/chat", headers=h, json={"message": "I sleep 11pm to 7am"})
    client.post("/api/chat", headers=h, json={"message": "I sleep 2am to 10am"})
    conn = get_connection()
    m = routine.merged(conn, dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()))
    assert m["sleep"] == [120, 600] or tuple(m["sleep"]) == (120, 600)


def test_questions_are_empty_offline(client, monkeypatch):
    h, uid = _user(client)

    class Dead:
        def generate(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr("core.generation.get_manager", lambda db=None: Dead())
    r = client.post("/api/goals", headers=h, json={"title": "Learn guitar", "deadline": "2099-01-01"})
    gid = r.json().get("id") or r.json().get("goal", {}).get("id")
    c = client.post(f"/api/goals/{gid}/clarify", headers=h).json()
    assert c["questions"] == []
    assert c["assumed"]