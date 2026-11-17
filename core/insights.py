from datetime import date, timedelta
from typing import Dict, List, Optional

from core import allocator

MIN_BUCKET_SAMPLES = 6        # a time of day needs this many resolved blocks before we believe it
MIN_WEEK_SAMPLES = 8          # each of weekday 
MIN_GOAL_SAMPLES = 6          # a goal needs this many resolved blocks before its plan is judged
LOOKBACK_DAYS = 30
GOAL_WINDOW_DAYS = 14
BACKLOG_DAYS = 7
EXCUSED = ("sick", "off", "travel")    


def _minutes(t) -> Optional[int]:
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
   
    except Exception:
        return None


def history(conn, user_id: int, today: str, days: int = LOOKBACK_DAYS) -> List[dict]:
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT goal_id, date, start_time, duration_min, status FROM actions "
        "WHERE user_id=? AND date>=? AND date<=? AND status IN ('done','missed')",
        (user_id, since, today)).fetchall()
  
    out = []
  
    for r in rows:
        if r["status"] == "missed" and r["date"] >= today:
            continue                               # today is not over yet
        out.append({"goal_id": r["goal_id"], "date": r["date"], "start": _minutes(r["start_time"]),
                    "dur": r["duration_min"] or 60, "done": r["status"] == "done"})
    
    return out


def _rate(done: int, n: int) -> float:
    return (done + 2) / (n + 3)                   


def _counted(rows, modes):
    return [r for r in rows if modes.get(r["date"]) not in EXCUSED]


def bucket_rates(rows, modes=None) -> Dict[str, float]:
    by: Dict[str, List[bool]] = {}
   
    for r in _counted(rows, modes or {}):
        if r["start"] is not None:
            by.setdefault(allocator.bucket_of(r["start"]), []).append(r["done"])
  
    return {b: round(_rate(sum(v), len(v)), 3) for b, v in by.items() if len(v) >= MIN_BUCKET_SAMPLES}


def weekend_factor(rows, modes=None) -> float:
    wk, we = [], []
   
    for r in _counted(rows, modes or {}):
        (we if date.fromisoformat(r["date"]).weekday() >= 5 else wk).append(r["done"])
   
    if len(wk) < MIN_WEEK_SAMPLES or len(we) < MIN_WEEK_SAMPLES:
        return 1.0
    ratio = _rate(sum(we), len(we)) / max(_rate(sum(wk), len(wk)), 0.05)
    
    return round(max(0.5, min(1.0, ratio)), 2)     # only ever lighter: we do not stack extra onto a free weekend


def block_cap(rows, project_goal_ids, modes=None) -> Optional[int]:
    long_, short = [], []
    
    for r in _counted(rows, modes or {}):
        if r["goal_id"] not in project_goal_ids:
            continue
        if r["dur"] >= 90:
            long_.append(r["done"])
        elif 30 <= r["dur"] <= 75:
            short.append(r["done"])
   
    if len(long_) < MIN_BUCKET_SAMPLES or len(short) < MIN_BUCKET_SAMPLES:
        return None
    rl, rs = _rate(sum(long_), len(long_)), _rate(sum(short), len(short))
   
    if rs - rl < 0.2:
        return None
    
    return 60 if rl < 0.5 else (75 if rl < 0.65 else None)


def goal_calibration(rows, today: str, modes=None) -> Dict[int, dict]:
    since = (date.fromisoformat(today) - timedelta(days=GOAL_WINDOW_DAYS)).isoformat()
    by: Dict[int, List[bool]] = {}
    
    for r in _counted(rows, modes or {}):
        if r["date"] >= since:
            by.setdefault(r["goal_id"], []).append(r["done"])
    out = {}
   
    for gid, v in by.items():
        if len(v) < MIN_GOAL_SAMPLES:
            continue
        raw = sum(v) / len(v)
        scale = 0.6 if raw < 0.3 else (0.75 if raw < 0.5 else 1.0)
        out[gid] = {"rate": round(raw, 2), "n": len(v), "scale": scale}
   
    return out


def backlog(conn, user_id: int, today: str, project_goal_ids) -> Dict[int, int]:
    since = (date.fromisoformat(today) - timedelta(days=BACKLOG_DAYS)).isoformat()
    out: Dict[int, int] = {}
   
    for r in conn.execute(
            "SELECT goal_id, SUM(COALESCE(duration_min, 60)) m FROM actions WHERE user_id=? AND status='missed' "
            "AND date>=? AND date<? GROUP BY goal_id", (user_id, since, today)).fetchall():
        if r["goal_id"] in project_goal_ids:
            out[r["goal_id"]] = int(r["m"] or 0)
    
    return out


def tuning_from(rows, project_goal_ids, modes, day_mode) -> allocator.Tuning:
    return allocator.Tuning(bucket_rate=bucket_rates(rows, modes),
                            weekend_factor=weekend_factor(rows, modes),
                            block_cap=block_cap(rows, project_goal_ids, modes),
                            day_mode=day_mode)