"""AI analysis of a finished test ("Phân tích kết quả bằng AI" on the teacher page).

The API key stays on the SERVER (.env), the browser never sees it. Two providers:
    GEMINI_API_KEY=...      Google Gemini (the prompt and call come from the team's ai_server.py)
    ANTHROPIC_API_KEY=...   Claude
Both set: AI_PROVIDER=gemini|anthropic chooses (empty = Gemini). No key: the feature is off,
/health reports "analyze": false and ars-client.js hides ARS.analyze, so the page skips the step.

Only what the analysis needs is sent to the model: question texts, answer distributions,
and per student the MSSV, answers and score. Names, the teacher id and the class code
are removed (data minimisation).
"""
import json

import httpx

from app.config import settings

LETTERS = "ABCD"

SYSTEM = (
    "Bạn là trợ lý phân tích kết quả kiểm tra trắc nghiệm cho giảng viên đại học Việt Nam. "
    "Viết bằng tiếng Việt, ngắn gọn, cụ thể, dựa hoàn toàn vào số liệu được cung cấp; không bịa số liệu. "
    "Định dạng: tiêu đề bắt đầu bằng '## ', gạch đầu dòng bằng '- ', chữ đậm bằng **...**. Không dùng bảng."
)

PROMPT = """Dưới đây là kết quả một bài kiểm tra (JSON). Hãy viết phân tích gồm các mục:

## Tổng quan lớp
Điểm trung bình, phân bố, tỉ lệ đạt; nhận xét chung trong 2–3 gạch đầu dòng.

## Câu hỏi cần chú ý
Các câu có tỉ lệ đúng thấp (dưới 50%). Với mỗi câu: phương án sai nào bị chọn nhiều nhất và điều đó gợi ý sinh viên đang hiểu sai kiến thức gì.

## Sinh viên cần hỗ trợ
Liệt kê MSSV có điểm dưới 5. Tách riêng những MSSV bỏ trống nhiều câu (có thể do lỗi thiết bị/mất kết nối hơn là không biết), và lưu ý các đáp án đã được giáo viên nhập tay.

## Đề xuất
3–5 đề xuất ôn tập hoặc chỉnh đề cụ thể.

Dữ liệu:
```json
{data}
```"""


class AnalyzeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def provider() -> str | None:
    """Which AI service is used, or None when no key is set."""
    want = settings.ai_provider.strip().lower()
    if want == "anthropic" and settings.anthropic_api_key:
        return "anthropic"
    if want in ("", "gemini") and settings.gemini_api_key:
        return "gemini"
    if settings.anthropic_api_key:
        return "anthropic"
    if settings.gemini_api_key:
        return "gemini"
    return None


def enabled() -> bool:
    return provider() is not None


def minimise(payload: dict) -> dict:
    """Keep only what the model needs. No names, no teacher id, no class code."""
    qs = payload.get("questions") or []
    students = payload.get("students") or []
    return {
        "subject": payload.get("subject"),
        "exam": payload.get("exam"),
        "stats": payload.get("stats"),
        "questions": [
            {
                "n": q.get("n"),
                "question": q.get("question"),
                "options": {LETTERS[i]: o for i, o in enumerate(q.get("options") or []) if i < 4},
                "answer": q.get("answer"),
                "correct_rate": q.get("correct_rate"),
                "dist": q.get("dist"),           # how many chose A/B/C/D, "none" = no answer
            }
            for q in qs
        ],
        "students": [
            {
                "mssv": s.get("mssv"),
                "answers": "".join(a or "-" for a in (s.get("answers") or [])),   # "B-C": "-" = no answer
                "correct": s.get("correct"),
                "missing": s.get("missing"),
                "score": s.get("score"),
                "manual_fixes": [f.get("q") for f in (s.get("fixes") or []) if f.get("manual")],
            }
            for s in students
        ],
    }


async def analyze(payload: dict) -> str:
    which = provider()
    if which is None:
        raise AnalyzeError(503, "Máy chủ chưa cấu hình GEMINI_API_KEY hoặc ANTHROPIC_API_KEY: không phân tích được.")
    text = PROMPT.format(data=json.dumps(minimise(payload), ensure_ascii=False, separators=(",", ":")))
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            if which == "gemini":
                return await _gemini(client, text)
            return await _anthropic(client, text)
    except httpx.HTTPError as exc:
        raise AnalyzeError(502, f"Không kết nối được dịch vụ AI ({which}): {exc.__class__.__name__}")


async def _gemini(client: httpx.AsyncClient, text: str) -> str:
    model = settings.gemini_model
    res = await client.post(
        f"{settings.gemini_base_url.rstrip('/')}/models/{model}:generateContent",
        headers={"x-goog-api-key": settings.gemini_api_key, "content-type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
        },
    )
    if res.status_code != 200:
        hint = {
            400: "Yêu cầu không hợp lệ hoặc API key sai.",
            403: "API key không có quyền hoặc bị chặn.",
            404: f"Không tìm thấy model '{model}'. Đổi GEMINI_MODEL trong .env.",
            429: "Hết hạn mức (quota) của Gemini. Đợi một lúc hoặc đổi GEMINI_MODEL sang gemini-3.5-flash-lite.",
        }.get(res.status_code, "")
        raise AnalyzeError(502, f"Gemini lỗi {res.status_code}. {hint} ({_error_message(res)})")
    data = res.json()
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "không rõ")
        raise AnalyzeError(502, f"Gemini không trả về nội dung (lý do: {reason}).")
    out = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])).strip()
    if not out:
        raise AnalyzeError(502, f"Gemini trả về rỗng (finishReason: {cands[0].get('finishReason')}).")
    return out


async def _anthropic(client: httpx.AsyncClient, text: str) -> str:
    res = await client.post(
        settings.anthropic_base_url.rstrip("/") + "/v1/messages",
        headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": settings.analyze_model, "max_tokens": 4096, "system": SYSTEM,
              "messages": [{"role": "user", "content": text}]},
    )
    if res.status_code != 200:
        raise AnalyzeError(502, f"Claude lỗi {res.status_code}: {_error_message(res)}")
    out = "".join(b.get("text", "") for b in res.json().get("content", []) if b.get("type") == "text").strip()
    if not out:
        raise AnalyzeError(502, "Claude không trả về nội dung.")
    return out


def _error_message(res: httpx.Response) -> str:
    try:
        return res.json()["error"]["message"]
    except Exception:
        return res.text[:300]
