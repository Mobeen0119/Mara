import json
import os
import uuid
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from core.database import init_db, get_connection, other_active_tasks
from core.auth import new_token, hash_password, verify_password
from core.models import SignupRequest, GuestRequest, LoginRequest, TaskCreateRequest, ConstraintUpdateRequest, CheckinRequest, ChatRequest, NoteRequest, CancelReasonRequest
from core.scheduler import start_scheduler
from core.persona import days_remaining, parse_plan_entries, get_today_action, action_confirmation_email, build_daily_email_html, other_tasks_windows_by_date
from core.ollama_client import generate_chat_reply, ollama_status, generate_global_chat_reply, generate_plan, generate_opening_message, generate_nudge, generate_message, generate_cancel_roast
from core.mailer import SMTP_USER, SMTP_PASS, send_mail

_plan_locks = {}
_plan_locks_guard = threading.Lock()


def _user_plan_lock(user_id):
    with _plan_locks_guard:
        if user_id not in _plan_locks:
            _plan_locks[user_id] = threading.Lock()
        return _plan_locks[user_id]


app = FastAPI(title="Mara")

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "storage", "uploads")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    start_scheduler()


def _branded_page(headline, body_line, accent="#c41e3a", status_code=200):
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARA</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #0a0908; color: #efe9e2; font-family: 'Inter', sans-serif;
  min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 32px;
}}
.card {{
  max-width: 460px; width: 100%; background: #131110; border: 1px solid #2a241e;
  border-top: 3px solid {accent}; border-radius: 12px; padding: 40px 32px; text-align: center;
}}
.brand {{ font-size: 13px; font-weight: 800; letter-spacing: 4px; color: {accent}; text-transform: uppercase; margin-bottom: 24px; }}
h2 {{ font-size: 20px; font-weight: 600; line-height: 1.4; margin-bottom: 12px; }}
p {{ font-size: 13px; color: #9c948a; line-height: 1.6; }}
</style></head>
<body><div class="card"><div class="brand">Mara</div><h2>{headline}</h2><p>{body_line}</p></div></body></html>"""
    return HTMLResponse(html, status_code=status_code)


def get_user_by_token(token):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    conn.close()
    return row


def require_user(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization.split(" ", 1)[1]
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="invalid token")
    return user


@app.get("/api/status")
def status():
    llm_status = ollama_status()
    return {
        "smtp_configured": bool(SMTP_USER and SMTP_PASS),
        "ollama_reachable": llm_status["any_reachable"],
        "local_reachable": llm_status["local_reachable"],
        "local_model_available": llm_status["local_model_available"],
        "ollama_model": llm_status["ollama_model"],
        "cloud_configured": llm_status["cloud_configured"],
        "openrouter_model": llm_status["openrouter_model"],
        "ollama_error": llm_status["ollama_error"],
        "openrouter_error": llm_status["openrouter_error"],
    }


def _send_verification_email(request: Request, email, name, verify_token):
    base = str(request.base_url).rstrip("/")
    link = f"{base}/api/verify?token={verify_token}"
    body = (
        f"{name}, confirm this is your real email address before Mara can send you anything:\n\n"
        f"{link}\n\n"
        f"Until you click this, no daily messages go out to this address. "
        f"If you didn't ask for this, ignore it — nothing else happens."
    )
    try:
        send_mail(email, "Confirm your email for Mara", body)
    except Exception:
        pass


@app.post("/api/signup")
def signup(body: SignupRequest, request: Request):
    conn = get_connection()
    existing = conn.execute("SELECT id FROM users WHERE email = ? AND password_hash IS NOT NULL", (body.email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="email already registered")
    token = new_token()
    verify_token = new_token()
    conn.execute(
        "INSERT INTO users (name, email, password_hash, token, verified, verify_token, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
        (body.name, body.email, hash_password(body.password), token, verify_token, datetime.now().isoformat()),
    )
    conn.commit()
    user_id = conn.execute("SELECT id FROM users WHERE token = ?", (token,)).fetchone()["id"]
    conn.close()
    _send_verification_email(request, body.email, body.name, verify_token)
    return {"token": token, "user_id": user_id, "name": body.name, "verified": False}


@app.post("/api/guest")
def guest(body: GuestRequest, request: Request):
    conn = get_connection()
    token = new_token()
    verify_token = new_token()
    conn.execute(
        "INSERT INTO users (name, email, password_hash, token, verified, verify_token, created_at) VALUES (?, ?, NULL, ?, 0, ?, ?)",
        (body.name, body.email, token, verify_token, datetime.now().isoformat()),
    )
    conn.commit()
    user_id = conn.execute("SELECT id FROM users WHERE token = ?", (token,)).fetchone()["id"]
    conn.close()
    _send_verification_email(request, body.email, body.name, verify_token)
    return {"token": token, "user_id": user_id, "name": body.name, "verified": False}


@app.post("/api/login")
def login(body: LoginRequest):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? AND password_hash IS NOT NULL", (body.email,)
    ).fetchone()
    conn.close()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": row["token"], "user_id": row["id"], "name": row["name"], "verified": bool(row["verified"])}


@app.get("/api/verify")
def verify_email(token: str):
    conn = get_connection()
    row = conn.execute("SELECT id, name FROM users WHERE verify_token = ?", (token,)).fetchone()
    if not row:
        conn.close()
        return _branded_page(
            "That link's dead.",
            "Invalid or already used. If you need a new one, hit resend from the app.",
            status_code=400,
        )
    conn.execute("UPDATE users SET verified = 1, verify_token = NULL WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return _branded_page(
        f"Email confirmed, {row['name']}.",
        "Now I can actually reach you. Close this tab — go back and file something.",
        accent="#6b7a5e",
    )


@app.get("/api/me")
def get_me(authorization: str = Header(None)):
    user = require_user(authorization)
    return {"name": user["name"], "email": user["email"], "verified": bool(user["verified"])}


@app.post("/api/verify/resend")
def resend_verification(request: Request, authorization: str = Header(None)):
    user = require_user(authorization)
    if user["verified"]:
        return {"ok": True, "already_verified": True}
    conn = get_connection()
    verify_token = new_token()
    conn.execute("UPDATE users SET verify_token = ? WHERE id = ?", (verify_token, user["id"]))
    conn.commit()
    conn.close()
    _send_verification_email(request, user["email"], user["name"], verify_token)
    return {"ok": True, "already_verified": False}


def build_task_context(conn, task_id):
    chat_rows = conn.execute(
        "SELECT sender, message FROM chat_messages WHERE task_id = ? ORDER BY created_at ASC LIMIT 20",
        (task_id,),
    ).fetchall()
    chat_context = "\n".join(
        f"{'User' if r['sender'] == 'user' else 'Mara'}: {r['message']}" for r in chat_rows
    ) or None
    note_rows = conn.execute(
        "SELECT kind, title, content FROM attachments WHERE task_id = ? AND kind IN ('note','link','file') ORDER BY created_at DESC LIMIT 20",
        (task_id,),
    ).fetchall()
    notes_context = "\n".join(
        f"{r['title']}: {r['content']}" if r["content"] else f"{r['title']} (uploaded file)" for r in note_rows
    ) or None
    return chat_context, notes_context


def _global_chat_context(conn, user_id, limit=20):
    rows = conn.execute(
        "SELECT sender, message FROM general_chat WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    rows = list(reversed(rows))
    return "\n".join(
        f"{'User' if r['sender'] == 'user' else 'Mara'}: {r['message']}" for r in rows
    ) or None


def _mark_plan_status(task_id, status, plan_text=None):
    conn = get_connection()
    if plan_text is not None:
        conn.execute("UPDATE tasks SET plan_text = ?, plan_status = ? WHERE id = ?", (plan_text, status, task_id))
    else:
        conn.execute("UPDATE tasks SET plan_status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    conn.close()


def _queue_regen(background_tasks, task_id, user_name, goal, deadline, constraints, reminder_time):
    conn = get_connection()
    conn.execute("UPDATE tasks SET plan_status = 'generating' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    background_tasks.add_task(_regenerate_plan_bg, task_id, user_name, goal, deadline, constraints, reminder_time)


def _queue_regen_for_other_active_tasks(background_tasks, user_id, user_name, exclude_task_id=None):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, goal, deadline, constraints, reminder_time FROM tasks WHERE user_id = ? AND status = 'active' AND id != ?",
        (user_id, exclude_task_id or -1),
    ).fetchall()
    conn.close()
    for r in rows:
        _queue_regen(background_tasks, r["id"], user_name, r["goal"], r["deadline"], json.loads(r["constraints"]), r["reminder_time"])


def _send_task_email(task_id, subject, message):
    conn = get_connection()
    row = conn.execute(
        """
        SELECT tasks.*, users.email AS user_email, users.verified AS user_verified,
               users.name AS user_name
        FROM tasks JOIN users ON tasks.user_id = users.id
        WHERE tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"sent": False, "error": "task not found"}
    now = datetime.now().isoformat()
    if not row["user_verified"]:
        conn.execute(
            "INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)",
            (task_id, "SKIPPED: recipient email not verified yet", now),
        )
        conn.commit()
        conn.close()
        return {"sent": False, "error": "email not verified yet — check your inbox for the confirmation link"}
    try:
        send_mail(row["user_email"], subject, message)
        conn.execute("UPDATE tasks SET last_sent_date = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d"), task_id))
        conn.execute("INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)", (task_id, message, now))
        conn.commit()
        conn.close()
        return {"sent": True, "error": None}
    except Exception as exc:
        conn.execute("INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)", (task_id, f"SEND FAILED: {exc}", now))
        conn.commit()
        conn.close()
        return {"sent": False, "error": str(exc)}


def _send_task_email_html(task_id, subject, plain_body, html_body):
    conn = get_connection()
    row = conn.execute(
        """
        SELECT tasks.*, users.email AS user_email, users.verified AS user_verified,
               users.name AS user_name
        FROM tasks JOIN users ON tasks.user_id = users.id
        WHERE tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"sent": False, "error": "task not found"}
    now = datetime.now().isoformat()
    if not row["user_verified"]:
        conn.execute(
            "INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)",
            (task_id, "SKIPPED: recipient email not verified yet", now),
        )
        conn.commit()
        conn.close()
        return {"sent": False, "error": "email not verified yet"}
    try:
        send_mail(row["user_email"], subject, plain_body, html=html_body)
        conn.execute("UPDATE tasks SET last_sent_date = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d"), task_id))
        conn.execute("INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)", (task_id, plain_body[:500], now))
        conn.commit()
        conn.close()
        return {"sent": True, "error": None}
    except Exception as exc:
        conn.execute("INSERT INTO logs (task_id, message, sent_at) VALUES (?, ?, ?)", (task_id, f"SEND FAILED: {exc}", now))
        conn.commit()
        conn.close()
        return {"sent": False, "error": str(exc)}


def _build_all_tasks_context(conn, user_id, exclude_task_id=None):
    rows = conn.execute(
        "SELECT id, goal, deadline, plan_text, reminder_time FROM tasks WHERE user_id = ? AND status = 'active' AND id != ?",
        (user_id, exclude_task_id or -1),
    ).fetchall()
    parts = []
    for r in rows:
        today_entries = [e for e in parse_plan_entries(r["plan_text"]) if e["date"] == datetime.now().strftime("%Y-%m-%d")] if r["plan_text"] else []
        if today_entries:
            parts.append(f"- {r['goal']}: {today_entries[0]['action']}")
        else:
            parts.append(f"- {r['goal']} (due {r['deadline']}, reminder @ {r['reminder_time']})")
    return "\n".join(parts) if parts else None


def _other_task_windows(conn, user_id, exclude_task_id=None):
    rows = conn.execute(
        "SELECT plan_text FROM tasks WHERE user_id = ? AND status = 'active' AND id != ? AND plan_text IS NOT NULL",
        (user_id, exclude_task_id or -1),
    ).fetchall()
    return other_tasks_windows_by_date([r["plan_text"] for r in rows])


def _generate_plan_and_send_first(task_id, user_name, goal, deadline, constraints, reminder_time):
    days_left = days_remaining(deadline)
    is_overdue = days_left < 0
    conn = get_connection()
    user_id = conn.execute("SELECT user_id FROM tasks WHERE id = ?", (task_id,)).fetchone()["user_id"]
    conn.close()
    with _user_plan_lock(user_id):
        conn = get_connection()
        all_tasks_ctx = _build_all_tasks_context(conn, user_id, exclude_task_id=task_id)
        other_windows = _other_task_windows(conn, user_id, exclude_task_id=task_id)
        global_chat_ctx = _global_chat_context(conn, user_id)
        conn.close()
        plan_text, _ = generate_plan(
            user_name, goal, deadline, constraints, days_left, reminder_time,
            None, None, all_tasks_ctx, global_chat_ctx, other_windows,
        )
        _mark_plan_status(task_id, "ready", plan_text)
    mara_message, _ = generate_message(user_name, goal, deadline, constraints, days_left, is_overdue, is_first=True)
    today_action = get_today_action(plan_text)
    conn = get_connection()
    other_tasks = other_active_tasks(conn, user_id, exclude_task_id=task_id)
    conn.close()
    for t in other_tasks:
        t["days_left"] = days_remaining(t["deadline"])
    html_body = build_daily_email_html(
        user_name, goal, deadline, constraints, days_left, is_overdue, mara_message, today_action, other_tasks
    )
    plain_body = mara_message
    if today_action:
        plain_body += "\n\nToday's schedule:\n" + today_action.replace(" | ", "\n")
    _send_task_email_html(task_id, "MARA: your schedule is up", plain_body, html_body)


@app.post("/api/tasks")
def create_task(body: TaskCreateRequest, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO tasks (user_id, goal, deadline, reminder_time, constraints, status, plan_status, created_at)
        VALUES (?, ?, ?, ?, ?, 'active', 'generating', ?)
        """,
        (user["id"], body.goal, body.deadline, body.reminder_time, json.dumps(body.constraints), datetime.now().isoformat()),
    )
    conn.commit()
    task_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    background_tasks.add_task(
        _generate_plan_and_send_first, task_id, user["name"], body.goal, body.deadline, body.constraints, body.reminder_time
    )
    return {"id": task_id, "async": True}


@app.get("/api/tasks")
def list_tasks(authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()
    result = []
    for row in rows:
        latest_log = conn.execute(
            "SELECT message, sent_at FROM logs WHERE task_id = ? ORDER BY sent_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        today_checkin = conn.execute(
            "SELECT completed FROM checkins WHERE task_id = ? AND date = ?", (row["id"], today)
        ).fetchone()
        last_checkin = conn.execute(
            "SELECT created_at FROM checkins WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (row["id"],)
        ).fetchone()
        reference_time = datetime.fromisoformat(last_checkin["created_at"]) if last_checkin else datetime.fromisoformat(row["created_at"])
        can_checkin = (now - reference_time) >= timedelta(hours=24)
        result.append({
            "id": row["id"],
            "goal": row["goal"],
            "deadline": row["deadline"],
            "reminder_time": row["reminder_time"],
            "constraints": json.loads(row["constraints"]),
            "status": row["status"],
            "days_left": days_remaining(row["deadline"]),
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "latest_message": latest_log["message"] if latest_log else None,
            "today_checkin": bool(today_checkin["completed"]) if today_checkin else None,
            "can_checkin": can_checkin,
            "plan_text": row["plan_text"],
            "plan_status": row["plan_status"] or "idle",
        })
    conn.close()
    return result


@app.patch("/api/tasks/{task_id}/constraints")
def update_constraints(task_id: int, body: ConstraintUpdateRequest, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    conn.execute("UPDATE tasks SET constraints = ? WHERE id = ?", (json.dumps(body.constraints), task_id))
    conn.commit()
    conn.close()
    _queue_regen(background_tasks, task_id, user["name"], task["goal"], task["deadline"], body.constraints, task["reminder_time"])
    return {"ok": True}


@app.post("/api/tasks/{task_id}/checkin")
def checkin_task(task_id: int, body: CheckinRequest, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO checkins (task_id, date, completed, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(task_id, date) DO UPDATE SET completed = excluded.completed
        """,
        (task_id, today, int(body.completed), now),
    )
    closed_early = False
    if body.completed and task["status"] == "active":
        conn.execute("UPDATE tasks SET status = 'succeeded', completed_at = ? WHERE id = ?", (now, task_id))
        closed_early = True
    conn.commit()
    conn.close()
    if closed_early:
        _queue_regen_for_other_active_tasks(background_tasks, user["id"], user["name"], exclude_task_id=task_id)
    return {"ok": True, "task_closed": closed_early}


@app.post("/api/tasks/{task_id}/request-delete")
def request_delete(task_id: int, body: CancelReasonRequest, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    roast, _ = generate_cancel_roast(user["name"], task["goal"], task["deadline"], body.reason)
    token = new_token()
    conn.execute(
        "INSERT INTO action_tokens (task_id, action, token, note, created_at) VALUES (?, 'delete', ?, ?, ?)",
        (task_id, token, roast, datetime.now().isoformat()),
    )
    conn.commit()
    days_left = days_remaining(task["deadline"])
    subject, body_text = action_confirmation_email(task["goal"], days_left, "delete")
    body_text = f"{body_text}\n\n{roast}"
    result = _send_task_email(task_id, subject, body_text)
    result["roast"] = roast
    conn.close()
    return result


@app.post("/api/tasks/{task_id}/request-complete")
def request_complete(task_id: int, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    token = new_token()
    conn.execute(
        "INSERT INTO action_tokens (task_id, action, token, created_at) VALUES (?, 'complete', ?, ?)",
        (task_id, token, datetime.now().isoformat()),
    )
    conn.commit()
    days_left = days_remaining(task["deadline"])
    subject, body = action_confirmation_email(task["goal"], days_left, "complete")
    result = _send_task_email(task_id, subject, body)
    conn.close()
    return result


@app.get("/api/confirm-action")
def confirm_action(token: str, background_tasks: BackgroundTasks):
    conn = get_connection()
    row = conn.execute("SELECT * FROM action_tokens WHERE token = ?", (token,)).fetchone()
    if not row or row["used"]:
        conn.close()
        return _branded_page("This link's used up.", "It's invalid or already confirmed. Nothing to do here.", status_code=400)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
    if not task:
        conn.close()
        return _branded_page("That task is already gone.", "Nothing left to confirm.", status_code=404)
    user_row = conn.execute("SELECT name FROM users WHERE id = ?", (task["user_id"],)).fetchone()
    user_name = user_row["name"] if user_row else "there"
    conn.execute("UPDATE action_tokens SET used = 1 WHERE id = ?", (row["id"],))
    if row["action"] == "delete":
        file_rows = conn.execute("SELECT filepath FROM attachments WHERE task_id = ? AND kind = 'file'", (task["id"],)).fetchall()
        for f in file_rows:
            if f["filepath"]:
                full_path = os.path.join(UPLOADS_DIR, f["filepath"])
                if os.path.exists(full_path):
                    os.remove(full_path)
        conn.execute("DELETE FROM logs WHERE task_id = ?", (task["id"],))
        conn.execute("DELETE FROM checkins WHERE task_id = ?", (task["id"],))
        conn.execute("DELETE FROM chat_messages WHERE task_id = ?", (task["id"],))
        conn.execute("DELETE FROM attachments WHERE task_id = ?", (task["id"],))
        conn.execute("DELETE FROM action_tokens WHERE task_id = ?", (task["id"],))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        conn.commit()
        conn.close()
        _queue_regen_for_other_active_tasks(background_tasks, task["user_id"], user_name, exclude_task_id=task["id"])
        roast = row["note"] or f"\"{task['goal']}\" is gone. Close this tab."
        return _branded_page(f"\"{task['goal']}\" — deleted.", roast)
    else:
        conn.execute("UPDATE tasks SET status = 'succeeded', completed_at = ? WHERE id = ?", (datetime.now().isoformat(), task["id"]))
        conn.commit()
        conn.close()
        _queue_regen_for_other_active_tasks(background_tasks, task["user_id"], user_name, exclude_task_id=task["id"])
        return _branded_page(f"\"{task['goal']}\" — marked complete.", "About time. Close this tab.", accent="#6b7a5e")


@app.get("/api/tasks/{task_id}/attachments")
def list_attachments(task_id: int, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    rows = conn.execute(
        "SELECT * FROM attachments WHERE task_id = ? ORDER BY created_at DESC", (task_id,)
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"], "kind": r["kind"], "title": r["title"],
            "content": r["content"], "filename": r["filename"], "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.post("/api/tasks/{task_id}/notes")
def add_note(task_id: int, body: NoteRequest, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    if body.kind not in ("note", "link"):
        conn.close()
        raise HTTPException(status_code=400, detail="kind must be note or link")
    conn.execute(
        "INSERT INTO attachments (task_id, kind, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, body.kind, body.title, body.content, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    if task["plan_status"] == "ready":
        _queue_regen(background_tasks, task_id, user["name"], task["goal"], task["deadline"], json.loads(task["constraints"]), task["reminder_time"])
    return {"ok": True}


@app.post("/api/tasks/{task_id}/upload")
async def upload_file(task_id: int, background_tasks: BackgroundTasks, authorization: str = Header(None), title: str = Form(...), file: UploadFile = File(...)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    ext = os.path.splitext(file.filename or "")[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = os.path.join(UPLOADS_DIR, stored_name)
    contents = await file.read()
    if len(contents) > 20 * 1024 * 1024:
        conn.close()
        raise HTTPException(status_code=400, detail="file too large, 20MB max")
    with open(dest_path, "wb") as f:
        f.write(contents)
    conn.execute(
        "INSERT INTO attachments (task_id, kind, title, filepath, filename, created_at) VALUES (?, 'file', ?, ?, ?, ?)",
        (task_id, title or file.filename, stored_name, file.filename, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    if task["plan_status"] == "ready":
        _queue_regen(background_tasks, task_id, user["name"], task["goal"], task["deadline"], json.loads(task["constraints"]), task["reminder_time"])
    return {"ok": True}


@app.get("/api/attachments/{attachment_id}/download")
def download_attachment(attachment_id: int, token: str = None):
    conn = get_connection()
    row = conn.execute(
        """
        SELECT attachments.*, tasks.user_id AS owner_id FROM attachments
        JOIN tasks ON attachments.task_id = tasks.id
        WHERE attachments.id = ?
        """,
        (attachment_id,),
    ).fetchone()
    conn.close()
    if not row or row["kind"] != "file":
        raise HTTPException(status_code=404, detail="file not found")
    full_path = os.path.join(UPLOADS_DIR, row["filepath"])
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(full_path, filename=row["filename"])


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    row = conn.execute(
        """
        SELECT attachments.*, tasks.user_id AS owner_id, tasks.plan_status AS task_plan_status FROM attachments
        JOIN tasks ON attachments.task_id = tasks.id
        WHERE attachments.id = ?
        """,
        (attachment_id,),
    ).fetchone()
    if not row or row["owner_id"] != user["id"]:
        conn.close()
        raise HTTPException(status_code=404, detail="not found")
    if row["kind"] == "file" and row["filepath"]:
        full_path = os.path.join(UPLOADS_DIR, row["filepath"])
        if os.path.exists(full_path):
            os.remove(full_path)
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    conn.commit()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
    conn.close()
    if task and task["plan_status"] == "ready":
        _queue_regen(background_tasks, task["id"], user["name"], task["goal"], task["deadline"], json.loads(task["constraints"]), task["reminder_time"])
    return {"ok": True}


def _regenerate_plan_bg(task_id, user_name, goal, deadline, constraints, reminder_time):
    try:
        days_left = days_remaining(deadline)
        conn = get_connection()
        user_id = conn.execute("SELECT user_id FROM tasks WHERE id = ?", (task_id,)).fetchone()["user_id"]
        conn.close()
        with _user_plan_lock(user_id):
            conn = get_connection()
            chat_context, notes_context = build_task_context(conn, task_id)
            all_tasks_ctx = _build_all_tasks_context(conn, user_id, exclude_task_id=task_id)
            other_windows = _other_task_windows(conn, user_id, exclude_task_id=task_id)
            global_chat_ctx = _global_chat_context(conn, user_id)
            conn.close()
            plan_text, source = generate_plan(
                user_name, goal, deadline, constraints, days_left, reminder_time,
                chat_context, notes_context, all_tasks_ctx, global_chat_ctx, other_windows,
            )
            _mark_plan_status(task_id, "ready", plan_text)
    except Exception as exc:
        try:
            conn = get_connection()
            existing_plan = conn.execute("SELECT plan_text FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.close()
            if existing_plan and existing_plan["plan_text"]:
                _mark_plan_status(task_id, "ready", existing_plan["plan_text"])
            else:
                _mark_plan_status(task_id, "ready", f"Day 1 ({datetime.now().strftime('%Y-%m-%d')}): schedule generation failed ({exc}). Hit Regenerate to try again.")
        except Exception:
            _mark_plan_status(task_id, "ready", f"Day 1 ({datetime.now().strftime('%Y-%m-%d')}): schedule generation failed. Hit Regenerate.")


@app.post("/api/tasks/{task_id}/plan")
def generate_task_plan(task_id: int, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    constraints = json.loads(task["constraints"])
    conn.execute("UPDATE tasks SET plan_status = 'generating' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    background_tasks.add_task(
        _regenerate_plan_bg, task_id, user["name"], task["goal"], task["deadline"], constraints, task["reminder_time"]
    )
    return {"async": True}


@app.get("/api/schedule")
def get_schedule(authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'active'", (user["id"],)
    ).fetchall()
    conn.close()
    today = datetime.now().date()
    per_task_entries = {}
    for row in rows:
        entries = parse_plan_entries(row["plan_text"])
        by_date = {}
        for e in entries:
            by_date.setdefault(e["date"], []).append(e["action"])
        per_task_entries[row["id"]] = {"goal": row["goal"], "deadline": row["deadline"], "by_date": by_date}
    days = []
    for i in range(15):
        d = today + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        day_tasks = []
        for task_id, info in per_task_entries.items():
            is_deadline = info["deadline"] == d_str
            actions = info["by_date"].get(d_str)
            if is_deadline:
                day_tasks.append({"id": task_id, "task_name": info["goal"], "action": None, "is_deadline": True})
            elif actions:
                day_tasks.append({"id": task_id, "task_name": info["goal"], "action": actions[0], "is_deadline": False})
        days.append({
            "date": d_str,
            "label": d.strftime("%a %d"),
            "is_today": i == 0,
            "tasks": day_tasks,
        })
    return days


@app.get("/api/tasks/{task_id}/chat")
def get_chat(task_id: int, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    rows = conn.execute(
        "SELECT sender, message, created_at FROM chat_messages WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    conn.close()
    return [{"sender": r["sender"], "message": r["message"], "created_at": r["created_at"]} for r in rows]


@app.post("/api/tasks/{task_id}/chat")
def post_chat(task_id: int, body: ChatRequest, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task not found")
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO chat_messages (task_id, sender, message, created_at) VALUES (?, 'user', ?, ?)",
        (task_id, body.message, now),
    )
    conn.commit()
    history_rows = conn.execute(
        "SELECT sender, message FROM chat_messages WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    history = [{"sender": r["sender"], "message": r["message"]} for r in history_rows]
    constraints = json.loads(task["constraints"])
    days_left = days_remaining(task["deadline"])
    is_overdue = days_left < 0
    _, notes_context = build_task_context(conn, task_id)
    reply, source = generate_chat_reply(
        user["name"], task["goal"], task["deadline"], constraints, days_left, is_overdue, history, body.message, notes_context
    )
    reply_time = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO chat_messages (task_id, sender, message, created_at) VALUES (?, 'mara', ?, ?)",
        (task_id, reply, reply_time),
    )
    conn.commit()
    triggered_schedule_build = False
    if task["plan_status"] == "ready":
        conn.execute("UPDATE tasks SET plan_status = 'generating' WHERE id = ?", (task_id,))
        conn.commit()
        background_tasks.add_task(
            _regenerate_plan_bg, task_id, user["name"], task["goal"], task["deadline"], constraints, task["reminder_time"]
        )
        triggered_schedule_build = True
    conn.close()
    return {"reply": reply, "source": source, "schedule_building": triggered_schedule_build}


def build_tasks_summary(conn, user_id):
    rows = conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'active' ORDER BY deadline ASC", (user_id,)
    ).fetchall()
    lines = []
    for row in rows:
        days_left = days_remaining(row["deadline"])
        tag = f"{days_left} day(s) left" if days_left >= 0 else f"{abs(days_left)} day(s) OVERDUE"
        lines.append(f"- {row['goal']} (due {row['deadline']}, {tag})")
    return "\n".join(lines)


def compute_mood(conn, user_id):
    active_rows = conn.execute("SELECT id, deadline FROM tasks WHERE user_id = ? AND status = 'active'", (user_id,)).fetchall()
    overdue_count = sum(1 for r in active_rows if days_remaining(r["deadline"]) < 0)
    failed_count = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE user_id = ? AND status = 'failed'", (user_id,)).fetchone()["c"]
    succeeded_count = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE user_id = ? AND status = 'succeeded'", (user_id,)).fetchone()["c"]
    task_ids = [r["id"] for r in active_rows]
    yes_count = no_count = 0
    if task_ids:
        placeholders = ",".join("?" * len(task_ids))
        checkin_rows = conn.execute(f"SELECT completed FROM checkins WHERE task_id IN ({placeholders})", task_ids).fetchall()
        yes_count = sum(1 for r in checkin_rows if r["completed"])
        no_count = sum(1 for r in checkin_rows if not r["completed"])
    total_checkins = yes_count + no_count
    if overdue_count > 0:
        return {"mood": "failing", "label": "FAILING", "detail": f"{overdue_count} task(s) overdue right now"}
    if failed_count > 0 and failed_count >= succeeded_count:
        return {"mood": "failing", "label": "FAILING", "detail": f"{failed_count} closed as failed"}
    if total_checkins == 0:
        return {"mood": "watching", "label": "WATCHING", "detail": "no check-in history yet"}
    yes_ratio = yes_count / total_checkins
    if yes_ratio >= 0.7:
        return {"mood": "on_track", "label": "ON TRACK", "detail": f"{yes_count}/{total_checkins} check-ins done"}
    elif yes_ratio >= 0.4:
        return {"mood": "slipping", "label": "SLIPPING", "detail": f"{yes_count}/{total_checkins} check-ins done"}
    else:
        return {"mood": "failing", "label": "FAILING", "detail": f"{yes_count}/{total_checkins} check-ins done"}


@app.get("/api/mood")
def get_mood(authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    mood = compute_mood(conn, user["id"])
    conn.close()
    return mood


@app.get("/api/chat")
def get_global_chat(authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    rows = conn.execute(
        "SELECT sender, message, created_at FROM general_chat WHERE user_id = ? ORDER BY created_at ASC",
        (user["id"],),
    ).fetchall()
    if not rows:
        tasks_summary = build_tasks_summary(conn, user["id"])
        opening, _ = generate_opening_message(user["name"], tasks_summary)
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO general_chat (user_id, sender, message, created_at) VALUES (?, 'mara', ?, ?)",
            (user["id"], opening, now),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT sender, message, created_at FROM general_chat WHERE user_id = ? ORDER BY created_at ASC",
            (user["id"],),
        ).fetchall()
    conn.close()
    return [{"sender": r["sender"], "message": r["message"], "created_at": r["created_at"]} for r in rows]


@app.post("/api/chat")
def post_global_chat(body: ChatRequest, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO general_chat (user_id, sender, message, created_at) VALUES (?, 'user', ?, ?)",
        (user["id"], body.message, now),
    )
    conn.commit()
    history_rows = conn.execute(
        "SELECT sender, message FROM general_chat WHERE user_id = ? ORDER BY created_at ASC", (user["id"],)
    ).fetchall()
    history = [{"sender": r["sender"], "message": r["message"]} for r in history_rows]
    tasks_summary = build_tasks_summary(conn, user["id"])
    reply, source = generate_global_chat_reply(user["name"], tasks_summary, history, body.message)
    reply_time = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO general_chat (user_id, sender, message, created_at) VALUES (?, 'mara', ?, ?)",
        (user["id"], reply, reply_time),
    )
    conn.commit()
    conn.close()
    _queue_regen_for_other_active_tasks(background_tasks, user["id"], user["name"])
    return {"reply": reply, "source": source}


@app.post("/api/chat/nudge")
def post_nudge(authorization: str = Header(None)):
    user = require_user(authorization)
    conn = get_connection()
    tasks_summary = build_tasks_summary(conn, user["id"])
    rows = conn.execute("SELECT deadline FROM tasks WHERE user_id = ? AND status = 'active'", (user["id"],)).fetchall()
    nearest_days_left = None
    for row in rows:
        dl = days_remaining(row["deadline"])
        if nearest_days_left is None or dl < nearest_days_left:
            nearest_days_left = dl
    nudge, _ = generate_nudge(user["name"], tasks_summary, nearest_days_left)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO general_chat (user_id, sender, message, created_at) VALUES (?, 'mara', ?, ?)",
        (user["id"], nudge, now),
    )
    conn.commit()
    conn.close()
    return {"message": nudge, "source": "generated"}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
