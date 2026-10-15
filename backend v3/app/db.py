"""Database connection pool.

A pool keeps a few connections open and lends them to requests, so no request pays
the cost of connecting. It is created here but OPENED in the lifespan (app/main.py).

- row_factory=dict_row: rows come back as dicts ({"id": 1, ...}) instead of tuples.
- autocommit=True: every statement is saved immediately. When several statements must
  succeed or fail together, wrap them in `async with conn.transaction():`.
"""
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings


def create_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(
        settings.database_url,
        open=False,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )
