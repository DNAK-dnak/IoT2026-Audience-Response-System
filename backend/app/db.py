from psycopg_pool import AsyncConnectionPool

from app.config import settings


def create_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(settings.database_url, open=False)