import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.database import get_connection
from core.deps import require_user
from core import generation
from core.models import ChatRequest, LinkRequest, NudgeRequest

router = APIRouter(prefix="/api", tags=["chat"])


def _add_message(conn, user_id, goal_id, role, content):
    if goal_id:
        conn.execute(
            "INSERT INTO chat_messages (goal_id, user_id, role, content) VALUES (?,?,?,?)",
            (goal_id, user_id, role, content),
        )
    else:
        conn.execute(
            "INSERT INTO general_chat (user_id, role, content) VALUES (?,?,?)",
            (user_id, role, content),
        )
    conn.commit()


def _history(conn, goal_id, user_id, limit=8):
    if goal_id:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE goal_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (goal_id, user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM general_chat WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [f"{r['role']}: {r['content']}" for r in reversed(rows)]


@router.post("/chat")
def chat(body: ChatRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    goal_id = body.goal_id
    goal = None
    if goal_id:
        goal = conn.execute(
            "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="goal not found")
        goal = dict(goal)

    _add_message(conn, user["id"], goal_id, "user", body.message)

    goals_summary = [
        r["display_title"]
        for r in conn.execute(
            "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    ]

    if goal_id and goal and generation.completion_detected(body.message):
        conn.execute("UPDATE goals SET status='succeeded', manually_succeeded=1 WHERE id=?", (goal_id,))
        conn.execute("UPDATE actions SET status='done' WHERE goal_id=? AND status='pending'", (goal_id,))
        conn.commit()
        reply = (
            "Claimed and closed. You said it's done, so the file reads done. If you're lying, "
            "the next check-in will find out. Don't insult my ledger with a fake 'done'."
        )
        _add_message(conn, user["id"], goal_id, "eloise", reply)
        return {"reply": reply, "goal_status": "succeeded", "source": "completion"}

    history = "\n".join(_history(conn, goal_id, user["id"]))
    goal_name = goal["display_title"] if goal else "general"
    text, source = generation.generate_chat_reply(user["name"], goal_name, history, body.message, db=conn)
    _add_message(conn, user["id"], goal_id, "eloise", text)
    return {"reply": text, "source": source}


@router.get("/chat")
def chat_history(user: dict = Depends(require_user)):
    conn = get_connection()
    goal_id = None
    rows = conn.execute(
        "SELECT * FROM general_chat WHERE user_id=?", (user["id"],)
    ).fetchall()
    msgs = [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]
    if rows:
        goal_id = None
    return {"goal_id": goal_id, "messages": msgs}


@router.get("/chat/goals/{goal_id}")
def goal_chat_history(goal_id: int, user: dict = Depends(require_user)):
    conn = get_connection()
    goal = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])
    ).fetchone()
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE goal_id=? AND user_id=?", (goal_id, user["id"])
    ).fetchall()
    msgs = [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]
    return {"goal_id": goal_id, "messages": msgs}


@router.post("/chat/links")
def links(body: LinkRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    goals_summary = [
        r["display_title"]
        for r in conn.execute(
            "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    ]
    goal_name = goals_summary[0] if goals_summary else "your goal"
    text, source = generation.generate_link_suggestion(user["name"], goal_name, body.message, db=conn)
    return {"reply": text, "source": source}


@router.post("/chat/nudge")
def nudge(body: NudgeRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    goal = conn.execute(
        "SELECT * FROM goals WHERE id=? AND user_id=?", (body.goal_id, user["id"])
    ).fetchone()
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")
    goals_summary = [
        r["display_title"]
        for r in conn.execute(
            "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    ]
    text, source = generation.generate_nudge(user["name"], "; ".join(goals_summary), goal["display_title"], db=conn)
    conn.execute(
        "INSERT INTO interventions (user_id, goal_id, type, message) VALUES (?,?,?,?)",
        (user["id"], body.goal_id, "nudge", text),
    )
    conn.commit()
    return {"reply": text, "source": source}