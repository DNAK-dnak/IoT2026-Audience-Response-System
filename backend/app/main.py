from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from app.db import create_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting up...")
    pool = create_pool()
    await pool.open()
    app.state.pool = pool
    print("Pool created and opened.")
    yield
    await pool.close()
    print("Pool closed.")
    print("Shutting down...")

app = FastAPI(lifespan=lifespan)

@app.get("/hello")
async def hello():
    return {"message": "hi"}

@app.get("/health")
async def health(request: Request):
    pool = request.app.state.pool
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1")
        row = await cur.fetchone()
    return {"db": "ok", "result": row[0]}