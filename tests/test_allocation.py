import json
import re
import threading
from datetime import date, timedelta

import pytest

from core import generation, planner
from core.database import get_connection
from core.routes import goal_routes
from tests.conftest import auth, signup

TODAY = date(2026, 11, 16)


def _day(n):
    return (TODAY + timedelta(days=n)).isoformat()


def _goal(i, imp, deadline_n, rate, session=45, free=False):
    return {"id": i, "importance": imp, "deadline": None if deadline_n is None else _day(deadline_n),
            "rate": rate, "session": session, "free": free}


def _alloc(goals, days=14, cap=360):
    return planner.allocate(goals, TODAY.isoformat(), days, lambda d: cap)


def test_every_goal_gets_time_and_the_day_never_overflows():
    goals = [_goal(i, 1 + i % 3, 10 + i, 120) for i in range(1, 11)]
    a = _alloc(goals)
    for n in range(14):
        assert sum(a[g["id"]].get(_day(n), 0) for g in goals) <= 360
    assert all(sum(a[g["id"]].values()) > 0 for g in goals)


def test_several_goals_share_one_day_and_none_is_starved():
    goals = [_goal(1, 3, 20, 180), _goal(2, 3, 20, 120), _goal(3, 2, 20, 90), _goal(4, 2, 20, 90), _goal(5, 1, 20, 60)]
    a = _alloc(goals, 10)
    busiest = max(range(10), key=lambda n: sum(1 for g in goals if a[g["id"]].get(_day(n), 0)))
    assert sum(1 for g in goals if a[g["id"]].get(_day(busiest), 0)) >= 4
    assert all(sum(a[g["id"]].values()) > 0 for g in goals)


def test_importance_beats_a_nearer_deadline_inside_a_few_days():
    exam = _goal(1, 3, 3, 240)
    date_night = _goal(2, 1, 2, 240)
    a = _alloc([exam, date_night], 4)
    assert sum(a[1].values()) > 2 * sum(a[2].values())
    for n in range(3):
        assert a[1].get(_day(n), 0) > a[2].get(_day(n), 0)


def test_equal_importance_follows_urgency():
    soon = _goal(1, 2, 2, 240)
    later = _goal(2, 2, 30, 240)
    a = _alloc([soon, later], 3)
    assert sum(a[1].values()) > sum(a[2].values())


def test_small_shares_bank_into_a_real_sitting_later():
    goals = [_goal(1, 3, 40, 300), _goal(2, 3, 40, 300), _goal(3, 1, 40, 120)]
    a = _alloc(goals, 14, cap=330)
    mine = a[3]
    assert mine and all(m >= planner.MIN_SESSION for m in mine.values())


def test_sessions_per_week_emerge_without_fixed_weekdays():
    habit = _goal(1, 1, None, 60 * 4 / 7.0, session=60, free=True)
    for start in range(7):
        a = planner.allocate([habit], _day(start), 14, lambda d: 360)
        days = sorted(a[1])
        assert 7 <= len(days) <= 9
        gaps = {(date.fromisoformat(b) - date.fromisoformat(x)).days for x, b in zip(days, days[1:])}
        assert gaps <= {1, 2, 3}


def test_sporadic_goal_keeps_its_rhythm_under_load():
    wife = _goal(1, 2, None, 60 * 3 / 7.0, session=60)
    busy = [_goal(i, 3, 30, 240) for i in range(2, 5)]
    a = _alloc([wife] + busy, 14)
    assert 4 <= len(a[1]) <= 7


def test_open_ended_goals_do_not_expire():
    a = _alloc([_goal(1, 1, None, 90)], 30)
    assert len(a[1]) == 30


def test_event_waits_for_its_active_window():
    g = {**_goal(1, 3, 9, 120), "start": _day(7)}
    a = _alloc([g], 10)
    assert min(a[1]) == _day(7)


def test_nothing_is_keyed_to_weekday_or_task_words():
    import inspect
    src = inspect.getsource(planner.allocate) + inspect.getsource(generation.goal_demand)
    assert "weekday" not in src


class _Mgr:
    def generate(self, system, user, **kw):
        ok = type("R", (), {"ok": True, "provider": "fake", "model": "fake", "error": ""})
        r = ok()
        if "rate how much a goal matters" in system:
            m = re.search(r"Goal: (.*)", user).group(1).lower()
            r.text = json.dumps({"importance": 3 if ("exam" in m or "startup" in m) else 1})
        elif "size real-life goals" in system:
            m = re.search(r"Goal: (.*)", user).group(1).lower()
            if "wife" in m:
                r.text = json.dumps({"kind": "habit", "total_hours": 10, "active_days": 10, "physical": False, "per_week": 3, "best_time": "evening"})
            elif "abs" in m:
                r.text = json.dumps({"kind": "habit", "total_hours": 30, "active_days": 20, "physical": True, "per_week": 4, "best_time": "morning"})
            else:
                r.text = json.dumps({"kind": "project", "total_hours": 40, "active_days": 10, "physical": False, "per_week": 3, "best_time": ""})
        else:
            slots = re.findall(r'\{"date": "([\d-]+)", "start_time": "([\d:]+)", "end_time": "([\d:]+)"\}', user)
            r.text = json.dumps([{"date": d, "start_time": s, "end_time": e, "title": f"Work through chapter {i} exercises on paper"}
                                 for i, (d, s, e) in enumerate(slots)])
        return r


@pytest.fixture()
def board(client, monkeypatch):
    monkeypatch.setattr(generation, "get_manager", lambda db=None: _Mgr())
    queued = []
    monkeypatch.setattr(goal_routes, "submit_plan", lambda user, gid, **k: queued.append(gid) or True)
    u = signup(client)
    h = auth(u["token"])
    today = date.today()
    specs = [("Pass the final exam", 4, {"time_per_day": "4 hours"}),
             ("Launch my startup landing page", 9, {"time_per_day": "3 hours"}),
             ("Finish the report for work", 12, {"time_per_day": "2 hours"}),
             ("Learn Spanish basics", 20, {"time_per_day": "1 hour"}),
             ("Quality time with my wife", 18, {}),
             ("Get 6 pack abs", 25, {})]
    ids = []
    for title, n, det in specs:
        gid = client.post("/api/goals", headers=h, json={"title": title, "deadline": (today + timedelta(days=n)).isoformat(),
                                                         "constraints": ["classes 9am-1pm"]}).json()["id"]
        if det:
            client.post(f"/api/goals/{gid}/details", headers=h, json=det)
        ids.append(gid)
    conn = get_connection()
    user = dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone())

    def run():
        order = sorted(ids, key=lambda g: goal_routes._rank(dict(conn.execute("SELECT id, deadline, open_ended, title, details FROM goals WHERE id=?", (g,)).fetchone())))
        for _ in range(3):
            for gid in order:
                goal_routes._regenerate_plan_bg(user, gid)
            queued.clear()
    run()
    return ids, specs


def _rows():
    return [dict(r) for r in get_connection().execute("SELECT * FROM actions WHERE status='pending' ORDER BY date, start_time").fetchall()]


def _m(t):
    return int(t[:2]) * 60 + int(t[3:])


def test_every_goal_ends_up_with_a_plan_not_no_free_time(board):
    ids, _ = board
    conn = get_connection()
    for gid in ids:
        row = conn.execute("SELECT plan_status, plan_summary FROM goals WHERE id=?", (gid,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM actions WHERE goal_id=?", (gid,)).fetchone()[0]
        assert n > 0, (gid, dict(row))
        assert row["plan_status"] != "blocked", dict(row)


def test_no_two_goals_overlap_and_days_stay_human(board):
    rows = _rows()
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append((_m(r["start_time"]), _m(r["end_time"]), r["goal_id"]))
    for d, items in by_day.items():
        items.sort()
        for a, b in zip(items, items[1:]):
            assert a[1] <= b[0], (d, a, b)
        assert sum(e - s for s, e, g in items) <= planner.DAY_FOCUS_CAP + 60


def test_goals_interleave_on_the_same_day(board):
    rows = _rows()
    per_day = {}
    for r in rows:
        per_day.setdefault(r["date"], set()).add(r["goal_id"])
    assert max(len(v) for v in per_day.values()) >= 3


def test_exam_week_goes_to_the_exam_not_the_wife(board):
    ids, _ = board
    exam, wife = ids[0], ids[4]
    week = {_day(n).replace("2026", str(date.today().year)) for n in range(0)}
    rows = _rows()
    cutoff = (date.today() + timedelta(days=4)).isoformat()
    mins = lambda gid: sum(_m(r["end_time"]) - _m(r["start_time"]) for r in rows if r["goal_id"] == gid and r["date"] <= cutoff)
    assert mins(exam) > 2 * mins(wife)


def test_nothing_lands_inside_the_class_window(board):
    for r in _rows():
        if r["goal_id"] in board[0][:4]:
            assert not (_m(r["start_time"]) < 13 * 60 and 9 * 60 < _m(r["end_time"])), r


def test_gym_gets_its_own_window_at_a_runtime_rhythm(board):
    ids, _ = board
    gym = ids[5]
    rows = _rows()
    sessions = sorted(r["date"] for r in rows if r["goal_id"] == gym and _m(r["end_time"]) - _m(r["start_time"]) >= 45)
    assert 8 <= len(sessions) <= 16
    gaps = {(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(sessions, sessions[1:])}
    assert gaps <= {1, 2, 3}
    window = {(r["start_time"], r["end_time"]) for r in rows if r["goal_id"] == gym and _m(r["end_time"]) - _m(r["start_time"]) >= 45}
    assert len(window) == 1
    s, e = _m(next(iter(window))[0]), _m(next(iter(window))[1])
    assert not any(r["goal_id"] != gym and _m(r["start_time"]) < e and s < _m(r["end_time"]) for r in rows)


def test_cheap_near_deadline_goals_cannot_swallow_the_important_one():
    goals = [_goal(1, 3, 3, 240)] + [_goal(i, 1, 1, 120) for i in range(2, 6)]
    a = _alloc(goals, 4, cap=90)
    assert sum(a[1].values()) >= 90
    assert all(sum(a[g["id"]].values()) > 0 for g in goals)


def test_every_goal_gets_at_least_one_sitting_even_when_overbooked():
    goals = [_goal(i, 1 + i % 3, 2, 120) for i in range(1, 12)]
    a = _alloc(goals, 3, cap=90)
    assert all(sum(a[g["id"]].values()) > 0 for g in goals)


def test_overloaded_board_blocks_with_a_reason_that_names_the_winners(client, monkeypatch):
    monkeypatch.setattr(generation, "get_manager", lambda db=None: _Mgr())
    monkeypatch.setattr(goal_routes, "submit_plan", lambda user, gid, **k: True)
    u = signup(client)
    h = auth(u["token"])
    today = date.today()
    specs = [("Pass the final exam", 3), ("Launch my startup landing page", 1), ("Finish the report for work", 2),
             ("Fix my bike", 1), ("Write a poem", 2), ("Read two books", 30)]
    ids = []
    for title, n in specs:
        ids.append(client.post("/api/goals", headers=h, json={"title": title, "deadline": (today + timedelta(days=n)).isoformat(),
                                                              "constraints": ["classes 8am-6pm", "gym 6-7pm"]}).json()["id"])
    conn = get_connection()
    user = dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone())
    for _ in range(3):
        for gid in ids:
            goal_routes._regenerate_plan_bg(user, gid)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT id, plan_status, plan_summary FROM goals").fetchall()}
    assert rows[ids[0]]["plan_status"] != "blocked"
    assert rows[ids[5]]["plan_status"] != "blocked"
    for r in rows.values():
        if r["plan_status"] == "blocked":
            assert "what matters more" in r["plan_summary"] or "fixed windows" in r["plan_summary"]