"""Results: ranking, per-question statistics, score distribution and raw-event statuses — all read from views."""
from fastapi import APIRouter, HTTPException, Request

from app.queries import Q

router = APIRouter(prefix="/sessions", tags=["results"])


@router.get("/{session_id}/results")
async def results(session_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(Q["get_session"], {"session_id": session_id})
        session = await cur.fetchone()
        if session is None:
            raise HTTPException(404, f"session {session_id} not found")
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
