import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from fastapi.responses import JSONResponse, StreamingResponse

from core import ratelimit
from core.clock import today_for
from core.database import get_connection
from core.deps import require_user
from core import generation, intents, routine
from core.models import ChatRequest, LinkRequest, NudgeRequest, QuickCommandRequest

router = APIRouter(prefix="/api", tags=["chat"])


@router.get("/eloise")
def eloise_state(user: dict = Depends(require_user)):
    """Feed for the console panel: live telemetry + Eloise's current mood, derived
    from the real board (pressure of the hottest goal, momentum, stakes, uptime)."""
    conn = get_connection()
    today_d = today_for(user)
    today = today_d.isoformat()
    goals = [dict(g) for g in conn.execute(
        "SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()]
    top = None
    for g in goals:
        acts = [dict(a) for a in conn.execute("SELECT * FROM actions WHERE goal_id=?", (g["id"],)).fetchall()]
        blk = conn.execute("SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)).fetchone()["c"]
        miss = sum(1 for a in acts if a["status"] == "missed")
        pr = generation.pressure_rating(g, acts, open_blockers=blk, missed_days=miss, today=today_d)
        if top is None or pr["score"] > top["score"]:
            top = {"goal": g["display_title"], "score": pr["score"], "label": pr["label"],
                   "days_left": pr["days_left"], "remaining_hours": pr["remaining_hours"]}
    mom = generation.momentum_stats(user["id"], db=conn, today=today_d)
    stakes = conn.execute(
        "SELECT s.id, s.punishment, g.display_title FROM stakes s JOIN goals g ON g.id=s.goal_id "
        "WHERE s.user_id=? AND s.status='active'", (user["id"],)
    ).fetchall()
    # boot/uptime feel
    created_at = conn.execute("SELECT created_at FROM users WHERE id=?", (user["id"],)).fetchone()["created_at"]
    boot = created_at or "seated"
    # mood from the last Eloise line
    last_row = conn.execute(
        "SELECT content FROM general_chat WHERE user_id=? AND role='eloise' ORDER BY id DESC LIMIT 1", (user["id"],)
    ).fetchone()
    mood = generation.classify_mood(last_row["content"] if last_row else "")
    return {
        "mood": mood["mood"],
        "status": mood["status"],
        "uptime": str(boot)[:19].replace("T", " "),
        "top_pressure": top,
        "momentum": mom["momentum"],
        "streak": mom["current_streak"],
        "burn_hours": mom["burn_hours_per_day"],
        "stakes": [{"punishment": s["punishment"], "goal": s["display_title"]} for s in stakes],
    }


def _triage_line(conn, user_id, today):
    try:
        from core import triage
        u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return triage.summary_line(conn, dict(u), today) if u else ""
    except Exception:
        return ""


def _active_board_snapshot(conn, user_id, today=None):
    """A compact, live digest of the user's current board: active goals with their
    deadlines, today's pending/done tasks, and any unresolved open flags."""
    rows = conn.execute(
        "SELECT display_title, deadline, constraints, details FROM goals WHERE user_id=? AND status='active'", (user_id,)
    ).fetchall()
    today = today or date.today().isoformat()
    goals = []
    for r in rows:
        try:
            dd = generation.days_remaining(r["deadline"], today)
        except Exception:
            dd = 0
        cons = ""
        try:
            clist = json.loads(r["constraints"] or "[]")
            if clist:
                cons = f", blocked: {', '.join(clist[:3])}"
        except Exception:
            cons = ""
        details = generation.goal_details_summary(r["details"])
        detail_str = f" [details: {details}]" if details else ""
        goals.append(f"'{r['display_title']}' (due {r['deadline']}, {dd} days left{cons}{detail_str})")
    pending = []
    done = []
    tasks = conn.execute(
        "SELECT a.title, a.start_time, a.date, a.status, g.display_title "
        "FROM actions a JOIN goals g ON g.id=a.goal_id "
        "WHERE a.user_id=? AND a.date=? AND g.status='active' ORDER BY a.start_time",
        (user_id, today),
    ).fetchall()
    for t in tasks:
        entry = f"{t['display_title']}: {t['title']}"
        if t["start_time"]:
            entry += f" @ {t['start_time']}"
        (done if t["status"] == "done" else pending).append(entry)
    upcoming = []
    try:
        from datetime import date as _d, timedelta as _td
        t0 = _d.fromisoformat(today)
        ahead = conn.execute(
            "SELECT a.date, a.title, a.start_time, g.display_title FROM actions a "
            "JOIN goals g ON g.id=a.goal_id WHERE a.user_id=? AND a.date>? AND a.date<=? "
            "AND a.status='pending' AND g.status='active' ORDER BY a.date, a.start_time LIMIT 12",
            (user_id, today, (t0 + _td(days=3)).isoformat()),
        ).fetchall()
        upcoming = [f"{r['date']} {r['start_time'] or ''} {r['display_title']}: {r['title']}".replace("  ", " ") for r in ahead]
    except Exception:
        upcoming = []
    powers = conn.execute(
        "SELECT type, message FROM interventions WHERE user_id=? AND acknowledged=0", (user_id,)
    ).fetchall()
    flags = [f"{p['type']}: {p['message']}" for p in powers]
    summary = {
        "active_goals": goals,
        "today_pending": pending,
        "today_done": done,
        "open_flags": flags,
    }
    return "; ".join(
        [
            (f"active goals: {', '.join(goals)}" if goals else "no active goals"),
            (f"today's tasks: {', '.join(pending)}" if pending else "no tasks scheduled today"),
            (f"next 3 days: {' | '.join(upcoming)}" if upcoming else ""),
            _triage_line(conn, user_id, today),
            (f"open flags: {', '.join(flags)}" if flags else ""),
        ]
    ).strip("; ")


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


def _history(conn, goal_id, user_id, limit=8, drop_reply_to=None):
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
  
    _MAX_TOTAL_CHARS = 2200
    _MAX_MSG_CHARS = 400
    lines = []
    for r in reversed(rows):
        content = r["content"]
        if len(content) > _MAX_MSG_CHARS:
            content = content[:_MAX_MSG_CHARS] + "…"
        lines.append(f"{r['role']}: {content}")
    while sum(len(l) for l in lines) > _MAX_TOTAL_CHARS and len(lines) > 1:
        lines.pop(0)

    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("eloise:"):
            lines.pop(i)
            break
    return lines


class ConfirmRequest(BaseModel):
    proposal_id: int
    accept: bool = True


def _act_before_llm(conn, user, goal_id, message):
    patch = routine.parse_statement(message)
    if patch:
        routine.save_override(conn, user["id"], patch)
        from core.routes.goal_routes import submit_plan
        for g in conn.execute("SELECT id FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall():
            submit_plan(user, g["id"])
        bits = []
    
        if patch.get("sleep"):
            bits.append(f"sleep {generation.fmt_min(patch['sleep'][0])}-{generation.fmt_min(patch['sleep'][1])}")   
       
        for k, v in (patch.get("meals") or {}).items():
            bits.append(f"{k} {generation.fmt_min(v[0])}")
      
        if patch.get("workout"):
            bits.append(f"workout {generation.fmt_min(patch['workout'][0])}-{generation.fmt_min(patch['workout'][1])}")
        return {"reply": "Noted for every goal, not just this one: " + ", ".join(bits) + ". Redrawing your plans around it.",
                "source": "routine"}
    nav = intents.navigate(conn, user["id"], message)
    if nav:
        return {"reply": nav["reply"], "ui": nav["ui"], "source": "nav"}
    prop = intents.detect(conn, user["id"], message, today_for(user).isoformat(), goal_id)
    if not prop:
        from core import actions
        prop = actions.propose(conn, user["id"], message, today_for(user).isoformat(), goal_id)
    if prop:
        return {"reply": prop["label"], "proposal": prop, "source": "proposal"}
    return None


def _model_label(conn):
    try:
        from core.llm.manager import LLMManager
        cloud = next((p for n, p in LLMManager()._providers().items() if n != "ollama"), None)
        return f"{cloud.name} - {cloud.model}" if cloud else "the model"
    except Exception:
        return "the model"


@router.post("/chat/confirm")
def chat_confirm(body: ConfirmRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    res = intents.resolve(conn, user["id"], body.proposal_id, body.accept)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("detail", "failed"))
    gid = res.get("goal_id")
    _add_message(conn, user["id"], gid if res.get("kind") in ("complete_goal", "cancel_goal", "complete_task", "delete_task", "replan", "set_deadline", "add_constraint", "move_task") else None,
                 "eloise", res["message"])
    return res


@router.post("/chat")
def chat(body: ChatRequest, user: dict = Depends(require_user)):
    ratelimit.enforce(f"llm:{user['id']}", 30, 60, "easy - you're sending faster than I can think")
    conn = get_connection()
    goal_id = body.goal_id
    _msg = (body.message or "").strip()[:2000]
    message = _msg
    goal = None
    if goal_id:
        goal = conn.execute(
            "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="goal not found")
        goal = dict(goal)

    _add_message(conn, user["id"], goal_id, "user", message)

    goals_summary = [
        r["display_title"]
        for r in conn.execute(
            "SELECT display_title FROM goals WHERE user_id=? AND status='active'", (user["id"],)
        ).fetchall()
    ]

    generation.absorb_persona_directive(conn, user["id"], message)

    acted = _act_before_llm(conn, user, goal_id, message)
    if acted:
        _add_message(conn, user["id"], goal_id, "eloise", acted["reply"])
        return acted

    history = "\n".join(_history(conn, goal_id, user["id"], drop_reply_to=message))
    goal_name = goal["title"] if (goal and goal.get("title")) else (goal["display_title"] if goal else "general")

  
    if generation._is_truly_harmful(message, context=history):
        refusal = generation.GUARDRAIL_REFUSAL
        _add_message(conn, user["id"], goal_id, "eloise", refusal)
        return {"reply": refusal, "source": "guardrail"}

    persona = generation.user_persona(conn, user["id"])
    location = generation.user_location(conn, user["id"])
    step_done = generation.step_done_detected(message)
    if goal_id:
        # goal-scoped chat: pull that goal's actual scheduled tasks so the
        # model can point at real concrete next steps instead of hand-waving.
        plan_rows = conn.execute(
            "SELECT date, title, start_time FROM actions WHERE goal_id=? AND status!='done' ORDER BY date, start_time LIMIT 12",
            (goal_id,),
        ).fetchall()
        plan_lines = "\n".join(
            f"- {r['date']} {r['start_time'] or ''}: {r['title']}" for r in plan_rows
        )
        text, source = generation.generate_chat_reply(
            user["name"], goal_name, history, message, db=conn, plan_lines=plan_lines,
            persona=persona, step_done=step_done, location=location,
        )
    else:
        board = _active_board_snapshot(conn, user["id"], today_for(user).isoformat())
        text, source = generation.generate_global_chat_reply(
            user["name"], [board], history, message, db=conn, persona=persona,
            location=location,
        )
    if source == "offline" or not text:
        conn.commit()
        raise HTTPException(
            status_code=503,
            detail="The model didn't give a usable reply — it may be unreachable, or it only "
                   "echoed back the question. Open Settings > LLM Provider, paste an API key "
                   "and hit Test connection: the test shows the real error. Once it answers, "
                   "send that again.",
        )
  
    prev = _last_eloise_reply(conn, goal_id, user["id"])
    if prev and _is_near_dup(text, prev):
        conn.commit()
        raise HTTPException(
            status_code=503,
            detail="The model repeated its previous reply verbatim instead of answering. That's on the model, not you — send it again and it should give a fresh answer, or switch the chat model in LLM settings.",
        )
    _add_message(conn, user["id"], goal_id, "eloise", text)
    return {"reply": text, "source": source}


def _last_eloise_reply(conn, goal_id, user_id):
    try:
        if goal_id:
            row = conn.execute(
                "SELECT content FROM chat_messages WHERE goal_id=? AND user_id=? AND role='eloise' ORDER BY id DESC LIMIT 1",
                (goal_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT content FROM general_chat WHERE user_id=? AND role='eloise' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return row["content"] if row else ""
    except Exception:
        return ""


def _is_near_dup(a, b):
    import difflib
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b or len(a) < 20 or len(b) < 20:
        return a == b and len(a) > 0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.82


class _SSEBodySerializer:
    pass


@router.post("/chat/stream")
def chat_stream(body: ChatRequest, user: dict = Depends(require_user)):
    """SSE variant of /chat: reply tokens are pushed as they're generated so the
    frontend can render text live instead of staring at a spinner for up to 90s."""
    import json as _json
    ratelimit.enforce(f"llm:{user['id']}", 30, 60, "easy - you're sending faster than I can think")

    def send(data: dict) -> str:
        return f"data: {_json.dumps(data)}\n\n"

    conn = get_connection()
    goal_id = body.goal_id
    message = (body.message or "").strip()[:2000]
    goal = None
    if goal_id:
        goal = conn.execute(
            "SELECT * FROM goals WHERE id=? AND user_id=?", (goal_id, user["id"])
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="goal not found")
        goal = dict(goal)

    _add_message(conn, user["id"], goal_id, "user", message)

   
    generation.absorb_persona_directive(conn, user["id"], message)

    acted = _act_before_llm(conn, user, goal_id, message)
    if acted:
        _add_message(conn, user["id"], goal_id, "eloise", acted["reply"])
        done = {"done": True, "source": acted["source"]}
        if acted.get("proposal"):
            done["proposal"] = acted["proposal"]
        if acted.get("ui"):
            done["ui"] = acted["ui"]
        return StreamingResponse(iter([send({"delta": acted["reply"]}), send(done)]), media_type="text/event-stream")

    history = "\n".join(_history(conn, goal_id, user["id"], drop_reply_to=message))
    goal_name = goal["title"] if (goal and goal.get("title")) else (goal["display_title"] if goal else "general")

    if generation._is_truly_harmful(message, context=history):
        refusal = generation.GUARDRAIL_REFUSAL
        _add_message(conn, user["id"], goal_id, "eloise", refusal)
        return StreamingResponse(
            iter([send({"delta": refusal}), send({"done": True, "source": "guardrail"})]),
            media_type="text/event-stream",
        )

 
    def generate():
        collected = []
        gconn = get_connection()
        persona = generation.user_persona(gconn, user["id"])
        location = generation.user_location(gconn, user["id"])
        step_done = generation.step_done_detected(message)
        if goal_id:
            plan_rows = gconn.execute(
                "SELECT date, title, start_time FROM actions WHERE goal_id=? AND status!='done' ORDER BY date, start_time LIMIT 12",
                (goal_id,),
            ).fetchall()
            plan_lines = "\n".join(
                f"- {r['date']} {r['start_time'] or ''}: {r['title']}" for r in plan_rows
            )
            stream = generation.stream_chat_reply(
                user["name"], goal_name, history, message, db=gconn, plan_lines=plan_lines,
                persona=persona, step_done=step_done, location=location,
            )
        else:
            board = _active_board_snapshot(gconn, user["id"], today_for(user).isoformat())
            stream = generation.stream_global_chat_reply(
                user["name"], [board], history, message, db=gconn, persona=persona,
                location=location,
            )
        source = None
        yield _json.dumps({"stage": "board"})
        yield _json.dumps({"stage": "model", "label": _model_label(gconn)})
        try:
            for chunk, src in stream:
                if not chunk:
                    continue
                source = source or src
                collected.append(chunk)
                yield _json.dumps({"delta": chunk})
            if collected:
                joined = "".join(collected)
                prev = _last_eloise_reply(gconn, goal_id, user["id"])
                if prev and _is_near_dup(joined, prev):
                    yield _json.dumps({"error": True, "done": True, "source": source or "offline",
                                       "detail": "The model repeated its previous reply verbatim instead of answering. That's on the model — send it again and it should give a fresh answer."})
                    return
                try:
                    _add_message(gconn, user["id"], goal_id, "eloise", joined)
                except Exception:
                    pass
            yield _json.dumps({"done": True, "source": source or "offline"})
        except Exception:
            yield _json.dumps({"error": True, "done": True, "source": "offline",
                               "detail": "The model didn't give a usable reply — it may be unreachable, or it only echoed back the question. Open Settings > LLM Provider and hit Test connection: that shows the real error. Fix it there, then send that again."})

    return StreamingResponse(
        (f"data: {frame}\n\n" for frame in generate()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat")
def chat_history(user: dict = Depends(require_user)):
    conn = get_connection()
    goal_id = None
    rows = list(reversed(conn.execute(
        "SELECT * FROM general_chat WHERE user_id=? ORDER BY id DESC LIMIT 200", (user["id"],)
    ).fetchall()))
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
    rows = list(reversed(conn.execute(
        "SELECT * FROM chat_messages WHERE goal_id=? AND user_id=? ORDER BY id DESC LIMIT 200",
        (goal_id, user["id"]),
    ).fetchall()))
    msgs = [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]
    return {"goal_id": goal_id, "messages": msgs}


@router.post("/chat/quick")
def quick_command(body: QuickCommandRequest, user: dict = Depends(require_user)):
    """Fast, deterministic command responses — no LLM, instant. Kicks off the quick-command
    chips in the chat so Eloise answers about the live board the moment you tap."""
    import re as _re
    from datetime import date as _d
    cmd = (body.command or "").strip().lower()
    conn = get_connection()
    today_d = today_for(user)
    today = today_d.isoformat()

    if cmd in ("today", "plate"):
        items = conn.execute(
            "SELECT a.title, a.start_time, a.end_time, a.status, a.goal_id, a.date, "
            "g.display_title FROM actions a JOIN goals g ON g.id=a.goal_id "
            "WHERE a.user_id=? AND a.date=? AND g.status='active' ORDER BY a.start_time",
            (user["id"], today),
        ).fetchall()
        if not items:
            return {"reply": "Nothing booked today. That's a red flag or a lie — file something real.", "source": "quick"}
        lines = [f"{t['start_time'] or 'anytime'} {t['display_title']} — {t['title']} [{t['status']}]" for t in items]
        return {"reply": "Today's plate:\n" + "\n".join(lines), "source": "quick"}

    if cmd in ("score", "pressure", "crisis"):
        goals = [dict(g) for g in conn.execute("SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall()]
        if not goals:
            return {"reply": "No active files. No pressure. Suspiciously calm in here.", "source": "quick"}
        out = []
        for g in goals:
            acts = [dict(a) for a in conn.execute("SELECT * FROM actions WHERE goal_id=?", (g["id"],)).fetchall()]
            blk = conn.execute("SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)).fetchone()["c"]
            miss = sum(1 for a in acts if a["status"] == "missed")
            pr = generation.pressure_rating(g, acts, open_blockers=blk, missed_days=miss, today=today_d)
            out.append(f"{g['display_title']}: {pr['score']}/10 — {pr['label']} ({pr['days_left']}d left)")
        out.sort(key=lambda s: 0)
        return {"reply": "Pressure board:\n" + "\n".join(out), "source": "quick"}

    if cmd in ("urgent", "fire"):
        goals = [dict(g) for g in conn.execute("SELECT * FROM goals WHERE user_id=? AND status='active'", (user["id"],)).fetchall()]
        best = None
        for g in goals:
            acts = [dict(a) for a in conn.execute("SELECT * FROM actions WHERE goal_id=?", (g["id"],)).fetchall()]
            blk = conn.execute("SELECT COUNT(*) c FROM blockers WHERE goal_id=? AND status='open'", (g["id"],)).fetchone()["c"]
            miss = sum(1 for a in acts if a["status"] == "missed")
            pr = generation.pressure_rating(g, acts, open_blockers=blk, missed_days=miss, today=today_d)
            if best is None or pr["score"] > best[0]:
                best = (pr["score"], g, pr)
        if not best:
            return {"reply": "No fires. The building is empty — great, that's boring.", "source": "quick"}
        _, g, pr = best
        act = conn.execute(
            "SELECT * FROM actions WHERE goal_id=? AND status!='done' AND date<=? ORDER BY date LIMIT 1",
            (g["id"], today),
        ).fetchone()
        urgent = act["title"] if act else g["display_title"]
        return {"reply": f"Fire at {pr['score']}/10: '{g['display_title']}'. Crack open '{urgent}' now.", "source": "quick"}

    if cmd in ("momentum", "streak"):
        m = generation.momentum_stats(user["id"], db=conn, today=today_d)
        verdict = "on fire" if m["momentum"] >= 70 else "ticking" if m["momentum"] >= 40 else "stalled"
        return {"reply":
            f"Momentum {m['momentum']}/100 — {verdict}. Best run: {m['best_streak']} straight days. "
            f"Current streak: {m['current_streak']}. Averaging {m['burn_hours_per_day']}h/day of done work.",
            "source": "quick"}

    if cmd in ("reschedule", "fuckit", "fuck it", "f-it", "f*ckit", "shove it"):
        ids = [r["id"] for r in conn.execute(
            "SELECT a.id FROM actions a JOIN goals g ON g.id=a.goal_id WHERE a.user_id=? AND g.status='active' AND a.status='pending'",
            (user["id"],)).fetchall()]
        if not ids:
            return {"reply": "Nothing to reschedule. 'Fuck it' only counts when there's work to dodge.", "source": "quick"}
        prop = intents._store(conn, user["id"], "slide_all", None, ids, "Push every pending task one day later?")
        return {"reply": prop["label"], "proposal": prop, "source": "quick"}

    if cmd in ("stakes", "debt"):
        stakes = conn.execute(
            "SELECT s.punishment, s.status, g.display_title FROM stakes s JOIN goals g ON g.id=s.goal_id "
            "WHERE s.user_id=? AND s.status='active'", (user["id"],)
        ).fetchall()
        if not stakes:
            return {"reply": "Nothing staked. Play it safe if you want — I respect a coward who at least admits it.", "source": "quick"}
        return {"reply": "On the line:\n" + "\n".join(f"- {s['punishment']} (if {s['display_title']} slips)" for s in stakes), "source": "quick"}

    if cmd in ("awesome", "cool", "nice", "impressive", "how"):
        return {
            "reply": "Because it's not magic, it's a ledger you can't lie to: live pressure scores, sawed-streak "
                     "momentum, stakes pinned to deadlines, and a schedule that redraws around what's actually blocked. "
                     "The polish is just fear wearing mascara.",
            "source": "quick",
        }

    return {"reply": "I don't know that command. Try Today, Score, Fire, Momentum, Reschedule, or Stakes.", "source": "quick"}


@router.post("/chat/nudge")
def nudge(body: NudgeRequest, user: dict = Depends(require_user)):
    ratelimit.enforce(f"llm:{user['id']}", 30, 60)
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