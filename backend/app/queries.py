"""Loads every SQL statement from sql/queries.sql into a dictionary.

Each block in that file starts with a line like `-- name: create_quiz`. Keeping SQL in a
.sql file (instead of long strings inside Python) means you can read, test and review
all queries in one place — and copy any block straight into psql.

Usage:
    from app.queries import Q
    await conn.execute(Q["get_quiz"], {"quiz_id": 1})
"""
import re
from pathlib import Path

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "queries.sql"


def load_queries(path: Path = SQL_FILE) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    queries: dict[str, str] = {}
    # split the file at every "-- name: xxx" line
    parts = re.split(r"^-- name:\s*(\w+).*$", text, flags=re.MULTILINE)
    # parts = [header, name1, body1, name2, body2, ...]
    for name, body in zip(parts[1::2], parts[2::2]):
        lines = [line for line in body.splitlines() if not line.lstrip().startswith("--")]
        sql = "\n".join(lines).strip()
        queries[name] = sql
    return queries


Q = load_queries()
