"""Horizon-aware planning, history-safe redraws, global constraints, triage."""
import json
from datetime import date, timedelta

import pytest

from core import generation, triage
from core.database import get_connection
from tests.conftest import auth, signup


def _d(n):
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.mark.parametrize("left,scope", [(1, "daily"), (30, "daily"), (56, "daily"), (57, "weekly"),
                                        (75, "weekly"), (76, "monthly"), (200, "monthly")])
def test_scope_thresholds(left, scope):
    assert generation.scope_for(left) == scope
    assert (generation.detail_days_for(left) is None) == (scope == "daily")


def test_monthly_and_weekly_outline_periods_use_real_free_hours():
    today, dl = _d(0), _d(119)
    mins = {_d(i): 180 for i in range(120)}                      # 3 free hours a day
    months = generation.build_outline_periods(today, dl, "monthly", mins)
    assert len(months) == 4 and all(p["kind"] == "month" for p in months)
    assert months[0]["hours"] == 90.0                            # 30 days x 3h
    assert [p["phase"] for p in months][0] == "Foundations" and months[-1]["phase"] == "Final push"
    assert months[0]["end"] < months[1]["start"] and months[-1]["end"] == dl
    weeks = generation.build_outline_periods(today, _d(62), "weekly", {_d(i): 60 for i in range(63)})
    assert len(weeks) == 9 and weeks[0]["hours"] == 7.0


def test_far_goal_draws_only_the_near_window_but_keeps_deadline_shape():
    goal = {"title": "Write thesis", "display_title": "Write thesis", "deadline": _d(100),
            "constraints": "[]", "details": "{}"}
    ol = {}
    clipped = generation._build_fallback_plan(goal, today=_d(0), detail_days=14, outline_out=ol)
    assert clipped and max(e["date"] for e in clipped) <= _d(13)
    assert len(ol["slot_minutes"]) >= 90                         # full span still measured
    full = generation._build_fallback_plan(goal, today=_d(0))
    assert max(e["date"] for e in full) > _d(60)


def test_constraint_typed_in_one_goal_blocks_every_goal(client, monkeypatch):
    monkeypatch.setattr("core.routes.goal_routes.submit_plan", lambda *a, **k: True)
    u = signup(client)
    h = auth(u["token"])
    client.post("/api/goals", headers=h, json={"title": "A", "deadline": _d(9)})
    client.post("/api/goals", headers=h, json={"title": "B", "deadline": _d(9), "constraints": ["gym 5-7pm"]})
    from core.routes.goal_routes import active_goal_blocked_windows
    conn = get_connection()
    user = dict(conn.execute("SELECT * FROM users").fetchone())
    assert (17 * 60, 19 * 60) in active_goal_blocked_windows(conn, user)


def test_redraw_keeps_done_history_and_other_windows(client):
    from core.routes import goal_routes
    u = signup(client)
    h = auth(u["token"])
    gid = client.post("/api/goals", headers=h, json={"title": "Pass exam", "deadline": _d(9)}).json()["id"]
    conn = get_connection()
    conn.execute("DELETE FROM actions WHERE goal_id=?", (gid,))
    uid = conn.execute("SELECT user_id FROM goals WHERE id=?", (gid,)).fetchone()[0]
    def put(day, st, status):
        conn.execute("INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx)"
                     " VALUES (?,?,?,?,?,?,60,?,0)", (gid, uid, day, "old task here now", st, "10:00", status))
    put(_d(-2), "09:00", "done"); put(_d(0), "09:00", "done"); put(_d(30), "09:00", "pending"); put(_d(2), "09:00", "pending")
    conn.commit()
    goal = dict(conn.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
    new = [{"date": _d(1), "start_time": "14:00", "end_time": "15:00", "title": "Solve ten subnetting problems, redo misses"},
           {"date": _d(2), "start_time": "14:00", "end_time": "15:00", "title": "Write both stacks from memory, fix gaps"}]
    assert goal_routes._write_plan(conn, goal, new)
    rows = {(r["date"], r["status"]) for r in conn.execute("SELECT date,status FROM actions WHERE goal_id=?", (gid,))}
    assert (_d(-2), "done") in rows and (_d(0), "done") in rows       # history survives
    assert (_d(30), "pending") in rows                                 # beyond the window: untouched
    titles = [r["title"] for r in conn.execute("SELECT title FROM actions WHERE goal_id=? AND date=?", (gid, _d(2)))]
    assert titles == ["Write both stacks from memory, fix gaps"]       # inside the window: replaced


def _tri_user(client):
    u = signup(client)
    return u, auth(u["token"])


def test_triage_calls_exam_over_gym_with_numbers(client, monkeypatch):
    monkeypatch.setattr("core.routes.goal_routes.submit_plan", lambda *a, **k: True)
    u, h = _tri_user(client)
    exam = client.post("/api/goals", headers=h, json={"title": "Pass the networking exam", "deadline": _d(4),
                       "constraints": ["gym 5-7pm", "uni 9 to 1"]}).json()["id"]
    client.post("/api/goals", headers=h, json={"title": "Read more books", "deadline": _d(60)})
    conn = get_connection()
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    for i in range(5):
        conn.execute("INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx)"
                     " VALUES (?,?,?,?,?,?,120,'pending',?)", (exam, uid, _d(i), "Solve ten problems and redo", "14:00", "16:00", i))
    conn.commit()
    t = client.get("/api/triage", headers=h).json()
    assert t["ranked"][0]["title"] == "Pass the networking exam"
    assert "wins" in t["call"] and "gym 5-7pm" in t["call"]
    assert all("uni" not in c["window"] for c in t["cut"])             # class is hard, never cut
    assert "Read more books" in t["call"]                              # can wait


def test_triage_stays_quiet_when_nothing_is_on_fire(client, monkeypatch):
    monkeypatch.setattr("core.routes.goal_routes.submit_plan", lambda *a, **k: True)
    u, h = _tri_user(client)
    client.post("/api/goals", headers=h, json={"title": "Learn guitar", "deadline": _d(50), "constraints": ["gym 5-7pm"]})
    t = client.get("/api/triage", headers=h).json()
    assert t["cut"] == [] and "wins" not in t["call"]


def test_window_kinds():
    assert triage.window_kind("gym 5-7pm") == "soft"
    assert triage.window_kind("uni 9 to 1") == "hard"
    assert triage.window_kind("dinner 8pm") == "hard"
    assert triage.window_kind("something odd 3-4pm") == "hard"        # unknown = protected


def test_chat_board_snapshot_carries_the_priority_call(client, monkeypatch):
    monkeypatch.setattr("core.routes.goal_routes.submit_plan", lambda *a, **k: True)
    u, h = _tri_user(client)
    client.post("/api/goals", headers=h, json={"title": "Pass the final exam", "deadline": _d(2), "constraints": ["gym 5-7pm"]})
    from core.routes.chat_routes import _active_board_snapshot
    conn = get_connection()
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    snap = _active_board_snapshot(conn, uid, date.today().isoformat())
    assert "PRIORITY CALL" in snap and "gym 5-7pm" in snap
