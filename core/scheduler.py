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


class EloiseScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.scheduler.add_job(
            self.tick, CronTrigger(minute="*/5"), id="tick",
            replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=120,
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
                    continue                  # drawn to the deadline in one go
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
            "ORDER BY start_time", (goal["id"], today),
        ).fetchall()
        if not acts or not _claim(conn, user["id"], goal["id"], "reminder", today):
            return
        first = next((a["start_time"] for a in acts if a["start_time"]), None)
        msg = f"Today on '{goal['display_title']}': {len(acts)} block{'s' if len(acts) != 1 else ''}" \
              + (f", first at {first}." if first else ".")
        conn.execute(
            "INSERT INTO interventions (user_id, goal_id, message, type) VALUES (?,?,?, 'reminder')",
            (user["id"], goal["id"], msg),
        )
        conn.commit()

    def _checkin_and_digest(self, conn, user, goals, today):
        email_ok = bool(user.get("email")) and bool(user.get("verified"))
        if email_ok and _claim(conn, user["id"], 0, "checkin_mail", today):
            self._mail_digest(conn, user, goals, today)

    def _mail_digest(self, conn, user, goals, today):
        tasks, total = [], 0.0
        for goal in goals:
            for a in conn.execute("SELECT * FROM actions WHERE goal_id=? AND date=? ORDER BY start_time",
                                  (goal["id"], today)).fetchall():
                tasks.append({"date": a["date"], "title": a["title"], "start_time": a["start_time"],
                              "end_time": a["end_time"], "status": a["status"]})
                total += (a["duration_min"] or 60) / 60.0
        if not tasks:
            return
        try:
            from core.mailer import send_email_detailed, smtp_configured
            from core.persona import build_daily_email_html
            if not smtp_configured():
                return
            html = build_daily_email_html(user["name"], tasks, round(total, 1), 0,
                                          goal_title=goals[0]["display_title"] if len(goals) == 1 else None)
            ok, err = send_email_detailed(user["email"], "Eloise - your day", "See your schedule in the app.", html)
        except Exception as exc:
            ok, err = False, f"{type(exc).__name__}: {exc}"
        conn.execute(
            "INSERT INTO logs (user_id, kind, message) VALUES (?,?,?)",
            (user["id"], "daily_digest" if ok else "daily_digest_failed",
             f"digest: {len(tasks)} tasks, {round(total, 1)}h" + ("" if ok else f" (mail failed: {err})")),
        )
        conn.commit()


eloise_scheduler = EloiseScheduler()
