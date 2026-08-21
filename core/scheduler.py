import json
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from core.database import get_connection, other_active_tasks
from core.persona import days_remaining, get_today_action, build_daily_email_html
from core.ollama_client import generate_message
from core.mailer import send_mail


def _plain_text_body(message, today_action):
    if today_action:
        return f"{message}\n\nToday's schedule:\n{today_action.replace(' | ', chr(10))}"
    return message


def _other_tasks_for_email(conn, user_id, exclude_task_id):
    tasks = other_active_tasks(conn, user_id, exclude_task_id)
    for t in tasks:
        t["days_left"] = days_remaining(t["deadline"])
    return tasks


def resolve_finished_tasks():
    now = datetime.now()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM tasks WHERE status = 'active'").fetchall()
    for row in rows:
        if days_remaining(row["deadline"]) < 0:
            checkin = conn.execute(
                "SELECT completed FROM checkins WHERE task_id = ? AND completed = 1 LIMIT 1",
                (row["id"],),
            ).fetchone()
            final_status = "succeeded" if checkin else "failed"
            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
                (final_status, now.isoformat(), row["id"]),
            )
    conn.commit()
    conn.close()


def send_reminders():
    now = datetime.now()
    current_hm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT tasks.*, users.name AS user_name, users.email AS user_email, users.verified AS user_verified
        FROM tasks JOIN users ON tasks.user_id = users.id
        WHERE tasks.status = 'active' AND tasks.reminder_time = ?
          AND (tasks.last_sent_date IS NULL OR tasks.last_sent_date != ?)
        """,
        (current_hm, today),
    ).fetchall()
    for row in rows:
        if not row["user_verified"]:
            conn.execute(
                "INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)",
                (row["id"], "SKIPPED: recipient email not verified yet", now.isoformat()),
            )
            continue
        constraints = json.loads(row["constraints"])
        days_left = days_remaining(row["deadline"])
        is_overdue = days_left < 0
        mara_message, _ = generate_message(
            row["user_name"], row["goal"], row["deadline"], constraints, days_left, is_overdue
        )
        today_action = get_today_action(row["plan_text"])
        plain_body = _plain_text_body(mara_message, today_action)
        html_body = build_daily_email_html(
            row["user_name"], row["goal"], row["deadline"], constraints, days_left, is_overdue,
            mara_message, today_action, _other_tasks_for_email(conn, row["user_id"], row["id"]),
        )
        try:
            send_mail(row["user_email"], "MARA: today's task", plain_body, html=html_body)
            conn.execute("UPDATE tasks SET last_sent_date = ? WHERE id = ?", (today, row["id"]))
            conn.execute(
                "INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)",
                (row["id"], plain_body[:500], now.isoformat()),
            )
        except Exception as exc:
            conn.execute(
                "INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)",
                (row["id"], f"SEND FAILED: {exc}", now.isoformat()),
            )
    conn.commit()
    conn.close()


def run_cycle():
    resolve_finished_tasks()
    send_reminders()


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_cycle, "interval", minutes=1, id="mara_daily_cycle")
    scheduler.start()
    return scheduler
