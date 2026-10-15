"""FastAPI application: startup/shutdown (lifespan) and the list of routers.

Start from the backend folder:
    python -m uvicorn app.main:app --reload --loop asyncio:SelectorEventLoop
Then open http://127.0.0.1:8000/docs
"""
import asyncio
import contextlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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

# Pages are sent with a version on every local script / stylesheet:  ars-client.js?v=<last change>.
# A browser that cached an old ars-client.js (before the no-cache rule below existed) sees a new
# address and downloads it; after every edit of a file its version, and so its address, changes.
_ASSET = re.compile(r'(\b(?:src|href)=")([^":?#]+\.(?:js|css))(")')


def _versioned(match: re.Match) -> str:
    f = WEB_DIR / match.group(2)
    if not f.is_file():
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}?v={f.stat().st_mtime_ns}{match.group(3)}"


@app.get("/ui/", include_in_schema=False)
@app.get("/ui/{page}.html", include_in_schema=False)
async def ui_page(page: str = "index"):
    f = WEB_DIR / f"{page}.html"
    if "/" in page or "\\" in page or not f.is_file():
        raise HTTPException(404)
    html = _ASSET.sub(_versioned, f.read_text(encoding="utf-8"))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")


@app.middleware("http")
async def ui_no_stale_cache(request: Request, call_next):
    """The teacher page's files change while we develop (index.html, ars-client.js ...).
    "no-cache" = the browser may keep a copy but must ask the server before using it.
    Unchanged files cost only a tiny "304 Not Modified"; a changed file is downloaded at once,
    so nobody ever runs an old ars-client.js again."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/ui"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
async def root():
    """Open the teacher page at an address that changes whenever a page file changes
    (/ui/?v=<last change>). A browser that still holds an old copy of /ui/ in its cache
    cannot reuse it for the new address, so http://<server>:8000/ always shows the current page."""
    files = [f for f in WEB_DIR.iterdir() if f.suffix in (".html", ".js", ".css")]
    version = max((f.stat().st_mtime_ns for f in files), default=0)
    return RedirectResponse(f"/ui/?v={version}", headers={"Cache-Control": "no-store"})


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
