"""Request and response shapes for the REST API (pydantic models).

FastAPI uses these to (1) validate incoming JSON — a bad request gets a 422 error
listing the exact field — and (2) document the API in /docs automatically.

Answers are numbers 1-4 everywhere in the backend and on the devices.
The teacher page shows them as letters A-D (A=1, B=2, C=3, D=4).
"""
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

N_OPTIONS = 4   # agreed: always 4 options (A-D)


class QuestionIn(BaseModel):
    text: str = Field(min_length=1)
    options: list[str] = Field(min_length=N_OPTIONS, max_length=N_OPTIONS)
    correct_choice: int = Field(ge=1, le=N_OPTIONS)
    time_limit_s: int = Field(default=20, ge=5, le=600)
    review_delay_s: int = Field(default=8, ge=0, le=120)

    @model_validator(mode="after")
    def options_not_empty(self):
        if any(not o.strip() for o in self.options):
            raise ValueError("all 4 options must have text")
        return self


class QuizIn(BaseModel):
    course_id: int = 1
    title: str = Field(min_length=1)
    subject: str | None = None
    questions: list[QuestionIn] = Field(min_length=1)


class QuestionOut(BaseModel):
    id: int
    position: int
    text: str
    options: list[str]
    n_choices: int
    correct_choice: int
    time_limit_s: int
    review_delay_s: int


class QuizOut(BaseModel):
    id: int
    course_id: int
    title: str
    subject: str | None = None
    created_at: datetime
    questions: list[QuestionOut] = []


class SessionIn(BaseModel):
    quiz_id: int
    # the room becomes part of MQTT topics, so only safe characters are allowed
    room: str = Field(default="room1", pattern=r"^[a-z0-9_-]{1,32}$")
    class_id: int | None = None   # set it to check student IDs against that class list


class SessionOut(BaseModel):
    id: int
    quiz_id: int
    class_id: int | None
    room: str
    phase: str
    current_q: int | None
    q_opened_at: datetime | None
    q_deadline: datetime | None
    registered: int
    n_questions: int


class StudentIn(BaseModel):
    mssv: str = Field(pattern=r"^[0-9]{8}$")
    full_name: str | None = None


class ClassIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)   # e.g. "DH22CNTT01"
    name: str | None = None
    students: list[StudentIn] = Field(min_length=1)
    mode: str = Field(default="replace", pattern="^(replace|append)$")
    source: str = Field(default="upload", pattern="^(upload|school_sync)$")


class ClassOut(BaseModel):
    id: int
    code: str
    name: str | None
    source: str
    updated_at: datetime
    students: int
