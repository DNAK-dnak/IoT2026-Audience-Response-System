"""Quiz endpoints: create a quiz with its questions, read it back, list quizzes."""
import psycopg
from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb

from app.models import QuizIn, QuizOut
from app.queries import Q

router = APIRouter(prefix="/quizzes", tags=["quizzes"])


@router.post("", response_model=QuizOut, status_code=201)
async def create_quiz(quiz: QuizIn, request: Request):
    pool = request.app.state.pool
    async with pool.connection() as conn:
        try:
            # ONE transaction: the quiz and all its questions are saved together, or nothing is
            async with conn.transaction():
                cur = await conn.execute(
                    Q["create_quiz"],
                    {"course_id": quiz.course_id, "title": quiz.title, "subject": quiz.subject},
                )
                quiz_id = (await cur.fetchone())["id"]
                for position, question in enumerate(quiz.questions, start=1):
                    params = question.model_dump()
                    params.update(
                        quiz_id=quiz_id,
                        position=position,
                        n_choices=len(question.options),
                        options=Jsonb(question.options),   # Python list -> JSONB column
                    )
                    await conn.execute(Q["add_question"], params)
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(status_code=404, detail=f"course {quiz.course_id} does not exist")
        return await _read_quiz(conn, quiz_id)


@router.get("", response_model=list[QuizOut])
async def list_quizzes(request: Request):
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(Q["list_quizzes"])
        return await cur.fetchall()


@router.get("/{quiz_id}", response_model=QuizOut)
async def get_quiz(quiz_id: int, request: Request):
    async with request.app.state.pool.connection() as conn:
        return await _read_quiz(conn, quiz_id)


async def _read_quiz(conn, quiz_id: int) -> dict:
    cur = await conn.execute(Q["get_quiz"], {"quiz_id": quiz_id})
    quiz = await cur.fetchone()
    if quiz is None:
        raise HTTPException(status_code=404, detail=f"quiz {quiz_id} not found")
    cur = await conn.execute(Q["get_questions"], {"quiz_id": quiz_id})
    quiz["questions"] = await cur.fetchall()
    return quiz
