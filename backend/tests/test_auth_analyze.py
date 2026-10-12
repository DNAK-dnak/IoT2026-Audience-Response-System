"""Round 4: password hashing rules and what the AI analysis is allowed to see."""
from app.analyze import minimise
from app.auth import hash_password, strong_enough, verify_password


def test_hash_roundtrip_and_salt():
    h1, h2 = hash_password("abc123"), hash_password("abc123")
    assert h1 != h2                                   # random salt
    assert verify_password("abc123", h1) and verify_password("abc123", h2)
    assert not verify_password("abc124", h1)


def test_verify_rejects_garbage():
    assert not verify_password("x", None)
    assert not verify_password("x", "")
    assert not verify_password("x", "plain-text-password")


def test_password_rule_matches_the_page():
    assert strong_enough("abc123")
    assert not strong_enough("123456")                # digits only
    assert not strong_enough("abcdef")                # letters only
    assert not strong_enough("ab12")                  # too short


def test_minimise_drops_names_and_teacher():
    payload = {
        "subject": "CTDL", "exam": "Đề 1", "teacher": "GV001", "class_code": "DH22CNTT01",
        "stats": {"avg": 6.0},
        "questions": [{"n": 1, "question": "Q?", "options": ["a", "b", "c", "d"], "answer": "B",
                       "correct_rate": 0.5, "dist": {"A": 1, "B": 1, "none": 0}}],
        "students": [{"dev": 7, "mssv": "22134007", "name": "Nguyễn Văn A", "answers": ["B", None],
                      "correct": 1, "missing": 1, "score": 5, "fixes": [{"q": 2, "by": "GV001"}]}],
    }
    out = minimise(payload)
    text = str(out)
    assert "Nguyễn" not in text and "GV001" not in text and "DH22CNTT01" not in text
    assert out["sinh_vien"][0] == {"mssv": "22134007", "tra_loi": "B-", "dung": 1, "bo_trong": 1,
                                   "diem": 5, "cau_da_sua_tay": [2]}
    assert out["cau_hoi"][0]["phuong_an"] == {"A": "a", "B": "b", "C": "c", "D": "d"}
