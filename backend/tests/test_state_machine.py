import pytest

from app.state_machine import InvalidTransition, Phase, next_phase


@pytest.mark.parametrize(
    "current, event, expected",
    [
        ("IDLE", "start", Phase.REGISTER),
        ("REGISTER", "next", Phase.QUESTION_OPEN),
        ("QUESTION_OPEN", "close", Phase.QUESTION_CLOSED),
        ("QUESTION_CLOSED", "next", Phase.QUESTION_OPEN),
        ("QUESTION_CLOSED", "end", Phase.ENDED),
        ("REGISTER", "end", Phase.ENDED),
    ],
)
def test_allowed_transitions(current, event, expected):
    assert next_phase(current, event) == expected


@pytest.mark.parametrize(
    "current, event",
    [
        ("IDLE", "next"),
        ("IDLE", "end"),
        ("REGISTER", "close"),
        ("QUESTION_OPEN", "next"),
        ("QUESTION_OPEN", "end"),
        ("ENDED", "start"),
        ("ENDED", "next"),
    ],
)
def test_rejected_transitions(current, event):
    with pytest.raises(InvalidTransition):
        next_phase(current, event)


def test_unknown_phase_is_an_error():
    with pytest.raises(ValueError):
        next_phase("FLYING", "start")
