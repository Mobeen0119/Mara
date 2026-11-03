"""The scheduling policy: does the day look like a human's?"""
from datetime import date

from core import planner as P

BUSY = [(23 * 60, 24 * 60), (0, 7 * 60), (7 * 60, 8 * 60), (13 * 60, 13 * 60 + 45),
        (17 * 60, 19 * 60), (19 * 60 + 30, 20 * 60 + 15)]


def horizon(today="2026-10-26", deadline="2026-11-01", commit=180, now_min=None, busy=BUSY, **kw):
    return P.plan_horizon(today, deadline, P.Prefs(commit_min=commit, **kw), lambda d: busy, now_min=now_min)


def test_blocks_are_focus_sized_with_real_breaks():
    for _, slots in horizon():
        for s, e in slots:
            assert 30 <= e - s <= 90
        for (s1, e1), (s2, e2) in zip(slots, slots[1:]):
            assert s2 - e1 >= P.SHORT_BREAK, "back-to-back blocks"


def test_old_behaviour_is_gone_no_wall_of_45_minute_chunks_from_8am():
    for _, slots in horizon():
        assert not slots or slots[0][0] >= 9 * 60, "work started before the person had time to wake up/eat"
        total = sum(e - s for s, e in slots)
        assert total <= 240


def test_never_inside_busy_sleep_meals_or_wind_down():
    for _, slots in horizon():
        for s, e in slots:
            for bs, be in BUSY:
                assert not (s < be and bs < e), f"{P.fmt(s)}-{P.fmt(e)} overlaps {P.fmt(bs)}-{P.fmt(be)}"
            assert e <= 23 * 60 - P.WIND_DOWN


def test_buffer_around_meals_and_gym():
    for _, slots in horizon():
        for s, e in slots:
            for bs, be in BUSY[2:]:
                assert not (s < be + P.BUFFER and bs - P.BUFFER < e)


def test_load_has_a_shape():
    days = dict(horizon(deadline="2026-12-05"))
    first, last, eve = days["2026-10-26"], days["2026-12-05"], days["2026-12-04"]
    assert len(first) == 1                      # far from deadline: a gentle drip
    assert len(last) <= 2 and all(e <= 14 * 60 for _, e in last)   # deadline day: finish in the morning
    assert len(eve) <= 3 and all(e <= 19 * 60 for _, e in eve)     # night before: no cramming
    assert max(len(s) for s in days.values()) <= P.MAX_BLOCKS_PER_DAY


def test_long_plans_get_a_lighter_day_about_weekly():
    days = horizon(deadline="2026-12-05", commit=240)
    light = [d for i, (d, s) in enumerate(days) if (i + 1) % 7 == 0 and len(days) - 1 - i > 3]
    for d in light:
        assert len(dict(days)[d]) <= 1


def test_today_only_gets_future_hours():
    slots = dict(horizon(today="2026-10-25", deadline="2026-10-27", now_min=18 * 60 + 40))["2026-10-25"]
    assert all(s >= 18 * 60 + 40 + P.LEAD_TODAY - 14 for s, _ in slots)
    late = dict(horizon(today="2026-10-25", deadline="2026-10-27", now_min=22 * 60 + 30))["2026-10-25"]
    assert late == []                           # nothing planned for hours that are gone


def test_work_lands_in_the_persons_best_hours():
    m = dict(horizon(chronotype="morning", commit=120))["2026-10-27"]
    e = dict(horizon(chronotype="evening", commit=120))["2026-10-27"]
    assert m[0][0] < 10 * 60 and e[0][0] >= 11 * 60 and e[0][0] > m[0][0]


def test_small_commitment_is_one_short_block_not_chopped_up():
    for _, slots in horizon(commit=30, deadline="2026-10-30"):
        assert len(slots) <= 1 and (not slots or slots[0][1] - slots[0][0] == 30)


def test_block_plan_arithmetic():
    assert P.block_plan(45) == (1, 45)
    assert P.block_plan(120) == (2, 60)
    assert P.block_plan(180) == (3, 60)
    assert P.block_plan(480)[0] == P.MAX_BLOCKS_PER_DAY and P.block_plan(480)[1] <= 90


def test_night_owl_and_early_bird_inference():
    assert P.infer_chronotype("", wake=10 * 60, bed=25 * 60) == "evening"
    assert P.infer_chronotype("mornings for sure") == "morning"
    assert P.infer_chronotype("", wake=7 * 60, bed=23 * 60) == "balanced"
    assert P.normalize_sleep(60, 9 * 60) == 25 * 60          # 01:00 bedtime is the END of the day


def test_fully_booked_day_yields_nothing_instead_of_double_booking():
    full = [(0, 24 * 60)]
    assert all(s == [] for _, s in horizon(busy=full))


def test_generation_uses_the_engine_and_titles_get_real_durations():
    import json
    from core import generation
    goal = {"deadline": "2026-11-01", "display_title": "Pass the exam", "title": "Pass the exam",
            "constraints": json.dumps(["gym 5-7pm"]),
            "details": json.dumps({"time_per_day": "3 hours", "sleep_schedule": "23:30 - 07:30",
                                   "meal_times": "08:00, 13:00, 20:00", "movement_time": "17:00",
                                   "peak_energy": "morning"})}
    rows = generation._build_fallback_plan(goal, today="2026-10-26")
    assert rows
    by = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r)
    assert set(by) == {f"2026-10-{d}" for d in range(26, 32)} | {"2026-11-01"}
    for day in by.values():
        day.sort(key=lambda r: r["start_time"])
        assert day[0]["start_time"] >= "08:15"
        for a, b in zip(day, day[1:]):
            assert b["start_time"] > a["end_time"], "blocks must be separated"
        assert all(r["duration_min"] >= 30 for r in day)


def test_other_goals_blocks_are_avoided_with_a_break_and_count_toward_the_daily_cap():
    prefs = P.Prefs(commit_min=240)
    others = [(9 * 60 + 30, 10 * 60 + 30), (10 * 60 + 45, 11 * 60 + 45), (15 * 60, 16 * 60)]
    mine = P.plan_day(prefs, BUSY, max_blocks=4, length=60, others=others)
    for s, e in mine:
        for os_, oe in others:
            assert e + P.SHORT_BREAK <= os_ or s >= oe + P.SHORT_BREAK, "no break between goals"
    assert sum(e - s for s, e in mine) + sum(e - s for s, e in others) <= P.DAY_FOCUS_CAP
    heavy = [(9 * 60 + 30, 12 * 60 + 30), (15 * 60, 18 * 60)]            # other goals already own 6h
    assert P.plan_day(prefs, BUSY, max_blocks=4, length=60, others=heavy) == []


def test_two_goals_drawn_at_once_do_not_collide_or_eat_lunch(client, monkeypatch):
    import json
    import threading
    from datetime import date, timedelta
    from core import generation
    from core.database import get_connection
    from core.routes import goal_routes
    from tests.conftest import auth, signup

    class FakeRes:
        ok, provider, model, error = True, "fake", "fake", ""
        def __init__(self, text): self.text = text

    class FakeMgr:
        def generate(self, system, user, **kw):
            import re
            slots = re.findall(r'\{"date": "([\d-]+)", "start_time": "([\d:]+)", "end_time": "([\d:]+)"\}', user)
            return FakeRes(json.dumps([{"date": d, "start_time": s, "end_time": e, "title": f"Solve problem set {i} on paper"}
                                       for i, (d, s, e) in enumerate(slots)]))
    monkeypatch.setattr(generation, "get_manager", lambda db=None: FakeMgr())
    monkeypatch.setattr(goal_routes, "submit_plan", lambda *a, **k: True)
    u = signup(client)
    h = auth(u["token"])
    ids = []
    for title, det in (("Pass the exam", {"time_per_day": "3 hours", "meal_times": "08:00, 13:00, 20:00", "peak_energy": "morning"}),
                       ("Ship the site", {"time_per_day": "2 hours", "peak_energy": "morning"})):
        gid = client.post("/api/goals", headers=h, json={"title": title, "deadline": (date.today() + timedelta(days=6)).isoformat(),
                                                         "constraints": ["gym 5-7pm"]}).json()["id"]
        client.post(f"/api/goals/{gid}/details", headers=h, json=det)
        ids.append(gid)
    conn = get_connection()
    user = dict(conn.execute("SELECT * FROM users WHERE id=1").fetchone())
    ts = [threading.Thread(target=goal_routes._regenerate_plan_bg, args=(user, gid)) for gid in ids]
    [t.start() for t in ts]; [t.join(30) for t in ts]
    rows = [dict(r) for r in get_connection().execute("SELECT * FROM actions ORDER BY date, start_time").fetchall()]
    assert rows
    to_min = lambda t: int(t[:2]) * 60 + int(t[3:])
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append((to_min(r["start_time"]), to_min(r["end_time"])))
    for day, blocks in by_day.items():
        blocks.sort()
        assert sum(e - s for s, e in blocks) <= P.DAY_FOCUS_CAP, day
        for (s1, e1), (s2, e2) in zip(blocks, blocks[1:]):
            assert s2 >= e1 + P.SHORT_BREAK, f"{day}: blocks {P.fmt(s1)}-{P.fmt(e1)} and {P.fmt(s2)}-{P.fmt(e2)} touch"
        for s, e in blocks:
            assert not (s < 13 * 60 + 45 and 13 * 60 < e), f"{day}: block {P.fmt(s)}-{P.fmt(e)} eats lunch"
            assert not (s < 19 * 60 and 17 * 60 < e), f"{day}: block {P.fmt(s)}-{P.fmt(e)} in the gym window"


def test_when_best_hours_are_taken_other_free_hours_are_used_not_dropped():
    uni = BUSY + [(9 * 60, 13 * 60)]
    slots = P.plan_day(P.Prefs(commit_min=180, chronotype="morning"), uni, max_blocks=3, length=60)
    assert sum(e - s for s, e in slots) >= 120 and all(e <= 23 * 60 - P.WIND_DOWN for _, e in slots)
    for s, e in slots:
        assert not any(s < be and bs < e for bs, be in uni)
    for (s1, e1), (s2, e2) in zip(slots, slots[1:]):
        assert s2 - e1 >= P.SHORT_BREAK


def test_no_scrap_blocks():
    for _, slots in horizon(commit=180, deadline="2026-11-10"):
        for s, e in slots:
            assert e - s >= P.MIN_BLOCK
