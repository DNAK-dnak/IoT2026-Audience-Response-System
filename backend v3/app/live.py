"""Live updates for the teacher page, over WebSocket.

The page opens   ws://<server>/sessions/<id>/live   and receives JSON events
(first a "snapshot", see app/routers/sessions.py):

    {"type": "state",   "phase": "QUESTION_OPEN", "q": 1, "n_choices": 4, "deadline_ms": 20000}
    {"type": "device",  "dev": 7, "mssv": "22134007", "full_name": "..."}      a device registered
    {"type": "answer",  "dev": 7, "q": 1, "answered": 12}                      someone answered (no choice shown while open)
    {"type": "ack",     "dev": 7, "kind": "register", "code": "NOT_IN_CLASS"}  every rejected message, for the teacher to see

The Hub keeps the open sockets per session and sends each event to all of them.
A socket that fails to send (browser closed) is simply dropped.
"""
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

log = logging.getLogger("ars.live")


class Hub:
    def __init__(self) -> None:
        self.sockets: dict[int, set[WebSocket]] = defaultdict(set)

    def add(self, session_id: int, ws: WebSocket) -> None:
        self.sockets[session_id].add(ws)

    def remove(self, session_id: int, ws: WebSocket) -> None:
        self.sockets[session_id].discard(ws)

    async def send(self, session_id: int, event: dict) -> None:
        text = json.dumps(event, default=str, separators=(",", ":"))
        for ws in list(self.sockets.get(session_id, ())):
            try:
                await ws.send_text(text)
            except Exception:
                self.remove(session_id, ws)
