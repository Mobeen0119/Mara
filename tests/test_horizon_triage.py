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


def _goal(title, days, constraints=()):
    return {"id": 1, "user_id": 1, "title": title, "display_title": title, "deadline": _d(days),
            "constraints": json.dumps(list(constraints)), "details": "{}"}


def _mins(e):
    h0, m0 = map(int, e["start_time"].split(":")); h1, m1 = map(int, e["end_time"].split(":"))
    return (h1 * 60 + m1) - (h0 * 60 + m0)


def test_romantic_event_is_a_few_hours_in_the_last_days_not_twenty_days():
    generation.set_workout_windows([])
    plan = generation._build_fallback_plan(_goal("Plan a romantic date night for my wife", 20), today=_d(0), now_min=0)
    days = sorted({e["date"] for e in plan})
    assert days and days[-1] <= _d(20)
    assert len(days) <= 4                                    # lead slot + last 3 days
    assert sum(_mins(e) for e in plan) <= 300
    late = [e for e in plan if e["date"] >= _d(18)]
    assert late                                              # the real work is in the last days
    early = [e for e in plan if e["date"] < _d(15)]
    assert len(early) <= 1 and (not early or _mins(early[0]) <= 60)   # one lead-time slot only


def test_intimate_night_arc_only_plans_the_final_days():
    generation.set_workout_windows([])
    plan = generation._build_fallback_plan(_goal("Intimate night with my girlfriend", 20), today=_d(0), now_min=0)
    days = sorted({e["date"] for e in plan})
    assert days[0] >= _d(18) and days[-1] == _d(20)


def test_event_inside_three_days_still_plans_every_day():
    generation.set_workout_windows([])
    plan = generation._build_fallback_plan(_goal("Surprise anniversary dinner", 2), today=_d(0), now_min=0)
    assert {e["date"] for e in plan} >= {_d(1), _d(2)}


def test_habit_goal_uses_the_gym_window_and_movement_snacks_after_meals():
    generation.set_workout_windows([(17 * 60, 18 * 60)])
    plan = generation._build_fallback_plan(_goal("Get a six pack", 14, ["gym 5 to 6 pm"]), today=_d(0), now_min=0)
    gym = [e for e in plan if e["start_time"] == "17:00" and e["end_time"] == "18:00"]
    snacks = [e for e in plan if _mins(e) <= 10]
    assert len(gym) >= 10                                     # ~6 of every 7 days
    assert len(snacks) >= 14 * 2                              # after most meals, every day
    assert not [e for e in plan if 17 * 60 < int(e["start_time"][:2]) * 60 + int(e["start_time"][3:]) < 18 * 60]
    assert {e["date"] for e in gym}.__len__() == len(gym)     # one session a day
    generation.set_workout_windows([])


def test_habit_goal_is_not_flagged_for_sitting_in_its_own_gym_window(client):
    from core.routes import goal_routes
    u = signup(client)
    conn = get_connection()
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    cons = json.dumps(["gym 5 to 6 pm"])
    conn.execute("INSERT INTO goals (user_id,title,display_title,deadline,constraints) VALUES (?,?,?,?,?)",
                 (uid, "Get a six pack", "Get a six pack", _d(5), cons))
    gid = conn.execute("SELECT MAX(id) FROM goals").fetchone()[0]
    conn.execute("INSERT INTO actions (goal_id,user_id,date,title,start_time,end_time,duration_min,status,order_idx)"
                 " VALUES (?,?,?,?,?,?,60,'pending',0)", (gid, uid, _d(1), "Push day: bench 4x8, rows 4x8", "17:00", "18:00"))
    conn.commit()
    habit = {"id": gid, "constraints": cons, "title": "Get a six pack", "display_title": "Get a six pack"}
    assert goal_routes._action_violates_goal_windows(habit) is False
    other = dict(habit, title="Ship the launch", display_title="Ship the launch")
    assert goal_routes._action_violates_goal_windows(other) is True       # normal goals must still avoid the gym