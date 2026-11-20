"""Background jobs. One tick every 5 minutes walks the users and does, in THEIR
local time (not the server's):

  * sweep   - pending blocks from earlier days become 'missed' (so every counter is honest)
  * reminder- at a goal's reminder_time, if it has blocks today, leave a plain factual note
  * checkin - at the user's checkin_time, once a day, mail a nudge if their address is confirmed
  * digest  - the day's schedule by email, once a day, at the check-in time
  * horizon - far-off goals only have the next ~2 weeks drawn hour-by-hour; when
              fewer than 7 days of pending tasks remain ahead, the next window is drawn

Every once-a-day action is recorded in job_runs(user, goal, kind, local day) with
INSERT OR IGNORE, so a restart, a second tick, or a slow tick can never send twice.
"""

import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.clock import now_for
from core.database import get_connection

logger = logging.getLogger("eloise")


def _hhmm(text, default):
    try:
        h, m = (text or default).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        h, m = default.split(":")
        return int(h) * 60 + int(m)


def _claim(conn, user_id, goal_id, kind, day):
    """True exactly once per (user, goal, kind, day)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO job_runs (user_id, goal_id, kind, day) VALUES (?,?,?,?)",
        (user_id, goal_id or 0, kind, day),
    )
    conn.commit()
    return cur.rowcount == 1


MAX_MAIL_TRIES = 6


def _mail_once(conn, user, goal_id, kind, day, send):
    """Mail at most once a day, but retry a failed send (up to MAX_MAIL_TRIES) instead of burning the day.
    send() returns True (sent), False (failed) or None (nothing to send right now)."""
    gid = goal_id or 0
    done = conn.execute(
        "SELECT 1 FROM job_runs WHERE user_id=? AND goal_id=? AND kind=? AND day=?",
        (user["id"], gid, kind, day),
    ).fetchone()
    tries = conn.execute(
        "SELECT COUNT(*) FROM job_runs WHERE user_id=? AND goal_id=? AND kind=? AND day LIKE ?",
        (user["id"], gid, kind + "_try", day + "#%"),
    ).fetchone()[0]
    if done or tries >= MAX_MAIL_TRIES:
        return
    result = send()
    if result is True:
        _claim(conn, user["id"], goal_id, kind, day)
    elif result is False:
        _claim(conn, user["id"], goal_id, kind + "_try", f"{day}#{tries}")


class EloiseScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.scheduler.add_job(
            self.tick,
            CronTrigger(minute="*/5"),
            id="tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )

    def start(self):
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    def tick(self, now_fn=None):
        conn = get_connection()
        for user in conn.execute("SELECT * FROM users").fetchall():
            try:
                self.run_user(conn, dict(user), (now_fn or now_for)(user))
            except Exception as exc:
                logger.warning("tick failed for user %s: %r", user["id"], exc)

    def run_user(self, conn, user, now):
        today = now.date().isoformat()
        now_min = now.hour * 60 + now.minute
        self._sweep(conn, user, today)
        self._extend_horizon(conn, user, today)
        goals = conn.execute(
            "SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
        if not goals:
            return
        for g in goals:
            if now_min >= _hhmm(g["reminder_time"], "09:00"):
                self._reminder(conn, user, g, today)
        if now_min >= _hhmm(user.get("checkin_time"), "08:00"):
            self._checkin_and_digest(conn, user, goals, today)
        self._bedtime_preview(conn, user, goals, now, today, now_min)

    # ------------------------------------------------------------------
    def _extend_horizon(self, conn, user, today):
        """Roll the detailed window forward for goals planned weekly/monthly."""
        from core import generation
        from datetime import date as _date

        for g in conn.execute(
            "SELECT id, deadline FROM goals WHERE user_id=? AND status='active' AND deadline>=?",
            (user["id"], today),
        ).fetchall():
            try:
                left = generation.days_remaining(g["deadline"], today)
                if generation.scope_for(left) == "daily":
                    continue  # drawn to the deadline in one go
                last = conn.execute(
                    "SELECT MAX(date) FROM actions WHERE goal_id=? AND status='pending'", (g["id"],)
                ).fetchone()[0]
                need_to = min(g["deadline"], (_date.fromisoformat(today) + timedelta(days=7)).isoformat())
                if (last is None or last < need_to) and _claim(conn, user["id"], g["id"], "horizon", today):
                    from core.routes.goal_routes import submit_plan

                    submit_plan(user, g["id"])
            except Exception as exc:
                logger.warning("horizon extend failed for goal %s: %r", g["id"], exc)

    def _sweep(self, conn, user, today):
        conn.execute(
            "UPDATE actions SET status='missed' WHERE status='pending' AND date<? AND user_id=? "
            "AND goal_id IN (SELECT id FROM goals WHERE status='active')",
            (today, user["id"]),
        )
        conn.commit()

    def _reminder(self, conn, user, goal, today):
        acts = conn.execute(
            "SELECT start_time FROM actions WHERE goal_id=? AND date=? AND status='pending' "
            "ORDER BY start_time",
            (goal["id"], today),
        ).fetchall()
        if not acts:
            return
        first = next((a["start_time"] for a in acts if a["start_time"]), None)
        msg = f"Today on '{goal['display_title']}': {len(acts)} block{'s' if len(acts) != 1 else ''}" + (
            f", first at {first}." if first else "."
        )
        if _claim(conn, user["id"], goal["id"], "reminder", today):
            conn.execute(
                "INSERT INTO interventions (user_id, goal_id, message, type) VALUES (?,?,?, 'reminder')",
                (user["id"], goal["id"], msg),
            )
            conn.commit()
        from core import emails

        _mail_once(
            conn,
            user,
            goal["id"],
            "reminder_mail",
            today,
            lambda: emails.send_reminder(conn, user, goal, msg, today),
        )

    def _bedtime_preview(self, conn, user, goals, now, today, now_min):
        """An hour before sleep, mail the schedule of the day that starts when they wake up."""
        if not (user.get("email") and user.get("verified")):
            return
        try:
            from core import emails, routine

            bed, wake = routine.merged(conn, user)["sleep"]
        except Exception as exc:
            logger.warning("bedtime preview skipped for user %s: %r", user["id"], exc)
            return
        send_at = (bed - 60) % 1440
        if (now_min - send_at) % 1440 >= 180 or now_min < send_at:
            return
        wakes_today = send_at < wake
        day = now.date() if wakes_today else now.date() + timedelta(days=1)
        label = "Today's plan" if wakes_today else "Tomorrow"
        _mail_once(
            conn,
            user,
            0,
            "tomorrow_mail",
            today,
            lambda: emails.send_tomorrow(conn, user, [dict(g) for g in goals], day.isoformat(), label),
        )

    def _checkin_and_digest(self, conn, user, goals, today):
        email_ok = bool(user.get("email")) and bool(user.get("verified"))
        if email_ok:
            _mail_once(
                conn, user, 0, "checkin_mail", today, lambda: self._mail_digest(conn, user, goals, today)
            )

    def _mail_digest(self, conn, user, goals, today):
        from core import emails

        return emails.send_daily(conn, user, [dict(g) for g in goals], today)


eloise_scheduler = EloiseScheduler()