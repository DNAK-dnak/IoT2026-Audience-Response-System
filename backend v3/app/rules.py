"""Validation rules for messages from the clickers.

Pure functions: they receive plain values and return an ack code. No database, no MQTT.
That makes them easy to test (tests/test_rules.py) and easy to reason about.

Ack codes (from the contract):
    OK            accepted
    BAD           malformed message (not JSON, missing/wrong fields, dev mismatch, bad choice)
    UNREGISTERED  answer from a device that has not registered in this session
    CLOSED        not accepted in the current phase / wrong question / after the deadline
    DUP           duplicate or older seq; or MSSV already used on another device
    NOT_IN_CLASS  the MSSV is not on the class list linked to the session
"""
import re
from datetime import datetime
from typing import Any

MSSV_PATTERN = re.compile(r"^[0-9]{8}$")


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python (True == 1), so exclude it explicitly
    return isinstance(value, int) and not isinstance(value, bool)


def check_register(
    payload: Any, dev_from_topic: int, phase: str, mssv_owner: int | None, in_class: bool = True
) -> str:
    """Decide what to do with a register message.

    payload:        parsed JSON (None if it was not valid JSON)
    dev_from_topic: device number taken from the topic (ars/room1/dev/07/register -> 7)
    phase:          current session phase
    mssv_owner:     device that already holds this MSSV in the session (None = nobody)
    in_class:       False if the session has a class list and this MSSV is not on it
                    (True when the session has no class list)
    """
    # 1. BAD: shape of the message
    if not isinstance(payload, dict):
        return "BAD"
    dev, mssv, seq = payload.get("dev"), payload.get("mssv"), payload.get("seq")
    if not (_is_int(dev) and _is_int(seq) and isinstance(mssv, str)):
        return "BAD"
    if dev != dev_from_topic or seq < 1 or not MSSV_PATTERN.match(mssv):
        return "BAD"

    # 2. CLOSED: registration is open during REGISTER. During the quiz, a device may only
    #    RE-register itself with the same MSSV (e.g. after a reboot).
    reconnect = mssv_owner == dev
    if phase != "REGISTER" and not (reconnect and phase in ("QUESTION_OPEN", "QUESTION_CLOSED")):
        return "CLOSED"

    # 3. NOT_IN_CLASS: the session checks a class list and this student is not on it
    if not in_class:
        return "NOT_IN_CLASS"

    # 4. DUP: this student is already registered on a different device
    if mssv_owner is not None and mssv_owner != dev:
        return "DUP"

    return "OK"


def check_answer(payload: Any, dev_from_topic: int, ctx: dict | None, received_at: datetime) -> str:
    """Decide what to do with an answer message.

    ctx is the row from the `answer_context` query:
        phase, current_q, q_deadline, question_id, n_choices, registered, last_seq
    received_at comes from raw_events (database clock) — the only clock we trust.
    """
    # 1. BAD: shape of the message
    if not isinstance(payload, dict):
        return "BAD"
    dev, q, choice, seq = (payload.get(k) for k in ("dev", "q", "choice", "seq"))
    if not all(_is_int(v) for v in (dev, q, choice, seq)):
        return "BAD"
    if dev != dev_from_topic or q < 1 or seq < 1 or choice < 1:
        return "BAD"
    if ctx is None:
        return "CLOSED"  # no session context at all
    if ctx["question_id"] is not None and choice > ctx["n_choices"]:
        return "BAD"

    # 2. UNREGISTERED
    if not ctx["registered"]:
        return "UNREGISTERED"

    # 3. DUP: not newer than the last accepted message from this device.
    #    Checked BEFORE "closed": a resend of a message we already stored (its ack was lost)
    #    may arrive after the deadline; it must say "already have it" (DUP), not CLOSED,
    #    otherwise the device thinks its answer was refused. Found with fake_devices.py --test all.
    if seq <= ctx["last_seq"]:
        return "DUP"

    # 4. CLOSED: wrong phase, not the current question, or too late
    if ctx["phase"] != "QUESTION_OPEN" or q != ctx["current_q"] or ctx["question_id"] is None:
        return "CLOSED"
    if received_at > ctx["q_deadline"]:
        return "CLOSED"

    return "OK"
