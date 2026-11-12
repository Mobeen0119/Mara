import logging
from html import escape as _e

logger = logging.getLogger("eloise.emails")


def _span(t):
    s, e = t.get("start_time"), t.get("end_time")
    return f"{s}-{e}" if s and e else "anytime"


def render_day(name, day_label, groups, assumed=None, headline=None):
    """groups: [(goal_title, [task dicts])]. Returns (subject, markdown_text, html)."""
    total = sum(len(ts) for _, ts in groups)
    subject = f"Eloise - {headline or day_label}: {total} block{'s' if total != 1 else ''}"
    md = [f"# {headline or day_label}", "", f"Morning, {name}. Here is what is drawn out.", ""]
    rows = []
    for title, ts in groups:
        md += [f"## {title}", ""]
        rows.append(f"<tr><td colspan='2' style='padding:18px 0 6px;font:600 13px/1.2 Helvetica,Arial,sans-serif;"
                    f"letter-spacing:.08em;text-transform:uppercase;color:#b4783a'>{_e(title)}</td></tr>")
        for t in ts:
            mark = "x" if t.get("status") == "done" else " "
            md.append(f"- [{mark}] **{_span(t)}** {t.get('title', '')}")
            rows.append(
                "<tr><td style='padding:10px 14px 10px 0;white-space:nowrap;vertical-align:top;font:600 14px Helvetica,Arial,sans-serif;"
                f"color:#2b2118'>{_e(_span(t))}</td><td style='padding:10px 0;border-bottom:1px solid #eadfcf;"
                f"font:15px/1.45 Georgia,serif;color:#2b2118'>{_e(str(t.get('title', '')))}</td></tr>")
        md.append("")
    if assumed:
        md += ["_I assumed: " + "; ".join(assumed) + ". Tell me in chat to fix it._", ""]
    md.append("Mark blocks done in the app. No 'almost'.")
    note = (f"<p style='margin:20px 0 0;font:13px/1.5 Helvetica,Arial,sans-serif;color:#7a6a55'>I assumed: "
            f"{_e('; '.join(assumed))}. Tell me in chat to fix it.</p>") if assumed else ""
    html = (
        "<!doctype html><html><body style='margin:0;background:#f4ece0;padding:24px 12px'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0'><tr><td align='center'>"
        "<table role='presentation' width='560' cellpadding='0' cellspacing='0' style='max-width:560px;background:#fffaf2;"
        "border-radius:16px;overflow:hidden;border:1px solid #eadfcf'>"
        "<tr><td style='background:#2b2118;padding:22px 28px'>"
        "<div style='font:700 22px Georgia,serif;color:#f4ece0'>Eloise</div>"
        f"<div style='font:13px Helvetica,Arial,sans-serif;color:#d9a566;margin-top:2px'>{_e(headline or day_label)}</div></td></tr>"
        "<tr><td style='padding:24px 28px 28px'>"
        f"<p style='margin:0 0 4px;font:17px/1.5 Georgia,serif;color:#2b2118'>Morning, {_e(name)}. Here is what is drawn out.</p>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{''.join(rows)}</table>{note}"
        "<p style='margin:22px 0 0;font:13px Helvetica,Arial,sans-serif;color:#7a6a55'>Mark blocks done in the app. No 'almost'.</p>"
        "</td></tr></table></td></tr></table></body></html>"
    )
    return subject, "\n".join(md), html


def _tasks(conn, goal_id, day):
    return [dict(r) for r in conn.execute(
        "SELECT title,start_time,end_time,status FROM actions WHERE goal_id=? AND date=? ORDER BY start_time",
        (goal_id, day)).fetchall()]


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
    conn.execute("INSERT INTO logs (user_id, kind, message) VALUES (?,?,?)",
                 (user["id"], kind if ok else kind + "_failed", subject + ("" if ok else f" ({err})")))
    conn.commit()
    return ok


def send_daily(conn, user, goals, today):
    if not _deliverable(user):
        return False
    groups = [(g["display_title"], t) for g in goals if (t := _tasks(conn, g["id"], today))]
    if not groups:
        return False
    subject, text, html = render_day(user["name"], today, groups, _assumed(conn, user), headline="Your day")
    return _send(conn, user, "daily_digest", subject, text, html)


def send_first_day(conn, user, goal):
    from core.scheduler import _claim
    if not _deliverable(user) or not _claim(conn, user["id"], goal["id"], "first_mail", "once"):
        return False
    row = conn.execute("SELECT MIN(date) d FROM actions WHERE goal_id=? AND status='pending'", (goal["id"],)).fetchone()
    if not row or not row["d"]:
        conn.execute("DELETE FROM job_runs WHERE goal_id=? AND kind='first_mail'", (goal["id"],))
        conn.commit()
        return False
    subject, text, html = render_day(
        user["name"], row["d"], [(goal["display_title"], _tasks(conn, goal["id"], row["d"]))],
        _assumed(conn, user), headline=f"Day one: {goal['display_title']}")
    return _send(conn, user, "first_day_mail", subject, text, html)