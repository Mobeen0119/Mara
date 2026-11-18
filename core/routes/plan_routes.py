import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import balance, routine
from core.database import get_connection
from core.deps import require_user
from core.routes.goal_routes import rebalance_user
from core.routes.goal_routes import submit_plan


router = APIRouter(prefix="/api", tags=["plan"])
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DayModeRequest(BaseModel):
    date: str
    mode: str


class WindowKindRequest(BaseModel):
    label: str
    kind: str


@router.put("/day-mode")
def put_day_mode(body: DayModeRequest, user=Depends(require_user)):
   
    if not _ISO.match(body.date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
   
    try:
        date.fromisoformat(body.date)
   
    except ValueError:
        raise HTTPException(400, "invalid date")
   
    if body.mode not in routine.DAY_MODES:
        raise HTTPException(400, f"mode must be one of {sorted(routine.DAY_MODES)}")
    conn = get_connection()
   
    redraw = balance.apply_day_mode(conn, user, body.date, body.mode)

    for gid in sorted(redraw):
        submit_plan(user, gid)
    return {"ok": True, "date": body.date, "mode": body.mode, "modes": routine.day_modes(conn, user["id"])}


@router.get("/plan/notes")
def plan_notes(days: int = 7, user=Depends(require_user)):
    return balance.explain(get_connection(), user, days=max(1, min(days, 21)))


@router.get("/windows")
def windows(user=Depends(require_user)):
    conn = get_connection()
    return {"ask": balance.explain(conn, user, days=1)["ask"], "answers": routine.window_kinds(conn, user["id"])}


@router.put("/windows/kind")
def put_window_kind(body: WindowKindRequest, user=Depends(require_user)):
   
    if body.kind not in ("soft", "hard"):
        raise HTTPException(400, "kind must be 'soft' or 'hard'")
    label = body.label.strip()
   
    if not label or len(label) > 120:
        raise HTTPException(400, "label required (max 120 chars)")
    conn = get_connection()
    routine.set_window_kind(conn, user["id"], label, body.kind)
    rebalance_user(user)
    
    return {"ok": True, "label": label, "kind": body.kind}
