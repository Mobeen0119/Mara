import json
import logging
import re
from datetime import date, timedelta

from core import allocator, generation, insights, planner, routine, triage
from core.clock import minutes_now, today_for

logger = logging.getLogger(__name__)

DEFAULT_GYM_START = 18 * 60


def _jl(raw, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _split(w):
    """One (start, end) window -> pieces inside a single day (overnight spans split at midnight)."""
    s, e = int(w[0]) % 1440, int(w[1])
    if e > 1440:
        e %= 1440
    if e <= s:
        return [(s, 1440)] + ([(0, e)] if e > 0 else [])
    return [(s, e)]


def _prefs(rt):
    wake, bed = 7 * 60, 23 * 60
    sl = rt.get("sleep")
    if sl:
        b0, w0 = sl[0], sl[1]
        if (w0 - b0) % 1440 > 14 * 60:  # typed backwards ("7am to 11pm")
            b0, w0 = w0, b0
        nb = planner.normalize_sleep(b0, w0)
        if nb is not None:
            bed, wake = nb, w0
    return planner.Prefs(
        wake=wake, bed=bed, commit_min=120, chronotype=planner.infer_chronotype(rt.get("peak"), wake, bed)
    )


def _gym_window(rt):
    if rt.get("workout"):
        return tuple(rt["workout"])
    wake = (rt.get("sleep") or (23 * 60, 7 * 60))[1] % 1440
    start = wake + 60 if rt.get("peak") == "morning" else DEFAULT_GYM_START
    return (start, start + 60)


_PERISHABLE = re.compile(r"\b(flowers?|bouquet|cake|balloons?|groceries|ingredients|fresh)\b", re.I)

_PARTNER = (
    "wife",
    "husband",
    "partner",
    "spouse",
    "girlfriend",
    "boyfriend",
    "gf",
    "bf",
    "intimacy",
    "intimate",
)
_ONE_OFF = (
    "anniversary",
    "proposal",
    "propose",
    "surprise",
    "gift",
    "valentine",
    "birthday",
    "wedding",
    "date night",
    "memorable",
    "honeymoon",
    "trip",
    "dinner reservation",
)


def _ongoing_relationship(title):
    """'intimacy with my wife' is a standing commitment, not a 3-day event. A proposal, an
    anniversary or a surprise is an event. Used only when no model brief exists to say so."""
    t = (title or "").lower()
    return any(re.search(rf"\b{w}\b", t) for w in _PARTNER) and not any(w in t for w in _ONE_OFF)


def build_specs(conn, user, rt, today):
    out = []
    saved = getattr(generation._state_brief, "b", None)
    try:
        for r in conn.execute(
            "SELECT * FROM goals WHERE user_id=? AND status='active' ORDER BY id", (user["id"],)
        ).fetchall():
            g = dict(r)
            d = _jl(g.get("details"), {})
            cached = d.get("brief") if d.get("brief_for") == g.get("title") else None
            generation.set_brief(cached)
            left = max(generation.days_remaining(g["deadline"], today), 0)
            prof = generation.effort_profile(g, left)
            imp = triage.importance_of(g)
            kind = prof["kind"]
            if kind == "event" and g.get("open_ended"):
                kind, prof = "recurring", {"kind": "recurring", "per_week": 3}
            elif (
                kind == "event"
                and prof.get("event_kind") == "romance"
                and _ongoing_relationship(g.get("title"))
            ):
                kind, prof = "recurring", {"kind": "recurring", "per_week": 7, "best_time": "evening"}
            commit = generation.daily_commit_min(d)
            has_time = bool((d.get("time_per_day") or "").strip())
            base = dict(
                id=g["id"],
                deadline=g["deadline"],
                title=g.get("display_title") or g.get("title") or "",
                weight=imp * 2.0 + min(6.0 / max(left, 1), 3.0),
                important=imp >= 3,
            )
            if kind == "habit":
                spec = allocator.Spec(
                    kind="habit",
                    ideal=generation.SESSION_MAX_MIN,
                    floor=30,
                    min_block=15,
                    max_block=75,
                    touch=15,
                    uses_soft=True,
                    windows=[_gym_window(rt)],
                    per_week=prof.get("per_week") or generation.habit_sessions_per_week(g),
                    **base,
                )
            elif kind == "recurring":
                spec = allocator.Spec(
                    kind="recurring",
                    ideal=min(commit, 180) if has_time else 90,
                    floor=30,
                    max_block=180,
                    per_week=prof.get("per_week") or 3,
                    best_time=prof.get("best_time") or "",
                    **base,
                )
            elif kind == "event":
                active = max(1, int(prof.get("active_days") or 3))
                if _PERISHABLE.search(f"{g.get('title') or ''} {d.get('brief') or ''}"):
                    active = min(active, 2)
                budget = int(prof.get("budget_min") or 180)
                spec = allocator.Spec(
                    kind="event",
                    budget=budget,
                    active_days=active,
                    floor=45,
                    max_block=180,
                    ideal=min(240, max(60, budget // active)),
                    best_time="evening" if prof.get("event_kind") in ("hookup", "romance") else "",
                    **base,
                )
            else:
                spec = allocator.Spec(kind="project", ideal=commit, floor=30, max_block=90, **base)
            spec.skip_days = {x for x in d.get("skip_days", []) if x >= today}
            spec.pin_days = {k: int(v) for k, v in (d.get("pin_days") or {}).items() if k >= today}
            if spec.kind in ("project", "event"):
                try:
                    spec.scale = max(
                        1.0, min(1.5, float(d.get("size_hint") or 1.0))
                    )  # model said slots were too small
                except (TypeError, ValueError):
                    spec.scale = 1.0
            out.append(spec)
    finally:
        generation.set_brief(saved)
    return out


def compute(conn, user, rt=None):
    """-> (alloc, report, specs, today_iso, now_min)."""
    today = today_for(user).isoformat()
    now_min = minutes_now(user)
    rt = rt or routine.merged(conn, user)
    specs = build_specs(conn, user, rt, today)
    if not specs:
        return {}, {}, [], today, now_min
    last = max(s.deadline for s in specs)
    soft_dated = routine.dated_busy(rt, today, last, key="dated_soft")
    soft_set = {tuple(w) for w in rt.get("soft", [])}
    hard_rt = dict(rt, dated=[x for x in rt.get("dated", []) if x not in rt.get("dated_soft", [])])
    dated = routine.dated_busy(hard_rt, today, last)
    always = list(_split(rt["sleep"])) if rt.get("sleep") else []
    for w in rt.get("meals", {}).values():
        always += _split(w)
    for w in rt.get("other", []):
        if tuple(w) not in soft_set:  # class, work, prayer: fixed
            always += _split(w)
    soft_always = _split(rt["workout"]) if rt.get("workout") else []
    for w in soft_set:  # gym, movie night, gaming: give way to a fire
        soft_always += _split(w)

    def hard_for(iso):
        extra = []
        for w in dated.get(iso, []):
            extra += _split(w)
        return always + extra

    def soft_for(iso):
        extra = []
        for w in soft_dated.get(iso, []):
            extra += _split(w)
        return soft_always + extra

    uid = user["id"]
    modes = routine.day_modes(conn, uid)
    focus_ids = {s.id for s in specs if s.kind in ("project", "event")}
    try:
        rows = insights.history(conn, uid, today)
        cal = insights.goal_calibration(rows, today, modes)
        owed = insights.backlog(conn, uid, today, focus_ids)
        tuning = insights.tuning_from(rows, focus_ids, modes, lambda iso: modes.get(iso, "normal"))
    except Exception as exc:  # learning must never block planning
        logger.warning("history analysis failed: %r", exc)
        rows, cal, owed = [], {}, {}
        tuning = allocator.Tuning(day_mode=lambda iso: modes.get(iso, "normal"))
    for sp in specs:
        c = cal.get(sp.id)
        if sp.id in focus_ids and c and c["scale"] < 1.0:
            sp.scale = c["scale"]  # they cannot live this plan: ease it and say so
        elif sp.id in owed:
            sp.backlog = owed[sp.id]  # missed recently: pay back in small pieces
    alloc, report = allocator.allocate(
        specs, today, _prefs(rt), hard_for, soft_for, now_min=now_min, tuning=tuning
    )
    report["insight"] = {
        "bucket_rate": tuning.bucket_rate,
        "weekend_factor": tuning.weekend_factor,
        "block_cap": tuning.block_cap,
        "calibration": cal,
        "backlog": owed,
    }
    return alloc, report, specs, today, now_min


def _hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _to_min(t):
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "half": 0.5}
_DUR = re.compile(
    r"(?<![\w.])(?P<n>\d+(?:\.\d+)?|one|two|three|four|five|six|half)(?P<sep>[- ]?)"
    r"(?P<u>hours?|hrs?|h|minutes?|mins?|min|m)\b",
    re.I,
)


def _fmt_duration(minutes, hyphen, plural_ok=True):
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h}-hour" if hyphen else f"{h} hour{'s' if h != 1 else ''}"
    return f"{minutes}-minute" if hyphen else f"{minutes} minutes"


def retime_title(title, old_min, new_min):
    """A title that names the session length ('2-hour deep dive') must follow the slot when it
    shrinks or grows. Only a phrase that matches the OLD slot is rewritten, so a sub-step like
    'ten minutes of review' is left alone."""
    if not title or not old_min or old_min == new_min:
        return title

    def swap(m):
        raw = m.group("n").lower()
        n = _WORDNUM.get(raw)
        n = float(raw) if n is None else n
        unit_h = m.group("u").lower().startswith("h")
        mins = n * 60 if unit_h else n
        if abs(mins - old_min) > max(10, 0.15 * old_min):
            return m.group(0)
        return _fmt_duration(int(new_min), hyphen=(m.group("sep") == "-"))

    return _DUR.sub(swap, title, count=1)


def reslot(conn, user, alloc, today, now_min):
    """Put every goal's EXISTING pending tasks onto the new allocation, in place. Returns the
    ids of goals whose tasks no longer fit their slots (new slots, or a slot that changed size
    a lot) and therefore need fresh titles. Done/missed tasks and anything already past stay."""
    now_hm = _hm(now_min)
    redraw = set()
    unfilled, slots = {}, {}
    goals = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id, deadline FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    }
    rows = {}
    for r in conn.execute(
        "SELECT id, goal_id, date, start_time, end_time, duration_min, title FROM actions "
        "WHERE user_id=? AND status='pending' AND date>=? ORDER BY date, start_time",
        (user["id"], today),
    ).fetchall():
        rows.setdefault(r["goal_id"], {}).setdefault(r["date"], []).append(dict(r))
    for gid, days in alloc.items():
        g = goals.get(gid)
        if not g:
            continue
        mine = rows.get(gid, {})
        left = max(generation.days_remaining(g["deadline"], today), 0)
        dd = generation.detail_days_for(left)
        cutoff = (date.fromisoformat(today) + timedelta(days=dd - 1)).isoformat() if dd else None
        if days and not mine:
            redraw.add(gid)  # nothing drawn yet
        for d in sorted(set(days) | set(mine)):
            if cutoff and d > cutoff:
                continue
            acts = mine.get(d, [])
            if d == today:
                acts = [a for a in acts if (a["start_time"] or "") >= now_hm and a["start_time"]]
            new = days.get(d, []) if d <= g["deadline"] else []
            for k in range(max(len(acts), len(new))):
                if k < len(acts) and k < len(new):
                    a, (s, e) = acts[k], new[k]
                    old = (
                        _to_min(a["end_time"]) - _to_min(a["start_time"])
                        if _to_min(a["start_time"]) is not None and _to_min(a["end_time"]) is not None
                        else (a["duration_min"] or 0)
                    )
                    if _hm(s) != a["start_time"] or _hm(e) != a["end_time"]:
                        conn.execute(
                            "UPDATE actions SET start_time=?, end_time=?, duration_min=?, title=? WHERE id=?",
                            (_hm(s), _hm(e), e - s, retime_title(a["title"], old, e - s), a["id"]),
                        )
                    if old and abs((e - s) - old) > 0.4 * old:
                        redraw.add(gid)
                elif k < len(acts):
                    conn.execute("DELETE FROM actions WHERE id=?", (acts[k]["id"],))
                else:
                    unfilled[gid] = unfilled.get(gid, 0) + 1
            slots[gid] = slots.get(gid, 0) + len(new)
    # A few slots the model could not title are not worth a redraw loop; a real hole is.
    for gid, n in unfilled.items():
        if n > 0.15 * max(slots.get(gid, 0), 1):
            redraw.add(gid)
    conn.commit()
    return redraw


def _at_risk(spec, rep):
    """A deadline goal that cannot get the time it needs before it is due. Said plainly, never hidden."""
    if spec.kind not in ("project", "event"):
        return None
    left = (date.fromisoformat(spec.deadline) - date.today()).days
    unpaid = int(rep.get("backlog") or 0)
    short = int(rep["short"])
    if left > 21 and not unpaid >= 120:
        return None
    if short >= max(60, 0.25 * max(rep["ideal"], 1)) or unpaid >= 120:
        h = (short + unpaid) / 60
        return {
            "short_min": short + unpaid,
            "note": f"About {h:.1f}h short of what this needs before it is due - cut something else or move the date.",
        }
    return None


def record_squeeze(conn, report, specs):
    """Plain-fact note on each goal: how many minutes of what it wanted it did not get, who ranked
    above it, whether it is at risk, and whether the plan was eased because it was too heavy to live.
    (Replaces the old ratchet that permanently cut other goals' hours.)"""
    by_id = {s.id: s for s in specs}
    cal = (report.get("insight") or {}).get("calibration", {})
    for gid, rep in report.items():
        if not isinstance(gid, int):  # "days" / "insight" are not goals
            continue
        spec = by_id.get(gid)
        row = conn.execute("SELECT details FROM goals WHERE id=?", (gid,)).fetchone()
        if not spec or not row:
            continue
        d = _jl(row["details"], {})
        lost = int(rep["short"])
        names = [
            o.title for o in sorted(specs, key=lambda o: -o.weight) if o.id != gid and o.weight > spec.weight
        ][:3]
        want = {"squeezed_min": lost if lost >= 60 else 0, "squeezed_by": names if lost >= 60 else []}
        risk = _at_risk(spec, rep)
        c = cal.get(gid)
        eased = {"rate": c["rate"], "scale": c["scale"]} if c and c["scale"] < 1.0 else None
        changed = any(d.get(k) != v for k, v in want.items()) or "cap_min" in d
        for key, val in (("at_risk", risk), ("eased", eased)):
            if val:
                if d.get(key) != val:
                    d[key] = val
                    changed = True
            elif key in d:
                d.pop(key)
                changed = True
        if changed:
            d.update(want)
            d.pop("cap_min", None)  # the old permanent cut, if any, is lifted
            conn.execute("UPDATE goals SET details=? WHERE id=?", (json.dumps(d), gid))
    conn.commit()


def rebalance(conn, user, rt=None):
    """Recompute the joint allocation and bring the board onto it. Returns goal ids needing new titles."""
    alloc, report, specs, today, now_min = compute(conn, user, rt)
    if not specs:
        return set()
    redraw = reslot(conn, user, alloc, today, now_min)
    try:
        record_squeeze(conn, report, specs)
    except Exception as exc:  # a note must never block the schedule
        logger.warning("squeeze note failed: %r", exc)
    return redraw


def apply_day_mode(conn, user, iso, mode):
    """Set how ONE day is planned ('sick today', 'travelling Friday', 'light day') and re-plan only what it touches.
    Today's unfinished blocks become 'missed' so the work is carried into the next days (backlog), not lost.
    Returns the goal ids that need new titles."""
    today = today_for(user).isoformat()
    routine.set_day_mode(conn, user["id"], iso, mode, today=today)
    if iso == today and mode in allocator.EXCUSED_MODES:
        conn.execute(
            "UPDATE actions SET status='missed' WHERE user_id=? AND date=? AND status='pending' "
            "AND goal_id IN (SELECT id FROM goals WHERE status='active')",
            (user["id"], today),
        )
        conn.commit()
    return rebalance(conn, user)


_MODE_WORDS = {"light": "a light day", "travel": "a travel day", "sick": "a sick day", "off": "a day off"}


def explain(conn, user, days=7):
    """Why the plan looks the way it does: per-day reasons, per-goal facts, and questions worth asking."""
    alloc, report, specs, today, _now = compute(conn, user)
    out = {"days": [], "goals": [], "ask": []}
    if not specs:
        return out
    info = report.get("days", {})
    for k in range(days):
        iso = (date.fromisoformat(today) + timedelta(days=k)).isoformat()
        d = info.get(iso)
        if not d:
            continue
        why, mode = d.get("why"), d.get("mode")
        if why == "heavy_streak":
            note = "Lighter day: three heavy days in a row, so this one is capped near 4h. Rest is part of the plan."
        elif why == "mode":
            note = f"Planned as {_MODE_WORDS.get(mode, mode)}, as you asked."
        elif why == "weekend":
            note = "Lighter weekend: you have finished less on weekends lately."
        else:
            note = ""
        rest = d.get("rest")
        out["days"].append(
            {
                "date": iso,
                "mode": mode,
                "total_min": d.get("total", 0),
                "light": bool(d.get("light")),
                "note": note,
                "rest": [_hm(rest[0]), _hm(rest[1])] if rest else None,
            }
        )
    ins = report.get("insight", {})
    names = {s.id: s.title for s in specs}
    for gid, c in (ins.get("calibration") or {}).items():
        if c["scale"] < 1.0 and gid in names:
            out["goals"].append(
                {
                    "goal_id": gid,
                    "kind": "eased",
                    "note": f"You finished {int(c['rate'] * 100)}% of '{names[gid]}' blocks lately, so daily time was cut {int(round((1 - c['scale']) * 100))}% to something you can keep.",
                }
            )
    for gid, m in (ins.get("backlog") or {}).items():
        sp = next((s for s in specs if s.id == gid), None)
        if sp and sp.backlog and m >= 30:
            out["goals"].append(
                {
                    "goal_id": gid,
                    "kind": "catching_up",
                    "note": f"Catching up about {m / 60:.1f}h missed on '{names[gid]}' in small pieces over the next days.",
                }
            )
    for s in specs:
        r = _at_risk(s, report[s.id])
        if r:
            out["goals"].append({"goal_id": s.id, "kind": "at_risk", "note": f"'{s.title}': {r['note']}"})
        if report[s.id]["borrowed"] >= 15:
            out["goals"].append(
                {
                    "goal_id": s.id,
                    "kind": "borrowed",
                    "note": f"Took {report[s.id]['borrowed']} min of your negotiable time for '{s.title}' (shortened, not cancelled).",
                }
            )
    kinds = routine.window_kinds(conn, user["id"])
    for lab in routine.window_labels(conn, user):
        if not triage.window_known(lab) and routine.norm_label(lab) not in kinds:
            out["ask"].append(
                {"label": lab, "question": f"Can '{lab}' move if something important needs the time?"}
            )
    return out