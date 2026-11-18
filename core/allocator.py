import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from core import planner as P

DAY_CAP = 8 * 60
SOFT_SHARE = 0.5
HEAVY_DAY = 5 * 60 + 30
HEAVY_STREAK = 3
LIGHT_CAP = 4 * 60
BREAK = P.SHORT_BREAK
TOUCH = 30
LAST_MIN = 24 * 60 - 15  # never schedule into the last quarter hour: times must stay HH:MM < 24:00
MAX_BLOCKS_PER_DAY = 4
FILL = 0.85  # share of free time we plan to use; fragmentation eats the rest

SESSION_WEEKDAYS = {
    1: (2,),
    2: (1, 4),
    3: (0, 2, 4),
    4: (0, 1, 3, 4),
    5: (0, 1, 2, 4, 5),
    6: (0, 1, 2, 3, 4, 5),
    7: (0, 1, 2, 3, 4, 5, 6),
}

Seg = Tuple[int, int]

MODE_FACTOR = {"normal": 1.0, "light": 0.5, "travel": 0.4, "sick": 0.0, "off": 0.0}
EXCUSED_MODES = ("sick", "off")
REST_MIN = 45  # a real gap in the middle of a long day (walk, nap, prayer, food)
REST_TRIGGER = 4 * 60
NEUTRAL_RATE = 0.7


def bucket_of(minute: int) -> str:
    m = minute % 1440
    if 300 <= m < 720:
        return "morning"
    if 720 <= m < 1020:
        return "afternoon"
    if 1020 <= m < 1320:
        return "evening"
    return "night"


@dataclass
class Tuning:
    """What the person's real history says. Every field is optional; empty = the plain plan."""

    bucket_rate: Dict[str, float] = field(
        default_factory=dict
    )  # time of day -> share of blocks actually done
    weekend_factor: float = 1.0  # learned: do weekends carry less?
    day_mode: Optional[Callable[[str], str]] = None  # iso date -> normal|light|travel|sick|off
    block_cap: Optional[int] = None  # learned longest focus block they really finish
    rest: bool = True


@dataclass
class Spec:
    id: int
    deadline: str
    weight: float = 1.0
    kind: str = "project"  # project | habit | recurring | event
    ideal: int = 120
    floor: int = TOUCH
    max_block: int = 90
    min_block: int = 30
    per_week: int = 3  # habit / recurring sessions per week
    best_time: str = ""
    important: bool = False
    budget: int = 0
    active_days: int = 3
    windows: List[Seg] = field(default_factory=list)
    uses_soft: bool = False
    touch: int = 0
    title: str = ""
    backlog: int = 0
    scale: float = 1.0


def _demand(g: Spec, day: date, d0: date):

    dl = date.fromisoformat(g.deadline)
    left = (dl - day).days
    if left < 0:
        return 0, 0, False, None
    total = (dl - d0).days + 1
    i = (day - d0).days
    floor = max(g.floor, g.min_block)
    if g.kind == "event":
        if left < g.active_days:
            share = P.round15(g.budget * g.scale / max(1, min(g.active_days, total))) if g.budget else g.ideal
            ideal = min(max(share, floor), 240)
            return ideal, min(floor, ideal), True, None
        if left == 5 and total > g.active_days + 3:  # one early slot for anything with lead time
            return 60, min(floor, 60), False, None
        return 0, 0, False, None
    if g.kind in ("habit", "recurring"):
        days = SESSION_WEEKDAYS[max(1, min(int(g.per_week or 3), 7))]
        ideal = g.ideal if day.weekday() in days else 0
        return ideal, min(floor, g.ideal), True, None

    latest = None
    if total == 1:
        frac = 1.0
    elif left == 0:
        frac, latest = 0.6, 14 * 60  # finish early on the day
    elif left == 1 and total >= 4:
        frac, latest = 0.8, 19 * 60  # no cramming into the night before
    elif total >= 9 and left > 3 and (i + 1) % 7 == 0:
        frac = 0.4  # a lighter day about weekly
    elif left > 21:
        frac = 0.5
    elif left > 7:
        frac = 0.75
    else:
        frac = 1.0
    ideal = max(floor, P.round15(g.ideal * g.scale * frac))
    return ideal, min(max(floor, P.round15(g.ideal * 0.3)), ideal), True, latest


def _inter(segs: List[Seg], w: Seg) -> List[Seg]:
    out = []
    for s, e in segs:
        a, b = max(s, w[0]), min(e, w[1])
        if b > a:
            out.append((a, b))
    return out


def _free(base: List[Seg], taken: List[Seg]) -> List[Seg]:
    if not taken:
        return list(base)
    occ = P.pad(taken, BREAK)
    out = []
    for seg in base:
        out += P.subtract(seg, occ)
    return out


def _chrono(g: Spec, prefs) -> str:
    return {"morning": "morning", "afternoon": "afternoon", "evening": "evening", "night": "night"}.get(
        g.best_time
    ) or prefs.chronotype


def _candidates(g: Spec, free: List[Seg], prefs, latest, rates=None) -> List[Seg]:
    if latest is not None:
        free = _inter(free, (0, latest))
    ordered: List[Seg] = []
    energy = list(P.ENERGY_WINDOWS.get(_chrono(g, prefs), P.ENERGY_WINDOWS["balanced"]))
    learned = bool(rates) and not g.best_time and not g.windows
    if learned:  # what they ACTUALLY finish beats what the chronotype guesses
        energy.sort(key=lambda w: -rates.get(bucket_of(w[0]), NEUTRAL_RATE))
    for w in list(g.windows) + energy:
        ordered += _inter(free, w)
    # then any other free waking time rather than silently dropping the work
    if learned:
        ordered += sorted(free, key=lambda sg: (-rates.get(bucket_of(sg[0]), NEUTRAL_RATE), sg[0]))
    else:
        ordered += sorted(free)
    seen, out = set(), []
    for seg in ordered:
        if seg not in seen and seg[1] - seg[0] >= g.min_block:
            seen.add(seg)
            out.append(seg)
    return out


def _cut(segs: List[Seg], w: Seg) -> List[Seg]:
    out: List[Seg] = []
    for seg in segs:
        out += P.subtract(seg, [w])
    return out


def _carve_rest(segs: List[Seg]) -> Optional[Seg]:
    """A REST_MIN gap at the middle of the day's free time, so a long day has a real pause in it."""
    segs = sorted(segs)
    total = sum(e - s for s, e in segs)
    if total < 300:
        return None
    mid, acc = total / 2, 0
    for s, e in segs:
        if acc + (e - s) >= mid:
            centre = s + int(mid - acc)
            a = max(s, ((centre - REST_MIN // 2) // 15) * 15)
            b = a + REST_MIN
            if b > e:
                b = (e // 15) * 15
                a = b - REST_MIN
            return (a, b) if a >= s and b <= e else None
        acc += e - s
    return None


def _place(
    g: Spec, want: int, allowed: List[Seg], taken: List[Seg], prefs, latest, rates=None, block_cap=None
) -> List[Seg]:
    """Put `want` minutes for goal g into blocks. Mutates `taken`. Returns the blocks."""
    blocks: List[Seg] = []
    remaining = int(want)
    longest = g.max_block
    if block_cap and g.kind in ("project", "event"):
        longest = max(g.min_block, min(longest, int(block_cap)))  # they really finish blocks up to this long
    while remaining >= g.min_block and len(blocks) < MAX_BLOCKS_PER_DAY:
        placed = False
        for seg_s, seg_e in _candidates(g, _free(allowed, taken), prefs, latest, rates):
            t = P.ceil15(seg_s)
            room = seg_e - t
            if room < g.min_block:
                continue
            use = min(longest, room, remaining)
            if 0 < remaining - use < g.min_block and room >= remaining:
                use = min(room, remaining, longest + g.min_block // 2)  # absorb a sliver
            use = (use // 5) * 5
            if use < g.min_block:
                continue
            blocks.append((t, t + use))
            taken.append((t, t + use))
            remaining -= use
            placed = True
            break
        if not placed:
            break
    return sorted(blocks)


def allocate(
    specs: List[Spec],
    today: str,
    prefs: "P.Prefs",
    hard_busy_for: Callable[[str], List[Seg]],
    soft_busy_for: Optional[Callable[[str], List[Seg]]] = None,
    *,
    now_min: Optional[int] = None,
    max_days: int = 121,
    tuning: Optional[Tuning] = None
):
    """-> (alloc, report). alloc[goal_id][date_iso] = [(start, end), ...].
    report[goal_id] = {ideal, got, short, missed_pairs, borrowed, backlog} in minutes / counts.
    report["days"][date_iso] = {mode, total, light, why, rest} explains each day."""
    tuning = tuning or Tuning()
    rates = tuning.bucket_rate or {}
    d0 = date.fromisoformat(today)
    if not specs:
        return {}, {}
    last = max(date.fromisoformat(s.deadline) for s in specs)
    span = max(1, min((last - d0).days + 1, max_days))
    alloc: Dict[int, Dict[str, List[Seg]]] = {s.id: {} for s in specs}
    report = {
        s.id: {"ideal": 0, "got": 0, "short": 0, "missed_pairs": 0, "borrowed": 0, "backlog": s.backlog}
        for s in specs
    }
    report["days"] = {}
    backlog_left = {s.id: s.backlog for s in specs}
    last_served = {s.id: -1 for s in specs}  # assume everyone was served "yesterday"
    heavy_run = 0  # consecutive heavy days so far

    for i in range(span):
        day = d0 + timedelta(days=i)
        iso = day.isoformat()
        mode = (tuning.day_mode(iso) if tuning.day_mode else "normal") or "normal"
        if mode in EXCUSED_MODES:  # sick / day off: plan nothing, owe nothing
            for g in specs:
                last_served[g.id] = i
            heavy_run = 0
            report["days"][iso] = {"mode": mode, "total": 0, "light": True, "why": mode, "rest": None}
            continue
        factor = MODE_FACTOR.get(mode, 1.0)
        weekend_cut = day.weekday() >= 5 and tuning.weekend_factor < 1.0
        if day.weekday() >= 5:
            factor *= tuning.weekend_factor
        lo = prefs.wake + P.WAKE_LEAD
        hi = min(prefs.bed - P.WIND_DOWN, LAST_MIN)
        if i == 0 and now_min is not None:
            lo = max(lo, P.ceil15(now_min + P.LEAD_TODAY))
        hard = P.merge(hard_busy_for(iso))
        soft = P.merge(soft_busy_for(iso)) if soft_busy_for else []
        general = P.subtract((lo, hi), P.pad(hard)) if hi - lo >= TOUCH else []
        with_soft = list(general)
        if soft:
            soft_free = []
            for w in soft:
                length = w[1] - w[0]
                give = (
                    min(length, max(int(length * SOFT_SHARE), 45)) // 15
                ) * 15  # half, but a 1h gym still yields 45 min
                if give >= TOUCH:
                    soft_free += _inter(general, (w[0], w[0] + give))
            general = []
            for seg in with_soft:
                general += P.subtract(seg, soft)
        else:
            soft_free = []
        free_total = sum(e - s for s, e in with_soft)

        # ---- who wants time today -------------------------------------------
        today_goals = []
        for g in specs:
            if (date.fromisoformat(g.deadline) - day).days < 0:
                continue
            ideal, floor, strict, latest = _demand(g, day, d0)
            owed = strict and (i - last_served[g.id]) >= 2  # missed yesterday -> must be served today
            if ideal == 0 and owed:
                ideal = g.touch or max(g.min_block, min(max(floor, g.floor), g.ideal))  # a rest-day touch
                floor = min(floor or ideal, ideal)
            if ideal <= 0:
                continue
            base, boost = ideal, 0
            if g.kind in ("project", "event") and backlog_left[g.id] >= 15 and strict:
                left_days = (date.fromisoformat(g.deadline) - day).days
                boost = min(
                    P.round15(backlog_left[g.id] / max(1, min(left_days + 1, 5))),
                    P.round15(ideal * 0.5),
                    backlog_left[g.id],
                )
                ideal += boost  # catch up in small daily pieces, not one cram day
            floor = max(g.min_block, min(floor, ideal))
            today_goals.append(
                {
                    "g": g,
                    "ideal": ideal,
                    "floor": floor,
                    "owed": owed,
                    "latest": latest,
                    "base": base,
                    "boost": boost,
                }
            )
            report[g.id]["ideal"] += ideal
        if not today_goals:
            heavy_run = 0
            continue

        def prio(c):
            return -(c["g"].weight * (3.0 if c["owed"] else 1.0))

        order = sorted(today_goals, key=prio)

        # ---- a real pause in the middle of a long day -------------------------------
        rest_win = None
        if tuning.rest and sum(c["ideal"] for c in today_goals) > REST_TRIGGER:
            rest_win = _carve_rest(with_soft)
            if rest_win:
                general = _cut(general, rest_win)
                with_soft = _cut(with_soft, rest_win)
                soft_free = _cut(soft_free, rest_win)
                free_total = sum(e - s for s, e in with_soft)

        # ---- how many minutes each gets ---------------------------------------
        eve = any(
            (date.fromisoformat(c["g"].deadline) - day).days <= 1 and c["g"].kind != "habit"
            for c in today_goals
        )
        heavy_rest = heavy_run >= HEAVY_STREAK and not eve
        day_cap = LIGHT_CAP if heavy_rest else DAY_CAP
        cap = int(min(day_cap, free_total) * FILL * factor)
        minutes = {c["g"].id: 0 for c in order}
        left = cap
        for c in order:  # 1) floors, best-ranked first
            give = c["floor"] if left >= c["floor"] else (left // 5) * 5
            if give >= c["g"].min_block:
                minutes[c["g"].id] = give
                left -= give
        for _ in range(8):  # 2) the rest by weight, up to what each wants
            open_ = [c for c in order if minutes[c["g"].id] > 0 and minutes[c["g"].id] < c["ideal"]]
            if left < 5 or not open_:
                break
            tw = sum(c["g"].weight for c in open_) or 1.0
            spent = 0
            for c in open_:
                share = int(left * c["g"].weight / tw / 5) * 5
                add = min(share, c["ideal"] - minutes[c["g"].id])
                if add >= 5:
                    minutes[c["g"].id] += add
                    spent += add
            if spent == 0:
                c = open_[0]
                add = min((left // 5) * 5, c["ideal"] - minutes[c["g"].id])
                if add < 5:
                    break
                minutes[c["g"].id] += add
                spent = add
            left -= spent

        # ---- put them on the clock; repair if fragmentation starved someone --------
        placed: Dict[int, List[Seg]] = {}
        for _attempt in range(5):
            taken: List[Seg] = []
            placed = {}
            for c in order:
                g = c["g"]
                if minutes[g.id] < g.min_block:
                    placed[g.id] = []
                    continue
                allowed = with_soft if g.uses_soft else general
                placed[g.id] = _place(
                    g, minutes[g.id], allowed, taken, prefs, c["latest"], rates, tuning.block_cap
                )
            starved = [c for c in order if minutes[c["g"].id] >= c["g"].min_block and not placed[c["g"].id]]
            if not starved:
                break
            victims = [c for c in reversed(order) if c not in starved and minutes[c["g"].id] > c["floor"]]
            if victims:
                minutes[victims[0]["g"].id] = victims[0]["floor"]
                continue
            victims = [
                c for c in reversed(order) if c not in starved and not c["owed"] and minutes[c["g"].id] > 0
            ]
            if victims:
                minutes[victims[0]["g"].id] = 0
                continue
            break

        # ---- an important goal that is still short may borrow the soft window --------
        if soft_free:
            taken = [b for bl in placed.values() for b in bl]
            for c in order:
                g = c["g"]
                got = sum(e - s for s, e in placed.get(g.id, []))
                if not g.important or g.uses_soft or got >= c["ideal"]:
                    continue
                extra = _place(
                    g, c["ideal"] - got, soft_free, taken, prefs, c["latest"], rates, tuning.block_cap
                )
                if extra:
                    placed[g.id] = sorted(placed.get(g.id, []) + extra)
                    report[g.id]["borrowed"] += sum(e - s for s, e in extra)

        day_total = sum(e - s for bl in placed.values() for s, e in bl)
        why = (
            "heavy_streak"
            if heavy_rest
            else ("mode" if mode != "normal" else ("weekend" if weekend_cut else ""))
        )
        report["days"][iso] = {
            "mode": mode,
            "total": day_total,
            "light": bool(why),
            "why": why,
            "rest": rest_win,
        }
        heavy_run = heavy_run + 1 if day_total >= HEAVY_DAY else 0
        for c in order:
            g = c["g"]
            blocks = placed.get(g.id, [])
            got = sum(e - s for s, e in blocks)
            report[g.id]["got"] += got
            report[g.id]["short"] += max(0, c["ideal"] - got)
            if c["boost"]:
                backlog_left[g.id] -= min(c["boost"], max(0, got - c["base"]))
                report[g.id]["backlog"] = backlog_left[g.id]
            if blocks:
                alloc[g.id][iso] = blocks
                last_served[g.id] = i
            elif c["owed"]:
                report[g.id]["missed_pairs"] += 1
    return alloc, report


def overlaps(alloc: Dict[int, Dict[str, List[Seg]]]):
    """Every pair of blocks from any goals that share a minute. Must always be empty."""
    by_day: Dict[str, List[Tuple[int, int, int]]] = {}
    for gid, days in alloc.items():
        for d, bl in days.items():
            for s, e in bl:
                by_day.setdefault(d, []).append((s, e, gid))
    bad = []

    for d, items in by_day.items():
        items.sort()
        for a, b in zip(items, items[1:]):
            if b[0] < a[1]:
                bad.append((d, a, b))
    return bad