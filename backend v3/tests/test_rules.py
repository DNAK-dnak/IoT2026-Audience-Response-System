from datetime import datetime, timedelta, timezone

import pytest

from app.rules import check_answer, check_register

NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


def ctx(**overrides):
    """A valid answer context: question 1 open, device registered, last_seq 1."""
    base = {
        "phase": "QUESTION_OPEN",
        "current_q": 1,
        "q_deadline": NOW + timedelta(seconds=20),
        "question_id": 10,
        "n_choices": 4,
        "registered": True,
        "last_seq": 1,
    }
    base.update(overrides)
    return base


def answer(**overrides):
    base = {"dev": 7, "q": 1, "choice": 2, "seq": 2}
    base.update(overrides)
    return base


# ---------------- answers ----------------

def test_valid_answer_is_ok():
    assert check_answer(answer(), 7, ctx(), NOW) == "OK"


@pytest.mark.parametrize(
    "payload",
    [
        None,                          # not JSON
        "hello",                       # JSON but not an object
        {"dev": 7, "q": 1, "seq": 2},  # missing choice
        answer(choice="2"),            # text instead of number
        answer(choice=True),           # bool is not a number here
        answer(choice=7),              # 7 of 4 choices
        answer(choice=0),
        answer(dev=8),                 # payload says device 8, topic says 7
        answer(seq=0),
    ],
)
def test_bad_answers(payload):
    assert check_answer(payload, 7, ctx(), NOW) == "BAD"


def test_unregistered_device():
    assert check_answer(answer(), 7, ctx(registered=False, last_seq=None), NOW) == "UNREGISTERED"


def test_question_closed():
    assert check_answer(answer(), 7, ctx(phase="QUESTION_CLOSED"), NOW) == "CLOSED"


def test_answer_for_another_question():
    assert check_answer(answer(q=1), 7, ctx(current_q=2), NOW) == "CLOSED"


def test_one_millisecond_late():
    late = NOW + timedelta(seconds=20, milliseconds=1)
    assert check_answer(answer(), 7, ctx(), late) == "CLOSED"


def test_exactly_on_deadline_is_accepted():
    assert check_answer(answer(), 7, ctx(), NOW + timedelta(seconds=20)) == "OK"


def test_same_seq_twice_is_dup():
    assert check_answer(answer(seq=5), 7, ctx(last_seq=5), NOW) == "DUP"


def test_older_seq_is_dup():
    assert check_answer(answer(seq=3), 7, ctx(last_seq=5), NOW) == "DUP"


def test_changed_answer_with_higher_seq_is_ok():
    assert check_answer(answer(choice=3, seq=6), 7, ctx(last_seq=5), NOW) == "OK"


# ---------------- registration ----------------

def reg(**overrides):
    base = {"dev": 7, "mssv": "22134007", "seq": 1}
    base.update(overrides)
    return base


def test_register_ok():
    assert check_register(reg(), 7, "REGISTER", None) == "OK"


@pytest.mark.parametrize(
    "payload",
    [None, reg(mssv=22134007), reg(mssv="2213"), reg(mssv="abcdefgh"), reg(dev=8), reg(seq=0)],
)
def test_register_bad(payload):
    assert check_register(payload, 7, "REGISTER", None) == "BAD"


def test_register_when_quiz_not_started():
    assert check_register(reg(), 7, "IDLE", None) == "CLOSED"


def test_new_student_cannot_register_during_quiz():
    assert check_register(reg(), 7, "QUESTION_OPEN", None) == "CLOSED"


def test_same_device_can_reregister_during_quiz():
    assert check_register(reg(), 7, "QUESTION_OPEN", 7) == "OK"


def test_student_already_on_another_device():
    assert check_register(reg(), 7, "REGISTER", 3) == "DUP"


def test_student_not_in_class_list():
    assert check_register(reg(), 7, "REGISTER", None, in_class=False) == "NOT_IN_CLASS"


def test_bad_mssv_is_bad_even_if_not_in_class():
    # shape errors are reported first: the device must fix the message itself
    assert check_register(reg(mssv="12"), 7, "REGISTER", None, in_class=False) == "BAD"


def test_not_in_class_is_checked_before_dup():
    assert check_register(reg(), 7, "REGISTER", 3, in_class=False) == "NOT_IN_CLASS"


def test_resend_after_deadline_is_dup_not_closed():
    # first copy was stored on time, its ack was lost, the resend arrives late
    late = NOW + timedelta(seconds=25)
    assert check_answer(answer(seq=5), 7, ctx(last_seq=5), late) == "DUP"


def test_new_answer_after_deadline_is_still_closed():
    late = NOW + timedelta(seconds=25)
    assert check_answer(answer(seq=6), 7, ctx(last_seq=5), late) == "CLOSED"
