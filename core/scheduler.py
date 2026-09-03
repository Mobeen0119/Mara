import json
import logging
from datetime import date, datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import generation
from core.database import get_connection

logger = logging.getLogger("eloise")


class EloiseScheduler:
    def __init__(self, db=None):
        self.db = db
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.scheduler.add_job(
            self.send_daily_digests, CronTrigger(hour=0, minute=0), id="daily_digest",
            replace_existing=True, max_instances=1, coalesce=True,
        )

    def start(self):
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def conn(self):
        return self.db or get_connection()

    # ------------------------------------------------------------------ digest
    def send_daily_digests(self):
        conn = self.conn()
        rows = conn.execute("SELECT * FROM users").fetchall()
        for user in rows:
            try:
                self._send_user_digest(conn, user)
            except Exception as exc:
                logger.warning("digest failed for user %s: %s", user["id"], exc)

    def _send_user_digest(self, conn, user):
        today = date.today().isoformat()
        goals = conn.execute(
            "SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
        tasks = []
        total = 0
        for goal in goals:
            acts = conn.execute(
                "SELECT * FROM actions WHERE goal_id=? AND date=?", (goal["id"], today)
            ).fetchall()
            for a in acts:
                tasks.append({
                    "date": a["date"], "title": a["title"], "start_time": a["start_time"],
                    "end_time": a["end_time"], "status": a["status"],
                })
                total += (a["duration_min"] or 60) / 60.0
        if not tasks:
            return
        try:
            from core.persona import build_daily_email_html
            html = build_daily_email_html(
                user["name"], tasks, round(total, 1), 0, goal_title=goals[0]["display_title"] if goals else None
            )
        except Exception:
            html = ""
        conn.execute(
            "INSERT INTO logs (user_id, kind, message) VALUES (?,?,?)",
            (user["id"], "daily_digest", f"digest ready: {len(tasks)} tasks, {round(total,1)}h"),
        )
        conn.commit()

    # ----------------------------------------------------------------- checkin
    def _is_checkin_due(self, conn, user):
        checkin_t = user["checkin_time"] or "08:00"
        try:
            hh, mm = int(checkin_t.split(":")[0]), int(checkin_t.split(":")[1])
        except Exception:
            hh, mm = 8, 0
        now = datetime.now()
        last = user["last_checkin_at"]
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            return True
        due_ref = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < due_ref:
            due_ref -= timedelta(days=1)
        return now - last_dt >= timedelta(hours=24) and now - due_ref >= timedelta(hours=24)


eloise_scheduler = EloiseScheduler()