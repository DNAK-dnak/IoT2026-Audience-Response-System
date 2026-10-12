"""FastAPI application: startup/shutdown (lifespan) and the list of routers.

Start from the backend folder:
    python -m uvicorn app.main:app --reload --loop asyncio:SelectorEventLoop
Then open http://127.0.0.1:8000/docs
"""
import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import create_pool
from app.live import Hub
from app.mqtt import run_worker
from app import analyze, auth
from app.routers import classes, quizzes, results, sessions

WEB_DIR = Path(__file__).resolve().parents[1] / "web"   # the teacher page lives here

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ars")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    pool = create_pool()
    await pool.open()
    app.state.pool = pool
    app.state.mqtt = None        # set by the worker once connected
    app.state.timers = {}        # session_id -> auto-close task
    app.state.hub = Hub()        # WebSocket connections of teacher pages
    worker = asyncio.create_task(run_worker(app))
    resume = asyncio.create_task(sessions.resume_timers(app))   # open questions survive a restart
    log.info("DB pool opened, MQTT worker started")

    yield                        # ---- the app serves requests here ----

    # ---- shutdown ----
    for task in list(app.state.timers.values()) + [worker, resume]:
        task.cancel()
    for task in list(app.state.timers.values()) + [worker, resume]:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await pool.close()
    log.info("DB pool closed")


app = FastAPI(title="ARS backend", lifespan=lifespan)
app.include_router(quizzes.router)
app.include_router(classes.router)
app.include_router(sessions.router)
app.include_router(results.router)
app.include_router(auth.router)

# Serve the teacher page from the same server: http://<server>:8000/ui/
# Same origin = the page can call the API and the WebSocket without any CORS setup.
WEB_DIR.mkdir(exist_ok=True)
app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui/")


@app.get("/health", tags=["health"])
async def health(request: Request):
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 AS ok")
        row = await cur.fetchone()
    return {
        "db": "ok" if row["ok"] == 1 else "error",
        "mqtt": "connected" if request.app.state.mqtt else "disconnected",
        "analyze": analyze.enabled(),     # false = no ANTHROPIC_API_KEY: the page hides the AI step
    }
