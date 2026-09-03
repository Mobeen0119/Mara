from core import generation


def test_time_windows():
    assert generation.parse_time_window("11pm-7am") == (23*60, 7*60 + 24*60)
    assert generation.parse_time_window("3pm-5pm")[0] == 15*60
    assert generation.parse_time_window("gym 5-7pm") == (17*60, 19*60)


def test_days_remaining():
    assert generation.days_remaining("2026-09-10", today="2026-09-03") == 7


def test_completion_detection():
    assert generation.completion_detected("all finished")
    assert generation.completion_detected("I am done")
    assert generation.completion_detected("I finished it")
    assert not generation.completion_detected("why do I always leave things for the last second")
    assert not generation.completion_detected("what do I have to do today")
    assert not generation.completion_detected("what am I supposed to do now")
    assert not generation.completion_detected("should I do the homework?")
    assert not generation.completion_detected("I'm done with this is getting long enough to be a longer sentence that exceeds the word cap for auto close")


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