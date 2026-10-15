"""Results: the numbers (from views), the teacher's corrections, the saved final result, AI analysis.

    GET  /sessions/{id}/results        ranking, per-question stats, score distribution, raw-event statuses
    PUT  /sessions/{id}/answers        a complaint / manual entry (ARS.setAnswer)       -> answer_overrides
    GET  /sessions/{id}/overrides      every fix, oldest first (audit trail)
    POST /sessions/{id}/results        save the page's final result (ARS.saveResults)   -> session_results (upsert)
    GET  /sessions/{id}/saved-results  read it back
    POST /sessions/{id}/analyze        AI summary of the result (ARS.analyze)           -> app/analyze.py

All views use v_effective_answers, so ranking and statistics already include the fixes.
"""
import logging

from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app import analyze as ai
from app.queries import Q

log = logging.getLogger("ars.results")
router = APIRouter(prefix="/sessions", tags=["results"])
LETTERS = "ABCD"


@router.get("/{session_id}/results")
async def results(session_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        session = await _session(conn, session_id)
        params = {"session_id": session_id}
        ranking = await (await conn.execute(Q["results_ranking"], params)).fetchall()
        questions = await (await conn.execute(Q["results_questions"], params)).fetchall()
        events = await (await conn.execute(Q["event_stats"], params)).fetchall()
        distribution = await (await conn.execute(Q["score_distribution"], params)).fetchall()
    return {
        "session_id": session_id,
        "phase": session["phase"],
        "n_questions": session["n_questions"],
        "ranking": ranking,
        "questions": questions,
        "distribution": distribution,   # phổ điểm: students per score
        "events": events,
    }


# ------------------------------------------------------------------ corrections

class AnswerFixIn(BaseModel):
    """What the page sends (BACKEND_API.md). answer null = clear the answer."""
    dev: int = Field(ge=1)
    q: int = Field(ge=1)                       # question number, 1..N
    answer: str | None = Field(default=None, pattern="^[A-Da-d]$")
    mssv: str | None = None                    # checked against the device's current MSSV
    reason: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    manual: bool = False
    by: str | None = Field(default=None, max_length=40)


@router.put("/{session_id}/answers")
async def set_answer(session_id: int, body: AnswerFixIn, request: Request):
    """Record a teacher's fix. Idempotent: sending the same fix again (the page's outbox retries)
    does not add a row; only a DIFFERENT fix becomes the new latest one."""
    choice = LETTERS.index(body.answer.upper()) + 1 if body.answer else None
    async with request.app.state.pool.connection() as conn:
        await _session(conn, session_id)
        async with conn.transaction():                         # FOR UPDATE below holds until commit
            ctx = await (await conn.execute(Q["override_context"],
                                            {"session_id": session_id, "dev": body.dev, "q": body.q})).fetchone()
            if ctx is None:
                raise HTTPException(404, f"thiết bị {body.dev:02d} chưa đăng ký trong phiên {session_id}")
            if ctx["question_id"] is None:
                raise HTTPException(404, f"đề không có câu {body.q}")
            if body.mssv and body.mssv != ctx["mssv"]:
                raise HTTPException(409, f"thiết bị {body.dev:02d} hiện thuộc MSSV {ctx['mssv']}, không phải {body.mssv}")
            if ctx["phase"] == "QUESTION_OPEN" and ctx["current_q"] == body.q:
                raise HTTPException(409, f"câu {body.q} đang mở: chờ hết giờ rồi mới sửa")
            if ctx["phase"] != "ENDED" and (ctx["current_q"] is None or body.q > ctx["current_q"]):
                raise HTTPException(409, f"câu {body.q} chưa được hỏi")
            if choice is not None and choice > ctx["n_choices"]:
                raise HTTPException(422, f"câu {body.q} chỉ có {ctx['n_choices']} phương án")

            params = {"session_id": session_id, "dev": body.dev, "question_id": ctx["question_id"],
                      "choice": choice, "mssv": ctx["mssv"], "reason": body.reason, "note": body.note,
                      "manual": body.manual, "by": body.by}
            last = await (await conn.execute(Q["latest_override"], params)).fetchone()
            same = last is not None and all(last[k] == v for k, v in (
                ("choice", choice), ("reason", body.reason), ("note", body.note),
                ("manual", body.manual), ("by_teacher", body.by)))
            if same:
                return {"ok": True, "id": last["id"], "duplicate": True}
            row = await (await conn.execute(Q["add_override"], params)).fetchone()
    log.info("session %s: fix dev %02d q%d -> %s (%s)", session_id, body.dev, body.q, body.answer, body.reason)
    return {"ok": True, "id": row["id"], "duplicate": False}


@router.get("/{session_id}/overrides")
async def list_overrides(session_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        await _session(conn, session_id)
        return await (await conn.execute(Q["list_overrides"], {"session_id": session_id})).fetchall()


# ------------------------------------------------------------------ saved final result

@router.post("/{session_id}/results")
async def save_results(session_id: int, payload: dict, request: Request):
    """Upsert by session: the page may send the same result several times (retries,
    or again after a late complaint). The newest payload wins; version counts the saves."""
    if payload.get("session_id") not in (None, session_id):
        raise HTTPException(422, "session_id in the body does not match the URL")
    async with request.app.state.pool.connection() as conn:
        await _session(conn, session_id)
        row = await (await conn.execute(Q["save_results"], {
            "session_id": session_id,
            "teacher": payload.get("teacher"),
            "payload": Jsonb(payload),
            "analysis": payload.get("analysis"),
        })).fetchone()
    return {"ok": True, **row}


@router.get("/{session_id}/saved-results")
async def get_saved_results(session_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        row = await (await conn.execute(Q["get_saved_results"], {"session_id": session_id})).fetchone()
    if row is None:
        raise HTTPException(404, f"session {session_id} has no saved result")
    return row


# ------------------------------------------------------------------ AI analysis

@router.post("/{session_id}/analyze")
async def analyze(session_id: int, payload: dict, request: Request):
    async with request.app.state.pool.connection() as conn:
        await _session(conn, session_id)
    try:
        text = await ai.analyze(payload)
    except ai.AnalyzeError as exc:
        raise HTTPException(exc.status, str(exc))
    return {"text": text}


async def _session(conn, session_id: int) -> dict:
    row = await (await conn.execute(Q["get_session"], {"session_id": session_id})).fetchone()
    if row is None:
        raise HTTPException(404, f"session {session_id} not found")
    return row
