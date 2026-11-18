import random
from datetime import date, timedelta

from core import allocator as A
from core import planner as P

TODAY = "2026-11-16"  # a Monday
WAKE, BED = 7 * 60, 23 * 60
PREFS = P.Prefs(wake=WAKE, bed=BED, chronotype="balanced", commit_min=120)
SLEEP = [(0, WAKE), (BED, 1440)]
MEALS = [(13 * 60, 13 * 60 + 45), (19 * 60 + 30, 20 * 60 + 15)]
CLASS = [(9 * 60, 12 * 60)]
GYM = [(17 * 60, 18 * 60)]


def hard(_d):
    return SLEEP + MEALS + CLASS


def soft(_d):
    return GYM


def dl(days):
    return (date.fromisoformat(TODAY) + timedelta(days=days)).isoformat()


def run(specs, **kw):
    return A.allocate(specs, TODAY, PREFS, hard, soft, **kw)


def minutes(alloc, gid, day):
    return sum(e - s for s, e in alloc[gid].get(day, []))


def test_one_goal_gets_what_it_wants_and_respects_busy():
    g = A.Spec(1, dl(10), weight=3, ideal=120)
    alloc, rep = run([g])
    day = dl(2)
    assert minutes(alloc, 1, day) >= 90
    for s, e in alloc[1][day]:
        for bs, be in hard(day):
            assert not (s < be and bs < e), (s, e, bs, be)
        assert s >= WAKE + P.WAKE_LEAD and e <= BED - P.WIND_DOWN


def test_adding_goals_shrinks_the_others_but_never_overlaps():
    wife = A.Spec(1, dl(28), weight=2, kind="recurring", ideal=180, floor=30, max_block=180, per_week=7)
    alone, _ = run([wife])
    abs_ = A.Spec(
        2, dl(28), weight=2, kind="habit", ideal=60, floor=30, per_week=7, uses_soft=True, windows=GYM
    )
    exam = A.Spec(3, dl(6), weight=9, ideal=240, important=True)
    job = A.Spec(4, dl(15), weight=5, ideal=180)
    crowded, rep = run([wife, abs_, exam, job])
    day = dl(3)
    assert minutes(crowded, 1, day) < minutes(alone, 1, day)  # intimacy shrank...
    assert minutes(crowded, 1, day) >= 30  # ...but kept its floor
    assert (
        minutes(crowded, 2, day) >= 30 and minutes(crowded, 3, day) >= 30 and minutes(crowded, 4, day) >= 30
    )
    assert A.overlaps(crowded) == []
    # the exam outranks the job: it keeps more of its wish
    assert minutes(crowded, 3, day) / 240 >= minutes(crowded, 4, day) / 180


def test_removing_a_goal_gives_time_back():
    a = A.Spec(1, dl(20), weight=2, kind="recurring", ideal=180, floor=30, max_block=180, per_week=7)
    b = A.Spec(2, dl(20), weight=6, ideal=240)
    c = A.Spec(3, dl(20), weight=6, ideal=240)
    three, _ = run([a, b, c])
    two, _ = run([a, b])
    day = dl(4)
    assert minutes(two, 1, day) >= minutes(three, 1, day)


def test_nobody_is_missed_two_days_in_a_row():
    specs = [
        A.Spec(1, dl(40), weight=2, kind="recurring", ideal=120, floor=30, per_week=3),
        A.Spec(
            2, dl(40), weight=2, kind="habit", ideal=60, floor=30, per_week=3, uses_soft=True, windows=GYM
        ),
        A.Spec(3, dl(40), weight=6, ideal=240),
        A.Spec(4, dl(40), weight=4, ideal=180),
        A.Spec(5, dl(40), weight=1, ideal=90),
    ]
    alloc, rep = run(specs)
    for s in specs:
        served = [dl(i) in alloc[s.id] for i in range(40)]
        for i in range(1, 40):
            assert served[i - 1] or served[i], f"goal {s.id} missed days {i - 1} and {i}"
        assert rep[s.id]["missed_pairs"] == 0


def test_dozens_of_goals_never_overlap_and_all_get_touched():
    rng = random.Random(7)
    specs = []
    for i in range(1, 13):
        kind = rng.choice(["project", "project", "habit", "recurring"])
        specs.append(
            A.Spec(
                i,
                dl(rng.randint(5, 45)),
                weight=rng.uniform(1, 9),
                kind=kind,
                ideal=rng.choice([60, 90, 120, 180]),
                floor=30,
                per_week=rng.choice([2, 3, 4, 7]),
                max_block=rng.choice([60, 90, 180]),
            )
        )
    alloc, rep = run(specs)
    assert A.overlaps(alloc) == []
    for day in (dl(i) for i in range(10)):
        total = sum(minutes(alloc, s.id, day) for s in specs)
        assert total <= A.DAY_CAP
    for s in specs:
        assert alloc[s.id], f"goal {s.id} got nothing at all"


def test_only_important_goals_borrow_the_gym_window():
    chill = A.Spec(1, dl(10), weight=1, ideal=600, max_block=180, important=False)
    alloc, _ = run([chill])
    for blocks in alloc[1].values():
        for s, e in blocks:
            assert not (s < GYM[0][1] and GYM[0][0] < e), "a non-important goal sat in the gym window"
    big = A.Spec(2, dl(10), weight=9, ideal=600, max_block=180, important=True)
    alloc, rep = run([big])
    assert rep[2]["borrowed"] > 0
    assert alloc[2][dl(2)]


def test_gym_habit_uses_its_own_window():
    gym = A.Spec(
        1, dl(14), weight=2, kind="habit", ideal=60, floor=30, per_week=7, uses_soft=True, windows=GYM
    )
    alloc, _ = run([gym])
    day = dl(3)
    ((s, e),) = alloc[1][day]
    assert GYM[0][0] <= s and e <= GYM[0][1] + 15


def test_today_never_schedules_the_past():
    g = A.Spec(1, dl(5), weight=3, ideal=120)
    alloc, _ = A.allocate([g], TODAY, PREFS, hard, soft, now_min=15 * 60)
    for s, e in alloc[1].get(TODAY, []):
        assert s >= 15 * 60 + P.LEAD_TODAY


def test_late_sleeper_never_gets_times_past_midnight():
    prefs = P.Prefs(wake=11 * 60, bed=29 * 60, chronotype="evening", commit_min=240)
    g = A.Spec(1, dl(6), weight=3, ideal=300)
    alloc, _ = A.allocate([g], TODAY, prefs, lambda d: [(0, 11 * 60)], None)
    for bl in alloc[1].values():
        for s, e in bl:
            assert e <= 24 * 60


def test_event_work_lives_in_its_last_days():
    ev = A.Spec(1, dl(10), weight=4, kind="event", budget=300, active_days=3, floor=60)
    alloc, _ = run([ev])
    days = sorted(alloc[1])
    assert days and all(d >= dl(7) or d == dl(5) for d in days)


MOVIE = [(20 * 60, 23 * 60)]


def test_a_fire_shortens_the_movie_it_does_not_cancel_it():
    """An important goal may take at most half of a negotiable window; the rest stays for the person."""

    def soft_movie(_d):
        return MOVIE

    big = A.Spec(1, dl(6), weight=9, ideal=600, max_block=180, important=True)
    alloc, rep = A.allocate([big], TODAY, PREFS, hard, soft_movie)
    assert rep[1]["borrowed"] > 0
    for blocks in alloc[1].values():
        for s, e in blocks:
            inside = max(0, min(e, MOVIE[0][1]) - max(s, MOVIE[0][0]))
            assert inside <= 90, "took more than half of the movie window"


def test_after_three_heavy_days_the_next_is_a_lighter_day():
    specs = [A.Spec(i, dl(30), weight=3 + i, ideal=360, max_block=120) for i in range(1, 4)]
    alloc, _ = run(specs)
    totals = [sum(minutes(alloc, s.id, dl(i)) for s in specs) for i in range(8)]
    assert max(totals[:3]) >= A.HEAVY_DAY  # the first days really are heavy
    assert min(totals[3:6]) <= A.LIGHT_CAP  # then a lighter day is built in
    assert all(totals[i] > 0 for i in range(8))  # rest, not abandonment


def test_no_rest_day_on_the_eve_of_a_deadline():
    specs = [
        A.Spec(1, dl(5), weight=9, ideal=480, max_block=120, important=True),
        A.Spec(2, dl(30), weight=3, ideal=360, max_block=120),
        A.Spec(3, dl(30), weight=3, ideal=360, max_block=120),
    ]
    alloc, _ = run(specs)
    day_total = sum(minutes(alloc, sp.id, dl(4)) for sp in specs)
    assert day_total > A.LIGHT_CAP  # the eve is planned in full, not cut to a rest day


def test_outings_and_shopping_are_negotiable_but_class_is_not():
    from core import triage

    for t in ("outing sunday 3-7pm", "shopping saturday 2-5", "play football 5-7pm", "movie night 8-11pm"):
        assert triage.window_kind(t) == "soft", t
    for t in ("class 9-12", "office 9-5", "prayer 1pm", "doctor appointment 4pm"):
        assert triage.window_kind(t) == "hard", t