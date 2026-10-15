"""Teacher login and password change for the teacher page.

Passwords are stored ONLY as a scrypt hash (Python standard library, no extra package):
    "scrypt$<n>$<r>$<p>$<salt hex>$<hash hex>"
scrypt is deliberately slow and memory-hungry, so a stolen table cannot be brute-forced
cheaply; the random salt makes two equal passwords hash differently.

Brute-force limits (two layers, both on the server):
  - per account: MAX_FAILS wrong passwords in a row lock it for LOCK_MINUTES (reason "LOCKED")
  - per IP:      more than IP_MAX_FAILS failures in IP_WINDOW_S seconds -> 429 for that IP

Answers follow BACKEND_API.md:
  POST /auth/login     {id, password}            -> {ok:true, user:{id,name}} | {ok:false, reason:"BAD"|"LOCKED"}
  POST /auth/password  {id, old_password, new_password}
                                                 -> {ok:true} | {ok:false, reason:"BAD_OLD"|"WEAK"|"LOCKED"}
A wrong id and a wrong password give the same answer ("BAD") and take the same time,
so the page cannot be used to find out which teacher ids exist.

Note: the other endpoints are not protected by this login yet (local demo network).
"""
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.queries import Q

router = APIRouter(prefix="/auth", tags=["auth"])

N, R, P = 2 ** 14, 8, 1           # scrypt cost: ~16 MB and ~50 ms per check
MAX_FAILS = 5
LOCK_MINUTES = 5
IP_MAX_FAILS = 20
IP_WINDOW_S = 600


# ------------------------------------------------------------------ hashing

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        _, n, r, p, salt, digest = (stored or "").split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(digest) // 2)
        return hmac.compare_digest(test.hex(), digest)     # constant-time comparison
    except ValueError:
        return False


_DUMMY = hash_password(secrets.token_hex(8))   # checked for unknown ids, so they take the same time


def strong_enough(pw: str) -> bool:
    """Same rule as the page: at least 6 characters, with letters AND digits."""
    return len(pw) >= 6 and re.search(r"[A-Za-z]", pw) is not None and re.search(r"\d", pw) is not None


# ------------------------------------------------------------------ per-IP limit (in memory)

_ip_fails: dict[str, deque] = defaultdict(deque)


def _ip_check(ip: str) -> None:
    q = _ip_fails[ip]
    while q and q[0] < time.monotonic() - IP_WINDOW_S:
        q.popleft()
    if len(q) >= IP_MAX_FAILS:
        raise HTTPException(429, "Quá nhiều lần đăng nhập sai từ máy này. Thử lại sau ít phút.")


def _ip_fail(ip: str) -> None:
    _ip_fails[ip].append(time.monotonic())


# ------------------------------------------------------------------ endpoints

class LoginIn(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


class PasswordIn(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


async def _check(conn, login_id: str, password: str, ip: str) -> tuple[dict | None, str | None]:
    """Returns (teacher, None) when the password is right, else (None, "BAD" | "LOCKED")."""
    _ip_check(ip)
    t = await (await conn.execute(Q["teacher_by_login"], {"login_id": login_id})).fetchone()
    if t is None:
        verify_password(password, _DUMMY)
        _ip_fail(ip)
        return None, "BAD"
    if t["locked"]:
        return None, "LOCKED"
    if not verify_password(password, t["password_hash"]):
        _ip_fail(ip)
        await conn.execute(Q["login_failed"], {"id": t["id"], "max_fails": MAX_FAILS, "lock_minutes": LOCK_MINUTES})
        return None, "BAD"
    await conn.execute(Q["login_ok"], {"id": t["id"]})
    return t, None


@router.post("/login")
async def login(body: LoginIn, request: Request):
    async with request.app.state.pool.connection() as conn:
        t, reason = await _check(conn, body.id.strip().upper(), body.password, request.client.host)
    if t is None:
        return {"ok": False, "reason": reason}
    return {"ok": True, "user": {"id": t["login_id"], "name": t["name"]}}


@router.post("/password")
async def change_password(body: PasswordIn, request: Request):
    async with request.app.state.pool.connection() as conn:
        t, reason = await _check(conn, body.id.strip().upper(), body.old_password, request.client.host)
        if t is None:
            return {"ok": False, "reason": "LOCKED" if reason == "LOCKED" else "BAD_OLD"}
        if not strong_enough(body.new_password) or body.new_password == body.old_password:
            return {"ok": False, "reason": "WEAK"}
        await conn.execute(Q["set_password"], {"id": t["id"], "hash": hash_password(body.new_password)})
    return {"ok": True}
