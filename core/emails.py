import logging
import os
import re
from datetime import date as _date
from html import escape as _e

logger = logging.getLogger("eloise.emails")


_MEDICAL = re.compile(
    r"\b(doctor|dr\.?|clinic|hospital|appointment|medic\w*|pill|pills|tablet|dose|injection|insulin|therapy|"
    r"physio\w*|dialysis|dentist|check-?up|surgery|prescription|blood test|lab test|scan|vaccin\w*|"
    r"chemo\w*|checkup|pharmacy)\b",
    re.I,
)
_IMPORTANT = re.compile(
    r"\b(exam|interview|flight|court|visa|submission|viva|defen[cs]e|presentation|debate|payment|rent due|"
    r"deadline|wedding|funeral)\b",
    re.I,
)
_CONDITION = re.compile(
    r"\b(injur\w*|pain|asthma|diabet\w*|heart|blood pressure|hypertension|pregnan\w*|sprain\w*|fractur\w*|"
    r"surgery|knee|back problem|migraine|epilep\w*|allerg\w*|ill|sick|fever|recover\w*)\b",
    re.I,
)
_PHYSICAL = re.compile(
    r"\b(workout|gym|run|jog|lift|squat|deadlift|cardio|hiit|sprint|swim|cycl\w*|exercise|push-?ups?|sit-?ups?|abs)\b",
    re.I,
)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _applies(label, day):
    """A typed window applies on `day` unless it names a different date or weekday."""
    low = label.lower()
    iso = re.search(r"\d{4}-\d{2}-\d{2}", low)
    if iso:
        return iso.group(0) == day
    named = [i for i, w in enumerate(_WEEKDAYS) if w in low]
    if named:
        return _date.fromisoformat(day).weekday() in named
    return True


def day_notes(conn, user, day, groups):
    """What must not be missed on `day`: medical and other important windows, plus care notes for physical tasks."""
    uid = user["id"]
    labels = []
    try:
        urow = conn.execute("SELECT blocked_windows FROM users WHERE id=?", (uid,)).fetchone()
        labels += _jl_list(urow["blocked_windows"] if urow else "[]")
        for g in conn.execute(
            "SELECT constraints, title, details FROM goals WHERE user_id=? AND status='active'", (uid,)
        ):
            labels += _jl_list(g["constraints"])
    except Exception as exc:
        logger.warning("day notes skipped: %r", exc)
    notes, seen, conditions = [], set(), set()
    for lab in labels:
        hit = _CONDITION.search(lab)
        if hit:
            conditions.add(hit.group(0).lower())
        key = lab.strip().lower()
        if key in seen or not _applies(lab, day):
            continue
        kind = "medical" if _MEDICAL.search(lab) else ("important" if _IMPORTANT.search(lab) else "")
        if kind:
            seen.add(key)
            notes.append({"kind": kind, "text": lab.strip()})
    for _title, ts in groups:
        for t in ts:
            title = str(t.get("title", ""))
            if _MEDICAL.search(title) and title.lower() not in seen:
                seen.add(title.lower())
                notes.append({"kind": "medical", "text": f"{_span(t)}  {title}"})
            if conditions and _PHYSICAL.search(title):
                cond = ", ".join(sorted(conditions))
                notes.append(
                    {
                        "kind": "care",
                        "text": f"'{title[:60]}': you mentioned {cond}. Go easy, stop if it hurts, and ask your doctor if unsure.",
                    }
                )
    notes.sort(key=lambda n: {"medical": 0, "important": 1, "care": 2}[n["kind"]])
    return notes[:8]


def _jl_list(raw):
    import json

    try:
        out = json.loads(raw or "[]")
        return [str(x) for x in out] if isinstance(out, list) else []
    except Exception:
        return []


def _span(t):
    s, e = t.get("start_time"), t.get("end_time")
    return f"{s}-{e}" if s and e else "anytime"


def _minutes(t):
    try:
        h1, m1 = t["start_time"].split(":")
        h2, m2 = t["end_time"].split(":")
        return max(0, int(h2) * 60 + int(m2) - int(h1) * 60 - int(m1))
    except Exception:
        return 0


def _length(mins):
    if mins <= 0:
        return ""
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m" if h and m else (f"{h}h" if h else f"{m}m")


def _app_url():
    return (os.environ.get("APP_URL") or os.environ.get("PUBLIC_URL") or "").strip().rstrip("/")


_CSS = (
    "<style>"
    "@media (prefers-color-scheme: dark){"
    ".bg{background:#17120d!important}.card{background:#221a13!important;border-color:#3a2d20!important}"
    ".ink{color:#f1e6d6!important}.muted{color:#b9a78f!important}.line{border-color:#3a2d20!important}"
    ".pill{background:#3a2d20!important;color:#f1e6d6!important}}"
    "@media (max-width:480px){.pad{padding:20px 18px!important}"
    ".cell{padding:7px 6px!important;font-size:13px!important}}"
    "</style>"
)


_TH = (
    "padding:8px 10px;background:#2b2118;color:#f4ece0;font:700 12px Helvetica,Arial,sans-serif;"
    "text-align:left;letter-spacing:.04em;text-transform:uppercase;border:1px solid #2b2118"
)
_NOTE_STYLE = {
    "medical": ("#fde8e6", "#b3261e", "MEDICAL - don't miss"),
    "important": ("#fff1d6", "#9a5b00", "IMPORTANT"),
    "care": ("#e6f0fb", "#1f5fa6", "TAKE CARE"),
}


def _notes_table(notes):
    if not notes:
        return "", []
    rows, md = [], ["## Needs your attention", "", "| Type | Detail |", "| --- | --- |"]
    for n in notes:
        bg, fg, label = _NOTE_STYLE[n["kind"]]
        rows.append(
            f"<tr><td style='padding:9px 10px;border:1px solid {fg};background:{bg};color:{fg};white-space:nowrap;"
            f"font:700 12px Helvetica,Arial,sans-serif;vertical-align:top'>{label}</td>"
            f"<td style='padding:9px 10px;border:1px solid {fg};background:{bg};color:#2b2118;"
            f"font:14px/1.45 Helvetica,Arial,sans-serif'>{_e(n['text'])}</td></tr>"
        )
        md.append(f"| {label} | {n['text']} |")
    table = (
        "<p style='margin:20px 0 8px;font:700 13px Helvetica,Arial,sans-serif;color:#b3261e;"
        "letter-spacing:.06em;text-transform:uppercase'>Needs your attention</p>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        f"style='border-collapse:collapse'>{''.join(rows)}</table>"
    )
    return table, md + [""]


def render_day(
    name, day_label, groups, assumed=None, headline=None, greeting=None, preheader=None, notes=None
):
    """groups: [(goal_title, [task dicts])]. Returns (subject, text, html). Tasks are shown as tables."""
    tasks = [t for _, ts in groups for t in ts]
    total = len(tasks)
    mins = sum(_minutes(t) for t in tasks)
    title = headline or day_label
    subject = f"Eloise - {title}: {total} block{'s' if total != 1 else ''}"
    hello = greeting or f"Hi {name}, here is what is drawn out."
    summary = f"{total} block{'s' if total != 1 else ''}" + (
        f" - {_length(mins)} of focused time" if mins else ""
    )
    notes_html, notes_md = _notes_table(notes)
    md = [f"# {title}", "", hello, summary, ""] + notes_md
    blocks = []
    for gtitle, ts in groups:
        md += [f"## {gtitle}", "", "| Time | Task | Length | Status |", "| --- | --- | --- | --- |"]
        rows = []
        for n, t in enumerate(ts):
            done = t.get("status") == "done"
            length = _length(_minutes(t)) or "-"
            md.append(f"| {_span(t)} | {t.get('title', '')} | {length} | {'Done' if done else 'To do'} |")
            bg = "#fbf4e8" if n % 2 else "#fffaf2"
            cell = f"padding:9px 10px;border:1px solid #e0d2bc;background:{bg};vertical-align:top;"
            strike = "text-decoration:line-through;color:#8a7a64;" if done else "color:#2b2118;"
            status = (
                "<span style='color:#2e7d32;font-weight:700'>Done</span>"
                if done
                else "<span style='color:#8a5a22;font-weight:700'>To do</span>"
            )
            rows.append(
                f"<tr><td class='cell' style='{cell}white-space:nowrap;font:600 13px Helvetica,Arial,sans-serif;{strike}'>"
                f"{_e(_span(t))}</td>"
                f"<td class='cell' style='{cell}font:14px/1.5 Georgia,serif;{strike}'>{_e(str(t.get('title', '')))}</td>"
                f"<td class='cell' style='{cell}white-space:nowrap;font:13px Helvetica,Arial,sans-serif;color:#6b5b45'>"
                f"{_e(length)}</td>"
                f"<td class='cell' style='{cell}white-space:nowrap;font:13px Helvetica,Arial,sans-serif'>{status}</td></tr>"
            )
        md.append("")
        blocks.append(
            f"<p style='margin:24px 0 8px;font:700 14px Georgia,serif;color:#2b2118;border-left:4px solid #d9a566;"
            f"padding-left:10px'>{_e(gtitle)}</p>"
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse'>"
            f"<tr><th style='{_TH}'>Time</th><th style='{_TH}'>Task</th><th style='{_TH}'>Length</th>"
            f"<th style='{_TH}'>Status</th></tr>{''.join(rows)}</table>"
        )
    if assumed:
        md += ["I assumed: " + "; ".join(assumed) + ". Tell me in chat to fix it.", ""]
    md.append("Mark blocks done in the app. No 'almost'.")
    note = (
        (
            "<p class='muted' style='margin:20px 0 0;font:13px/1.5 Helvetica,Arial,sans-serif;color:#7a6a55'>"
            f"I assumed: {_e('; '.join(assumed))}. Tell me in chat to fix it.</p>"
        )
        if assumed
        else ""
    )
    url = _app_url()
    button = (
        f"<p style='margin:26px 0 0'><a href='{_e(url)}' style='display:inline-block;background:#2b2118;color:#f4ece0;"
        "text-decoration:none;border-radius:10px;padding:12px 22px;font:600 14px Helvetica,Arial,sans-serif'>"
        "Open Eloise</a></p>"
        if url
        else ""
    )
    pre = _e(preheader or summary)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<meta name='color-scheme' content='light dark'>{_CSS}</head>"
        "<body class='bg' style='margin:0;background:#f4ece0;padding:0'>"
        f"<div style='display:none;max-height:0;overflow:hidden;opacity:0'>{pre}</div>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' class='bg' style='background:#f4ece0'>"
        "<tr><td align='center' style='padding:24px 12px'>"
        "<table role='presentation' width='600' cellpadding='0' cellspacing='0' class='card' style='width:100%;"
        "max-width:600px;background:#fffaf2;border-radius:16px;overflow:hidden;border:1px solid #eadfcf'>"
        "<tr><td style='background:#2b2118;padding:24px 28px'>"
        "<div style='font:700 22px Georgia,serif;color:#f4ece0'>Eloise</div>"
        f"<div style='font:600 14px Helvetica,Arial,sans-serif;color:#d9a566;margin-top:4px'>{_e(title)}</div></td></tr>"
        "<tr><td class='pad' style='padding:26px 28px 30px'>"
        f"<p class='ink' style='margin:0 0 6px;font:18px/1.5 Georgia,serif;color:#2b2118'>{_e(hello)}</p>"
        f"<p class='muted' style='margin:0;font:600 13px Helvetica,Arial,sans-serif;color:#8a7a64'>{_e(summary)}</p>"
        f"{notes_html}{''.join(blocks)}{note}{button}"
        "<p class='muted' style='margin:24px 0 0;font:13px Helvetica,Arial,sans-serif;color:#7a6a55'>"
        "Mark blocks done in the app. No 'almost'.</p>"
        "</td></tr></table></td></tr></table></body></html>"
    )
    return subject, "\n".join(md), html


def _tasks(conn, goal_id, day):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT title,start_time,end_time,status FROM actions WHERE goal_id=? AND date=? ORDER BY start_time",
            (goal_id, day),
        ).fetchall()
    ]


def _deliverable(user):
    from core.mailer import smtp_configured

    return bool(user.get("email")) and bool(user.get("verified")) and smtp_configured()


def _assumed(conn, user):
    try:
        from core import routine

        return routine.merged(conn, user).get("assumed", [])
    except Exception:
        return []


def _send(conn, user, kind, subject, text, html):
    from core.mailer import send_email_detailed

    try:
        ok, err = send_email_detailed(user["email"], subject, text, html)
    except Exception as exc:
        ok, err = False, f"{type(exc).__name__}: {exc}"
    conn.execute(
        "INSERT INTO logs (user_id, kind, message) VALUES (?,?,?)",
        (user["id"], kind if ok else kind + "_failed", subject + ("" if ok else f" ({err})")),
    )
    conn.commit()
    return ok


def send_daily(conn, user, goals, today):
    if not _deliverable(user):
        return None
    groups = [(g["display_title"], t) for g in goals if (t := _tasks(conn, g["id"], today))]
    if not groups:
        return None
    subject, text, html = render_day(
        user["name"],
        today,
        groups,
        _assumed(conn, user),
        headline="Your day",
        notes=day_notes(conn, user, today, groups),
    )
    return _send(conn, user, "daily_digest", subject, text, html)


def send_reminder(conn, user, goal, message, today):
    """The goal's own reminder, by mail at its reminder time. None = nothing to send."""
    if not _deliverable(user):
        return None
    ts = _tasks(conn, goal["id"], today)
    if not ts:
        return None
    subject, text, html = render_day(
        user["name"],
        today,
        [(goal["display_title"], ts)],
        headline=f"Reminder: {goal['display_title']}",
        greeting=message,
        notes=day_notes(conn, user, today, [(goal["display_title"], ts)]),
    )
    return _send(conn, user, "reminder_mail", subject, text, html)


def send_tomorrow(conn, user, goals, day, label="Tomorrow"):
    """The schedule for the next day, an hour before bed. None = nothing planned or no mail set up."""
    if not _deliverable(user):
        return None
    groups = [(g["display_title"], t) for g in goals if (t := _tasks(conn, g["id"], day))]
    if not groups:
        return None
    subject, text, html = render_day(
        user["name"],
        day,
        groups,
        _assumed(conn, user),
        headline=f"{label}: {day}",
        notes=day_notes(conn, user, day, groups),
        greeting=f"Hi {user['name']}, here is {label.lower()} before you wind down.",
        preheader=f"{label}'s plan is ready. Sleep well.",
    )
    return _send(conn, user, "tomorrow_mail", subject, text, html)


def send_first_day(conn, user, goal):
    from core.scheduler import _claim

    if not _deliverable(user) or not _claim(conn, user["id"], goal["id"], "first_mail", "once"):
        return False
    row = conn.execute(
        "SELECT MIN(date) d FROM actions WHERE goal_id=? AND status='pending'", (goal["id"],)
    ).fetchone()
    if not row or not row["d"]:
        conn.execute("DELETE FROM job_runs WHERE goal_id=? AND kind='first_mail'", (goal["id"],))
        conn.commit()
        return False
    subject, text, html = render_day(
        user["name"],
        row["d"],
        [(goal["display_title"], _tasks(conn, goal["id"], row["d"]))],
        _assumed(conn, user),
        headline=f"Day one: {goal['display_title']}",
        notes=day_notes(conn, user, row["d"], [(goal["display_title"], _tasks(conn, goal["id"], row["d"]))]),
    )
    return _send(conn, user, "first_day_mail", subject, text, html)