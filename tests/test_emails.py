from datetime import date
from core import emails
from core.database import get_connection
from tests.conftest import signup


def _setup(client, monkeypatch):
    signup(client)
    conn = get_connection()
    conn.execute("UPDATE users SET verified=1")
    conn.execute("INSERT INTO goals (user_id,title,deadline,display_title) VALUES (1,'Learn <b>guitar</b>','2099-01-01','Learn <b>guitar</b>')")
    today = date.today().isoformat()
    conn.execute("INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time) VALUES (1,1,?,?,?,?)",
                 (today, "Chords: G C D", "18:00", "18:45"))
    conn.commit()
    sent = []
    monkeypatch.setattr("core.mailer.smtp_configured", lambda: True)
    monkeypatch.setattr("core.mailer.send_email_detailed", lambda to, s, t="", h=None: sent.append((to, s, t, h)) or (True, ""))
    return conn, today, sent


def test_first_day_mail_sent_once_and_escaped(client, monkeypatch):
    conn, today, sent = _setup(client, monkeypatch)
    user = dict(conn.execute("SELECT * FROM users").fetchone())
    goal = dict(conn.execute("SELECT * FROM goals").fetchone())
    assert emails.send_first_day(conn, user, goal)
    assert not emails.send_first_day(conn, user, goal)
    to, subject, text, html = sent[0]
    assert "Chords: G C D" in text and "- [ ]" in text
    assert "<b>guitar</b>" not in html and "&lt;b&gt;guitar" in html


def test_unverified_user_gets_nothing(client, monkeypatch):
    conn, today, sent = _setup(client, monkeypatch)
    conn.execute("UPDATE users SET verified=0")
    conn.commit()
    user = dict(conn.execute("SELECT * FROM users").fetchone())
    assert not emails.send_daily(conn, user, [dict(conn.execute("SELECT * FROM goals").fetchone())], today)
    assert not sent


def test_daily_digest_uses_styled_mail(client, monkeypatch):
    conn, today, sent = _setup(client, monkeypatch)
    user = dict(conn.execute("SELECT * FROM users").fetchone())
    assert emails.send_daily(conn, user, [dict(conn.execute("SELECT * FROM goals").fetchone())], today)
    assert "18:00-18:45" in sent[0][2] and "<table" in sent[0][3]