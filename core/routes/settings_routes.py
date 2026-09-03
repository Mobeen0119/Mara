import json
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_connection
from core.deps import require_user
from core import generation
from core.llm import LLMManager
from core.models import CheckinTimeRequest, LLMSettingsRequest

router = APIRouter(prefix="/api", tags=["settings"])

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _build(conn):
    return LLMManager(db=conn)


@router.get("/status")
def status(user: dict = Depends(require_user)):
    conn = get_connection()
    mgr = _build(conn)
    return mgr.status_dict()


@router.get("/settings/llm")
def get_llm(user: dict = Depends(require_user)):
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key='llm'").fetchone()
    cfg = {}
    if row and row[0]:
        try:
            cfg = json.loads(row[0])
        except Exception:
            cfg = {}
    import os
    return {
        "ollama_url": cfg.get("ollama_url") or os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        "ollama_model": cfg.get("ollama_model") or os.environ.get("OLLAMA_MODEL", "huihui_ai/dolphin3-abliterated:latest"),
        "openrouter_model": cfg.get("openrouter_model") or os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"),
        "openrouter_key_set": bool(cfg.get("openrouter_key") or os.environ.get("OPENROUTER_API_KEY")),
    }


@router.put("/settings/llm")
def put_llm(body: LLMSettingsRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key='llm'").fetchone()
    cfg = {}
    if row and row[0]:
        try:
            cfg = json.loads(row[0])
        except Exception:
            cfg = {}
    if body.ollama_url is not None:
        cfg["ollama_url"] = body.ollama_url.strip()
    if body.ollama_model is not None:
        cfg["ollama_model"] = body.ollama_model.strip()
    if body.openrouter_model is not None:
        cfg["openrouter_model"] = body.openrouter_model.strip()
    if body.openrouter_key is not None and body.openrouter_key.strip():
        cfg["openrouter_key"] = body.openrouter_key.strip()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('llm', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(cfg),),
    )
    conn.commit()
    return {"ok": True}


@router.put("/settings/checkin-time")
def set_checkin_time(body: CheckinTimeRequest, user: dict = Depends(require_user)):
    if not TIME_RE.match(body.checkin_time):
        raise HTTPException(status_code=400, detail="invalid time, expected HH:MM")
    conn = get_connection()
    conn.execute("UPDATE users SET checkin_time=? WHERE id=?", (body.checkin_time, user["id"]))
    conn.commit()
    return {"ok": True}


@router.get("/settings")
def get_settings(user: dict = Depends(require_user)):
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key='llm'").fetchone()
    cfg = {}
    if row and row[0]:
        try:
            cfg = json.loads(row[0])
        except Exception:
            cfg = {}
    return {"checkin_time": user["checkin_time"] or "08:00", "llm": get_llm(user=user)}


@router.get("/today")
def today(user: dict = Depends(require_user)):
    conn = get_connection()
    today = date.today().isoformat()
    goals = conn.execute(
        "SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()
    items = []
    interventions = []
    for g in goals:
        acts = conn.execute(
            "SELECT * FROM actions WHERE goal_id=? AND date=? ORDER BY order_idx", (g["id"], today)
        ).fetchall()
        for a in acts:
            items.append({
                "id": a["id"], "goal_id": g["id"], "goal_title": g["display_title"],
                "title": a["title"], "start_time": a["start_time"], "end_time": a["end_time"],
                "status": a["status"],
            })
        ivs = conn.execute(
            "SELECT * FROM interventions WHERE goal_id=? AND acknowledged=0", (g["id"],)
        ).fetchall()
        for iv in ivs:
            interventions.append({
                "id": iv["id"], "goal_id": g["id"], "message": iv["message"], "type": iv["type"],
            })
    return {"items": items, "interventions": interventions, "date": today}


@router.post("/interventions/{iv_id}/acknowledge")
def acknowledge(iv_id: int, user: dict = Depends(require_user)):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM interventions WHERE id=? AND user_id=?", (iv_id, user["id"])
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="intervention not found")
    conn.execute("UPDATE interventions SET acknowledged=1 WHERE id=?", (iv_id,))
    conn.commit()
    return {"ok": True}


@router.get("/scoreboard")
def scoreboard(user: dict = Depends(require_user)):
    conn = get_connection()
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()
    scored = []
    for g in rows:
        dl = generation.days_remaining(g["deadline"], today)
        plan = conn.execute(
            "SELECT * FROM actions WHERE goal_id=? ORDER BY order_idx", (g["id"],)
        ).fetchall()
        pl_min = sum(a["duration_min"] or 60 for a in plan)
        done_min = sum(a["duration_min"] or 60 for a in plan if a["status"] == "done")
        planned_days = sorted(set(a["date"] for a in plan))
        missed = sum(1 for a in plan if a["status"] == "missed")
        buffer_hours = round((pl_min - done_min) / 60.0, 1)
        open_blockers = conn.execute(
            "SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)
        ).fetchone()["c"]
        if dl < 0:
            risk = "overdue"
        elif dl == 0 and buffer_hours > 0:
            risk = "impossible"
        elif buffer_hours > 0 and dl >= 1:
            pace = (pl_min - done_min) / 60.0 / max(dl, 1)
            risk = "safe" if pace <= 4.0 else "at_risk"
        else:
            risk = "safe"
        scored.append({
            "goal_id": g["id"],
            "title": g["display_title"],
            "deadline": g["deadline"],
            "days_left": dl,
            "shortfall_hours": buffer_hours,
            "pace_hours_per_day": 0.0,
            "days_needed_at_pace": 0,
            "open_blocker_count": open_blockers,
            "missed_days": missed,
            "risk": risk,
        })
    scored.sort(key=lambda s: {"safe": 0, "at_risk": 1, "impossible": 2, "overdue": 3}[s["risk"]], reverse=True)
    return {"goals": scored, "worst": (scored[0] if scored else None), "date": today}


@router.get("/schedule")
def schedule(user: dict = Depends(require_user)):
    conn = get_connection()
    today = date.today().isoformat()
    horizon = (date.today() + timedelta(days=14)).isoformat()
    goals = conn.execute(
        "SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()
    out = {}
    for g in goals:
        acts = conn.execute(
            "SELECT * FROM actions WHERE goal_id=? AND date>=? AND date<=? ORDER BY order_idx",
            (g["id"], today, horizon),
        ).fetchall()
        by_date = {}
        for a in acts:
            by_date.setdefault(a["date"], []).append({
                "title": a["title"], "start_time": a["start_time"], "end_time": a["end_time"],
                "status": a["status"], "goal_id": g["id"],
            })
        out[str(g["id"])] = {"title": g["display_title"], "by_date": by_date}
    return out