import json
import re

from core import generation

DEFAULT_SLEEP = (23 * 60, 7 * 60)
DEFAULT_MEALS = {"breakfast": (7 * 60 + 30, 8 * 60 + 15), "lunch": (13 * 60, 13 * 60 + 45),
                 "dinner": (19 * 60 + 30, 20 * 60 + 15)}

_ACTIVITY_WORDS = (
    ("sleep", ("sleep", "bed time", "bedtime", "asleep", "nap")),
    ("workout", ("gym", "workout", "work out", "exercise", "training", "run", "jog", "yoga", "swim")),
    ("breakfast", ("breakfast",)),
    ("lunch", ("lunch",)),
    ("dinner", ("dinner", "supper", "iftar")),
)

_VAGUE = ("morning", "afternoon", "evening", "night")


def activity_of(label):
    low = (label or "").lower()
   
    for key, words in _ACTIVITY_WORDS:
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            return key
   
    return None


def _vague(label, wake):
    low = (label or "").lower()
   
    for w in _VAGUE:
        if re.search(rf"\b{w}\b", low):
            return {"morning": (wake + 30, wake + 90), "afternoon": (15 * 60, 16 * 60),
                    "evening": (18 * 60, 19 * 60), "night": (21 * 60, 22 * 60)}[w]
   
    return None


def window_of(label, wake=7 * 60):
    return generation.parse_time_window(label) or _vague(label, wake)


def _jl(raw, default):
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if v is not None else default
   
    except Exception:
        return default


def _apply(state, key, win, source):
    if win is None or key in state["seen"]:
        return
    state["seen"].add(key)
    state["src"][key] = source
  
    if key == "sleep":
        state["sleep"] = (win[0] % 1440, win[1] % 1440)
   
    elif key == "workout":
        state["workout"] = (win[0] % 1440, (win[0] + max(win[1] - win[0], 45)) % 1440 if win[1] - win[0] < 45 else win[1] % 1440)
   
    else:
        state["meals"][key] = (win[0] % 1440, win[1] % 1440 if win[1] - win[0] >= 20 else (win[0] + 45) % 1440)


_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def weekdays_in(label):
    low = (label or "").lower()
   
    if re.search(r"\bweek ?days?\b|\bmon(?:day)?\s*(?:-|to|through)\s*fri", low):
        return {0, 1, 2, 3, 4}
  
    if re.search(r"\bweek ?ends?\b", low):
        return {5, 6}
    found = {v for k, v in _DAY_NAMES.items() if re.search(rf"\b{k}[a-z]*\b", low)}
   
    return found or None


def merged(conn, user):
    uid = user["id"]
    state = {"sleep": None, "meals": {}, "workout": None, "peak": None, "seen": set(), "src": {}, "other": [], "dated": [],
             "soft": [], "dated_soft": []}
  
    urow = conn.execute("SELECT routine, blocked_windows FROM users WHERE id=?", (uid,)).fetchone()
    mine = _jl(urow["routine"] if urow and "routine" in urow.keys() else "", {})
   
    if mine.get("sleep"):
        _apply(state, "sleep", tuple(mine["sleep"]), "you told me")
   
    for k, v in (mine.get("meals") or {}).items():
        _apply(state, k, tuple(v), "you told me")
   
    if mine.get("workout"):
        _apply(state, "workout", tuple(mine["workout"]), "you told me")
   
    if mine.get("peak"):
        state["peak"] = mine["peak"]

    wake_guess = (state["sleep"] or DEFAULT_SLEEP)[1]
    labels = [(lab, "settings") for lab in _jl(urow["blocked_windows"] if urow else "[]", [])]
   
    for g in conn.execute("SELECT id, constraints, details FROM goals WHERE user_id=? AND status='active' ORDER BY id DESC", (uid,)).fetchall():
        d = _jl(g["details"], {})
        t = generation.parse_clock_list(d.get("sleep_schedule") or "")
       
        if len(t) >= 2:
            _apply(state, "sleep", (t[0], t[1]), f"goal #{g['id']}")
        mt = generation.parse_clock_list(d.get("meal_times") or "")
        
        for name, tm in zip(("breakfast", "lunch", "dinner"), mt):
            _apply(state, name, (tm, tm + 45), f"goal #{g['id']}")
        mv = generation.parse_clock_list(d.get("movement_time") or "")
       
        if mv and (d.get("movement_time") or "").strip().lower() not in ("none", "no", "n/a", "-", "skip", "0"):
            _apply(state, "workout", (mv[0], mv[0] + 60), f"goal #{g['id']}")
       
        if d.get("peak_energy") and not state["peak"]:
            state["peak"] = d["peak_energy"]
        labels += [(lab, f"goal #{g['id']}") for lab in _jl(g["constraints"], [])]
   
    for lab, src in labels:
        act = activity_of(lab)
        wake = (state["sleep"] or DEFAULT_SLEEP)[1]
        win = window_of(lab, wake)
       
        if win is None and act in ("workout", "breakfast", "lunch", "dinner"):
            t = generation.parse_clock_list(lab)
            if t:
                win = (t[0], t[0] + (60 if act == "workout" else 45))
      
        if win is None:
            continue
       
        if act:
            _apply(state, act, win, src)
       
        else:
            from core import triage
            said = (mine.get("window_kinds") or {}).get(norm_label(lab))     # what the person told us wins
            soft = (said or triage.window_kind(lab)) == "soft"               # movie night, gaming...: can give way to a fire
            days = weekdays_in(lab)
            
            if days:
                if (tuple(win), days) not in state["dated"]:
                    state["dated"].append((tuple(win), days))
               
                if soft and (tuple(win), days) not in state["dated_soft"]:
                    state["dated_soft"].append((tuple(win), days))
            
            elif tuple(win) not in state["other"]:
                state["other"].append(tuple(win))
                if soft:
                    state["soft"].append(tuple(win))
   
    assumed = []
   
    if state["sleep"] is None:
        state["sleep"] = DEFAULT_SLEEP
        assumed.append(f"you sleep {generation.fmt_min(DEFAULT_SLEEP[0])}-{generation.fmt_min(DEFAULT_SLEEP[1])}")
    missing = [n for n in ("breakfast", "lunch", "dinner") if n not in state["meals"]]
   
    for n in missing:
        state["meals"][n] = DEFAULT_MEALS[n]
   
    if missing:
        assumed.append("meals at " + ", ".join(f"{n} {generation.fmt_min(DEFAULT_MEALS[n][0])}" for n in missing))
    state["assumed"] = assumed
    return state


def human_windows(r):
    return [tuple(r["sleep"])] + [tuple(w) for w in r["meals"].values()]


def dated_busy(r, today, deadline, key="dated"):
    from datetime import date, timedelta
    out = {}
    
    try:
        d, end = date.fromisoformat(today), date.fromisoformat(deadline)
   
    except Exception:
        return out
    n = 0
   
    while d <= end and n < 400:
        for win, days in r.get(key, []):
            if d.weekday() in days:
                out.setdefault(d.isoformat(), []).append(tuple(win))
        d += timedelta(days=1)
        n += 1
    return out


def blocked_windows(r):
    out = list(r["other"])
    if r["workout"]:
        out.append(tuple(r["workout"]))
    
    return out


def describe(r):
    parts = [f"sleep {generation.fmt_min(r['sleep'][0])}-{generation.fmt_min(r['sleep'][1])}"]
    parts += [f"{n} {generation.fmt_min(w[0])}" for n, w in r["meals"].items()]
  
    if r["workout"]:
        parts.append(f"workout {generation.fmt_min(r['workout'][0])}-{generation.fmt_min(r['workout'][1])}")
   
    return ", ".join(parts)


DAY_MODES = ("normal", "light", "travel", "sick", "off")


def norm_label(label):
    return re.sub(r"\s+", " ", (label or "").strip().lower())


def _stored(conn, user_id):
    row = conn.execute("SELECT routine FROM users WHERE id=?", (user_id,)).fetchone()
    return _jl(row["routine"] if row else "", {})


def day_modes(conn, user_id):
    return {d: m for d, m in (_stored(conn, user_id).get("day_modes") or {}).items() if m in DAY_MODES}


def set_day_mode(conn, user_id, iso, mode, today=None):
    if mode not in DAY_MODES:
        raise ValueError("mode must be one of " + ", ".join(DAY_MODES))
    modes = day_modes(conn, user_id)
    
    if mode == "normal":
        modes.pop(iso, None)
    else:
        modes[iso] = mode
   
    if today:
        modes = {d: m for d, m in modes.items() if d >= today}
    save_override(conn, user_id, {"day_modes": modes})
  
    return modes


def window_kinds(conn, user_id):
    return {k: v for k, v in (_stored(conn, user_id).get("window_kinds") or {}).items() if v in ("soft", "hard")}


def set_window_kind(conn, user_id, label, kind):
    if kind not in ("soft", "hard"):
        raise ValueError("kind must be soft or hard")
    kinds = window_kinds(conn, user_id)
    kinds[norm_label(label)] = kind
    save_override(conn, user_id, {"window_kinds": kinds})
   
    return kinds


def window_labels(conn, user):
    uid = user["id"]
    urow = conn.execute("SELECT blocked_windows FROM users WHERE id=?", (uid,)).fetchone()
    labels = list(_jl(urow["blocked_windows"] if urow else "[]", []))
   
    for g in conn.execute("SELECT constraints FROM goals WHERE user_id=? AND status='active' ORDER BY id", (uid,)).fetchall():
        labels += _jl(g["constraints"], [])
    out, seen = [], set()
  
    for lab in labels:
        n = norm_label(lab)
        if not n or n in seen or activity_of(lab) or window_of(lab, 7 * 60) is None:
            continue
        seen.add(n)
        out.append(lab)
  
    return out


def save_override(conn, user_id, patch):
    row = conn.execute("SELECT routine FROM users WHERE id=?", (user_id,)).fetchone()
    cur = _jl(row["routine"] if row else "", {})
   
    for k, v in patch.items():
        if k == "meals":
            cur.setdefault("meals", {}).update(v)
        else:
            cur[k] = v
    conn.execute("UPDATE users SET routine=? WHERE id=?", (json.dumps(cur), user_id))
    conn.commit()
   
    return cur


_STATEMENT = re.compile(r"\b(i|my|we)\b|\b(sleep|wake|gym|workout|lunch|dinner|breakfast)\b", re.I)


def parse_statement(message):
    low = (message or "").lower()
  
    if low.rstrip().endswith("?") or not _STATEMENT.search(low):
        return {}
    patch = {}
    m = re.search(r"(?:sleep|bed|asleep)[^.;\n]*?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)[^.;\n]*?(?:wake|get up|up at|to|until|-)[^\d.;\n]*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", low)
   
    if m:
        t = generation.parse_clock_list(f"{m.group(1)} {m.group(2)}")
        if len(t) >= 2:
            patch["sleep"] = [t[0], t[1]]
   
    for name in ("breakfast", "lunch", "dinner"):
        m = re.search(rf"{name}[^.;\n]*?(?:at|around|by|~)\s*(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?)", low)
        if m:
            t = generation.parse_clock_list(m.group(1))
            if t:
                patch.setdefault("meals", {})[name] = [t[0], t[0] + 45]
    wm = re.search(r"\b(gym|workout|work out|exercise)\b", low)
   
    if wm:
        seg = re.split(r"[.;\n]|\band\b(?=[^.;\n]*\b(?:sleep|lunch|dinner|breakfast)\b)", low[wm.start():])[0]
        win = generation.parse_time_window(seg) if len(generation.parse_clock_list(seg)) >= 2 else None
        if win is None:
            t = generation.parse_clock_list(re.sub(r"[^a-z0-9: ]", " ", seg))
            win = (t[0], t[0] + 60) if t else None
      
        if win is None:
            win = _vague(seg, (patch.get("sleep") or DEFAULT_SLEEP)[1])
     
        if win:
            patch["workout"] = [win[0], win[1] if win[1] - win[0] >= 45 else win[0] + 60]
   
    return patch