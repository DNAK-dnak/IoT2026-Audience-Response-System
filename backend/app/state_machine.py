"""The session state machine.

A session is always in exactly one phase. Only the transitions listed in TRANSITIONS
are allowed; everything else is rejected. This file has no database and no MQTT code,
so it is trivial to test (tests/test_state_machine.py).

    IDLE --start--> REGISTER --next--> QUESTION_OPEN --close--> QUESTION_CLOSED
                       |                    ^                        |   |
                       |                    +----------next----------+   |
                       +--end--> ENDED <----------------end--------------+

The SQL UPDATEs in sql/queries.sql repeat the same rules as guards
(`WHERE phase = ...`), so even two clicks racing cannot break the order.
"""
from enum import StrEnum


class Phase(StrEnum):
    IDLE = "IDLE"
    REGISTER = "REGISTER"
    QUESTION_OPEN = "QUESTION_OPEN"
    QUESTION_CLOSED = "QUESTION_CLOSED"
    ENDED = "ENDED"


class Event(StrEnum):
    START = "start"
    NEXT = "next"
    CLOSE = "close"
    END = "end"


# (current phase, event) -> new phase
TRANSITIONS: dict[tuple[Phase, Event], Phase] = {
    (Phase.IDLE, Event.START): Phase.REGISTER,
    (Phase.REGISTER, Event.NEXT): Phase.QUESTION_OPEN,
    (Phase.QUESTION_CLOSED, Event.NEXT): Phase.QUESTION_OPEN,
    (Phase.QUESTION_OPEN, Event.CLOSE): Phase.QUESTION_CLOSED,
    (Phase.QUESTION_CLOSED, Event.END): Phase.ENDED,
    (Phase.REGISTER, Event.END): Phase.ENDED,
}


class InvalidTransition(Exception):
    """Raised when an event is not allowed in the current phase."""


def next_phase(current: str | Phase, event: str | Event) -> Phase:
    key = (Phase(current), Event(event))
    if key not in TRANSITIONS:
        allowed = sorted(e.value for (p, e) in TRANSITIONS if p == key[0])
        raise InvalidTransition(
            f"'{key[1].value}' is not allowed in phase {key[0].value}; allowed: {allowed or 'nothing'}"
        )
    return TRANSITIONS[key]
