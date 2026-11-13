from datetime import date, timedelta
from core.database import get_connection
from tests.conftest import signup, auth as _auth


def _seed(client, headers=None):
    conn = get_connection()
    uid = conn.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO goals (user_id,title,deadline,display_title,status) VALUES (?,?,?,?,'active')",
                 (uid, "Learn spanish", "2099-01-01", "Learn Spanish"))
    gid = conn.execute("SELECT max(id) m FROM goals").fetchone()["m"]
    today = (date.today() - timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO actions (goal_id,user_id,date,title,status) VALUES (?,?,?,?,'pending')",
                 (gid, uid, today, "Flashcards review"))
    conn.commit()
    return gid


def test_done_needs_confirmation(client):
    auth_h = _auth(signup(client)['token'])
    gid = _seed(client)
    r = client.post("/api/chat/stream", json={"message": "i finished the flashcards"}, headers=auth_h)
    assert '"proposal"' in r.text
    conn = get_connection()
    assert conn.execute("SELECT status FROM actions WHERE goal_id=?", (gid,)).fetchone()["status"] == "pending"
    pid = conn.execute("SELECT max(id) m FROM pending_actions").fetchone()["m"]
    r = client.post("/api/chat/confirm", json={"proposal_id": pid, "accept": True}, headers=auth_h)
    assert r.json()["applied"]
    assert conn.execute("SELECT status FROM actions WHERE goal_id=?", (gid,)).fetchone()["status"] == "done"


def test_cancel_declined_changes_nothing(client):
    auth_h = _auth(signup(client)['token'])
    gid = _seed(client)
    r = client.post("/api/chat", json={"message": "cancel the spanish goal"}, headers=auth_h)
    assert r.json().get("proposal")
    pid = r.json()["proposal"]["id"]
    client.post("/api/chat/confirm", json={"proposal_id": pid, "accept": False}, headers=auth_h)
    conn = get_connection()
    assert conn.execute("SELECT status FROM goals WHERE id=?", (gid,)).fetchone()["status"] == "active"


def test_question_never_proposes(client):
    auth_h = _auth(signup(client)['token'])
    _seed(client)
    r = client.post("/api/chat/stream", json={"message": "have I finished the flashcards?"}, headers=auth_h)
    assert '"proposal"' not in r.text