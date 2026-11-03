"""Slot engine: decides WHEN work happens. (The model only decides WHAT to do in a slot.)

Built around how people actually work, not around filling a grid:

* Focus blocks of 45-90 minutes, never a wall of 45-minute chunks glued together.
* A real break after every block (15 min) and a longer reset (30 min) after two.
* At most two blocks per energy window, then the person gets their day back.
* Work lands in the person's best hours (morning / afternoon / evening type),
  not simply "as early as the grid allows".
* Nothing before they've had time to wake up (wake + 45 min) or inside the
  wind-down before bed (bed - 60 min). Meals, gym and every busy window keep a
  10-minute buffer on both sides.
* The load has a shape: a gentle drip when the deadline is far, full effort in
  the last stretch, a lighter day the day before, a morning-only finish on
  deadline day, and a lighter day roughly once a week on long plans.
* Today only gets hours that are still in the future.

Pure functions, no I/O, so the whole policy is unit-tested.
"""
import math
from dataclasses import dataclass
from datetime import date, timedelta

DAY = 24 * 60
BUFFER = 10            # minutes of air around any busy window
SHORT_BREAK = 15
LONG_BREAK = 30
WAKE_LEAD = 45         # no work until this long after waking
WIND_DOWN = 60         # no work this close to bed
MAX_BLOCKS_PER_DAY = 4
DAY_FOCUS_CAP = 6 * 60   # across ALL goals: nobody does more than ~6h of deep work a day
MAX_BLOCKS_PER_WINDOW = 2
MIN_BLOCK = 40         # no scraps: a block shorter than this isn't worth sitting down for
LEAD_TODAY = 30        # earliest start today = now + this

_H = 60
ENERGY_WINDOWS = {
    # (start, end) in minutes from midnight, in order of preference
    "morning":   [(8 * _H + 30, 12 * _H + 30), (15 * _H, 17 * _H + 30), (19 * _H + 30, 21 * _H)],
    "afternoon": [(13 * _H + 30, 17 * _H + 30), (9 * _H + 30, 12 * _H), (19 * _H + 30, 21 * _H + 30)],
    "balanced":  [(9 * _H + 30, 12 * _H + 30), (15 * _H, 18 * _H), (19 * _H + 30, 21 * _H + 30)],
    "evening":   [(11 * _H, 13 * _H), (16 * _H, 19 * _H), (20 * _H, 23 * _H + 30)],
}


@dataclass
class Prefs:
    wake: int = 7 * _H
    bed: int = 23 * _H          # may exceed 24h when bedtime is after midnight
    chronotype: str = "balanced"
    commit_min: int = 120       # what the person said they can give per day


def ceil15(m):
    return int(math.ceil(m / 15.0) * 15)


def round15(m):
    return int(round(m / 15.0) * 15)


def infer_chronotype(text, wake=None, bed=None):
    """Explicit answer wins; otherwise infer from sleep times; otherwise balanced."""
    t = (text or "").lower()
    if any(w in t for w in ("morning", "early", "am ", "sunrise", "dawn")) or t.strip() in ("am", "morning"):
        return "morning"
    if any(w in t for w in ("afternoon", "midday", "noon")):
        return "afternoon"
    if any(w in t for w in ("night", "evening", "late", "owl", "pm")):
        return "evening"
    if wake is not None and bed is not None:
        if wake >= 9 * _H + 30 or bed >= 25 * _H:
            return "evening"
        if wake <= 5 * _H + 30:
            return "morning"
    return "balanced"


def normalize_sleep(bed, wake):
    """bed/wake as minutes-from-midnight. A bedtime shortly after midnight is the
    END of the same waking day, so it becomes bed + 24h."""
    if bed is None or wake is None:
        return None
    if bed <= wake:
        bed += DAY
    # a bedtime of e.g. 01:00 with wake 09:00 -> 25:00; bed 23:00 wake 07:00 -> 23:00
    if bed - wake > 14 * _H:     # slept < 10h means the day is >14h long: fine; guard nonsense
        pass
    return bed


def block_plan(commit_min):
    """(number_of_blocks, block_length) for a daily commitment."""
    commit = max(15, int(commit_min))
    if commit <= 90:
        return 1, max(15, round15(commit)) if commit >= 30 else commit
    n = min(MAX_BLOCKS_PER_DAY, max(2, math.ceil(commit / 75.0)))
    length = max(45, min(90, round15(commit / float(n))))
    return n, length


def merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def pad(intervals, by=BUFFER):
    return merge([(s - by, e + by) for s, e in intervals])


def subtract(window, busy):
    """Free segments of `window` after removing merged busy intervals."""
    ws, we = window
    segs, cur = [], ws
    for bs, be in busy:
        if be <= cur:
            continue
        if bs >= we:
            break
        if bs > cur:
            segs.append((cur, min(bs, we)))
        cur = max(cur, be)
        if cur >= we:
            break
    if cur < we:
        segs.append((cur, we))
    return segs


def load_for_day(days_left, total_days, n_blocks, index):
    """-> (max_blocks, latest_end or None, kind). Shapes effort across the horizon."""
    if total_days == 1:
        return n_blocks, None, "crunch"
    if days_left == 0:
        return min(2, n_blocks), 14 * _H, "deadline"          # finish early on the day
    if days_left == 1 and total_days >= 4:
        return min(3, n_blocks), 19 * _H, "eve"               # no cramming into the night
    if total_days >= 9 and days_left > 3 and (index + 1) % 7 == 0:
        return 1, 13 * _H, "light"                            # a lighter day about weekly
    if days_left > 21:
        return 1, None, "drip"
    if days_left > 7:
        return max(1, math.ceil(n_blocks * 0.75)), None, "build"
    return n_blocks, None, "push"


def plan_day(prefs, busy, *, max_blocks, length, latest_end=None, earliest=None, others=()):
    """Place focus blocks inside one day. `busy` = fixed life (sleep, meals, gym...) as
    (start, end) minutes; `others` = focus blocks other goals already own that day.
    Returns sorted [(start, end)]."""
    bed = prefs.bed
    lo = prefs.wake + WAKE_LEAD
    hi = bed - WIND_DOWN
    if latest_end is not None:
        hi = min(hi, latest_end)
    if earliest is not None:
        lo = max(lo, earliest)
    if hi - lo < MIN_BLOCK:
        return []
    others = list(others)
    # other goals' blocks get a real break on both sides, and count toward the daily cap
    busy = merge(pad(busy) + pad(others, SHORT_BREAK))
    existing = sum(e - s for s, e in others)
    budget = min(max_blocks * length, max(0, DAY_FOCUS_CAP - existing))
    placed = []
    min_block = min(MIN_BLOCK, length)

    def fill(segments):
        nonlocal budget
        for seg_s, seg_e in segments:
            t = ceil15(seg_s)
            in_row = 0
            while len(placed) < max_blocks and in_row < MAX_BLOCKS_PER_WINDOW and budget >= min_block:
                room = seg_e - t
                if room < min_block:
                    break
                use = min(length, room, budget)
                if use < min_block:
                    break
                # don't leave a sliver: absorb a short remainder into this block
                if 0 < room - use < min_block and room <= length + min_block // 2:
                    use = min(room, budget)
                placed.append((t, t + use))
                budget -= use
                in_row += 1
                t = ceil15(t + use + (SHORT_BREAK if in_row == 1 else LONG_BREAK))
            if len(placed) >= max_blocks:
                break

    # 1) the person's best hours first
    for ws, we in ENERGY_WINDOWS.get(prefs.chronotype, ENERGY_WINDOWS["balanced"]):
        ws, we = max(ws, lo), min(we, hi)
        if we - ws < min_block:
            continue
        fill(subtract((ws, we), merge(busy + pad(placed, SHORT_BREAK))))
        if len(placed) >= max_blocks or budget < min_block:
            break
    # 2) still owed time and the best hours are full (uni, gym, dinner...)? use any other
    #    free waking hour rather than silently dropping the work
    if len(placed) < max_blocks and budget >= min_block:
        fill(subtract((lo, hi), merge(busy + pad(placed, SHORT_BREAK))))
    return sorted(placed)


def plan_horizon(today, deadline, prefs, busy_for, *, now_min=None, max_days=121, others_for=None):
    """Plan every day from `today` to `deadline` inclusive.
    busy_for(date_iso) -> list of (start, end) minutes for that date.
    now_min: minutes since midnight right now (only affects `today`).
    Returns [(date_iso, [(start, end), ...]), ...] in date order (days may be empty)."""
    d0, d1 = date.fromisoformat(today), date.fromisoformat(deadline)
    total = min(max(0, (d1 - d0).days) + 1, max_days)
    n_blocks, length = block_plan(prefs.commit_min)
    out = []
    for i in range(total):
        day = d0 + timedelta(days=i)
        days_left = total - 1 - i
        max_blocks, latest_end, _kind = load_for_day(days_left, total, n_blocks, i)
        earliest = None
        if i == 0 and now_min is not None:
            earliest = ceil15(now_min + LEAD_TODAY)
        slots = plan_day(prefs, busy_for(day.isoformat()), max_blocks=max_blocks,
                         length=length, latest_end=latest_end, earliest=earliest,
                         others=(others_for(day.isoformat()) if others_for else ()))
        out.append((day.isoformat(), slots))
    return out


def fmt(m):
    m %= DAY
    return f"{m // 60:02d}:{m % 60:02d}"
