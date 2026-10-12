"""Classes and their student lists (for the MSSV check at registration).

Today the teacher uploads the list: the page reads the Excel/CSV file (it already does this
for questions) and sends the rows as JSON. `source` records where a list came from, so an
automatic sync from a school system can be added later as another writer of the same tables.
"""
from fastapi import APIRouter, HTTPException, Request

from app.models import ClassIn, ClassOut
from app.queries import Q

router = APIRouter(prefix="/classes", tags=["classes"])


@router.post("", response_model=ClassOut, status_code=201)
async def upload_class(body: ClassIn, request: Request):
    """Create or update a class and its student list.
    mode=replace (default): the new list replaces the old one. mode=append: adds/updates students."""
    async with request.app.state.pool.connection() as conn:
        async with conn.transaction():   # the whole list is saved, or nothing
            cur = await conn.execute(Q["upsert_class"], {"code": body.code, "name": body.name, "source": body.source})
            cls = await cur.fetchone()
            if body.mode == "replace":
                await conn.execute(Q["clear_class_students"], {"class_id": cls["id"]})
            for st in body.students:
                await conn.execute(
                    Q["add_class_student"],
                    {"class_id": cls["id"], "mssv": st.mssv, "full_name": st.full_name},
                )
        return await _class_out(conn, cls["id"])


@router.get("", response_model=list[ClassOut])
async def list_classes(request: Request):
    async with request.app.state.pool.connection() as conn:
        return await (await conn.execute(Q["list_classes"])).fetchall()


@router.get("/{class_id}/students")
async def class_students(class_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        await _class_out(conn, class_id)
        return await (await conn.execute(Q["get_class_students"], {"class_id": class_id})).fetchall()


async def _class_out(conn, class_id: int) -> dict:
    rows = await (await conn.execute(Q["list_classes"])).fetchall()
    for row in rows:
        if row["id"] == class_id:
            return row
    raise HTTPException(404, f"class {class_id} not found")
