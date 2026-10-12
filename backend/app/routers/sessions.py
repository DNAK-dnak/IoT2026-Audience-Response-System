"""Session endpoints: the teacher drives the state machine from here.

Every transition endpoint does the same three things:
    1. read the session and check the transition with the state machine (409 if not allowed)
    2. run the guarded UPDATE from queries.sql (0 rows updated -> 409, e.g. no next question)
    3. publish the new RETAINED state so every clicker switches screen

Opening a question also starts a timer that closes it automatically at the deadline.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import psycopg
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.models import SessionIn, SessionOut
from app.mqtt import publish, publish_state
from app.queries import Q
from app.state_machine import Event, InvalidTransition, next_phase

log = logging.getLogger("ars.sessions")
router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut, status_code=201)
async def create_session(body: SessionIn, request: Request):
    async with request.app.state.pool.connection() as conn:
        try:
            cur = await conn.execute(Q["create_session"], body.model_dump())
            session_id = (await cur.fetchone())["id"]
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, f"room {body.room} already has an active session; end it first")
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(404, f"quiz {body.quiz_id} or class {body.class_id} does not exist")
        await publish_state(request.app, conn, session_id, body.room)
        return await _get(conn, session_id)


@router.get("/active", response_model=SessionOut | None)
async def active_session(request: Request, room: str = "room1"):
    """The session that is still running in this room (not ENDED), or null.
    The teacher page uses it to resume after a reload, or to end a forgotten session."""
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(Q["find_active_session"], {"room": room})
        row = await cur.fetchone()
        return await _get(conn, row["id"]) if row else None


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        return await _get(conn, session_id)


@router.get("/{session_id}/devices")
async def list_devices(session_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        await _get(conn, session_id)   # 404 if the session does not exist
        cur = await conn.execute(Q["list_session_devices"], {"session_id": session_id})
        return await cur.fetchall()


@router.get("/{session_id}/answers")
async def list_answers(session_id: int, request: Request):
    """The answer grid (device x question) AFTER the teacher's fixes. Choices are 1-4 (A-D on the page).
    choice = what counts; device_choice = what the device sent; override_id/reason/... = the fix, if any.
    A row with choice null = the teacher cleared that answer."""
    async with request.app.state.pool.connection() as conn:
        await _get(conn, session_id)
        cur = await conn.execute(Q["effective_answers"], {"session_id": session_id})
        return await cur.fetchall()


# ------------------------------------------------------------------ removing devices

@router.delete("/{session_id}/devices/{dev}")
async def remove_device(session_id: int, dev: int, request: Request):
    """Unregister one device (registration phase only). Its MSSV becomes free again and the
    device is told to go back to the MSSV screen (ack code REMOVED)."""
    return await _remove(request.app, session_id, dev)


@router.delete("/{session_id}/devices")
async def clear_devices(session_id: int, request: Request):
    """Unregister every device of the session (registration phase only)."""
    return await _remove(request.app, session_id, None)


async def _remove(app, session_id: int, dev: int | None) -> dict:
    async with app.state.pool.connection() as conn:
        session = await _get(conn, session_id)
        if session["phase"] != "REGISTER":
            raise HTTPException(409, "chỉ xóa được thiết bị khi phiên đang ở giai đoạn đăng ký (REGISTER)")
        if dev is None:
            rows = await (await conn.execute(Q["clear_devices"], {"session_id": session_id})).fetchall()
        else:
            rows = await (await conn.execute(Q["remove_device"], {"session_id": session_id, "dev": dev})).fetchall()
            if not rows:
                raise HTTPException(404, f"thiết bị {dev:02d} chưa đăng ký trong phiên {session_id}")
    for r in rows:
        # The device listens on its ack topic already, so no new subscription is needed:
        # seq null + code REMOVED = "you are no longer registered, ask for the MSSV again".
        await publish(app, f"ars/{session['room']}/dev/{r['device_no']:02d}/ack", {"seq": None, "code": "REMOVED"})
        await app.state.hub.send(session_id, {"type": "removed", "dev": r["device_no"], "mssv": r["mssv"]})
    log.info("session %s: removed %d device(s)", session_id, len(rows))
    return {"removed": [r["device_no"] for r in rows]}


@router.websocket("/{session_id}/live")
async def live(ws: WebSocket, session_id: int):
    """Live events for the teacher page (see app/live.py for the event types).

    On connect the page first receives a snapshot, so a page reload never loses the picture:
        {"type": "snapshot", "session": {...}, "devices": [...], "answered": n}
    """
    app = ws.app
    await ws.accept()
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(Q["get_session"], {"session_id": session_id})
        session = await cur.fetchone()
        if session is None:
            await ws.close(code=4404, reason="session not found")
            return
        devices = await (await conn.execute(Q["list_session_devices"], {"session_id": session_id})).fetchall()
        answered = await (await conn.execute(Q["answer_count"], {"session_id": session_id})).fetchone()
        state = await (await conn.execute(Q["state_message"], {"session_id": session_id})).fetchone()
    snapshot = {
        "type": "snapshot",
        "session": {**session, **{k: v for k, v in state.items() if v is not None}},
        "devices": devices,
        "answered": answered["answered"],
    }
    await ws.send_text(json.dumps(snapshot, default=str))   # default=str turns datetimes into text
    app.state.hub.add(session_id, ws)
    try:
        while True:
            await ws.receive_text()      # we do not expect messages; this just waits for disconnect
    except WebSocketDisconnect:
        pass
    finally:
        app.state.hub.remove(session_id, ws)


@router.post("/{session_id}/start", response_model=SessionOut)
async def start(session_id: int, request: Request):
    return await _transition(request.app, session_id, Event.START)


@router.post("/{session_id}/next", response_model=SessionOut)
async def next_question(session_id: int, request: Request):
    return await _transition(request.app, session_id, Event.NEXT)


@router.post("/{session_id}/close", response_model=SessionOut)
async def close(session_id: int, request: Request):
    return await _transition(request.app, session_id, Event.CLOSE)


@router.post("/{session_id}/end", response_model=SessionOut)
async def end(session_id: int, request: Request):
    return await _transition(request.app, session_id, Event.END)


# ------------------------------------------------------------------ helpers

async def _get(conn, session_id: int) -> dict:
    cur = await conn.execute(Q["get_session"], {"session_id": session_id})
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, f"session {session_id} not found")
    return row


async def _transition(app, session_id: int, event: Event) -> dict:
    async with app.state.pool.connection() as conn:
        session = await _get(conn, session_id)

        # 1. state machine check (clear error message for the teacher)
        try:
            next_phase(session["phase"], event)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc))

        # 2. guarded UPDATE
        params = {"session_id": session_id, "q": session["current_q"]}
        query = {
            Event.START: "start_session",
            Event.NEXT: "open_next_question",
            Event.CLOSE: "close_question",
            Event.END: "end_session",
        }[event]
        cur = await conn.execute(Q[query], params)
        row = await cur.fetchone()
        if row is None:
            detail = "no more questions: end the session" if event == Event.NEXT else "session changed, try again"
            raise HTTPException(409, detail)

        # timers: a manual close/end cancels the running timer; opening a question starts one
        _cancel_timer(app, session_id)
        if event == Event.NEXT:
            delay = (row["q_deadline"] - datetime.now(timezone.utc)).total_seconds()
            app.state.timers[session_id] = asyncio.create_task(
                _auto_close(app, session_id, session["room"], row["current_q"], delay)
            )

        # 3. publish the new state (retained)
        await publish_state(app, conn, session_id, session["room"])
        return await _get(conn, session_id)


def _cancel_timer(app, session_id: int) -> None:
    task = app.state.timers.pop(session_id, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def _auto_close(app, session_id: int, room: str, q: int, delay: float) -> None:
    """Close question q when its deadline passes — but only if q is still the open question."""
    await asyncio.sleep(max(0.0, delay) + 0.2)   # small margin so the deadline has really passed
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(Q["close_question"], {"session_id": session_id, "q": q})
        if await cur.fetchone():
            await publish_state(app, conn, session_id, room)
            log.info("session %s: question %s closed automatically", session_id, q)
    app.state.timers.pop(session_id, None)


async def resume_timers(app) -> None:
    """Called once at startup. Auto-close timers live in memory, so a restart of the server
    (crash, --reload, docker restart) would leave an open question open forever.
    Re-create a timer for every open question; one whose deadline has passed closes at once."""
    for _ in range(50):                       # wait up to 10 s for MQTT, so the new state is published
        if app.state.mqtt is not None:
            break
        await asyncio.sleep(0.2)
    async with app.state.pool.connection() as conn:
        rows = await (await conn.execute(Q["open_questions"])).fetchall()
    for r in rows:
        app.state.timers[r["id"]] = asyncio.create_task(
            _auto_close(app, r["id"], r["room"], r["current_q"], r["delay_s"])
        )
        log.info("session %s: resumed the timer of question %s (%.1f s left)", r["id"], r["current_q"], max(0, r["delay_s"]))

