"""AI analysis of a finished test ("Phân tích kết quả bằng AI" on the teacher page).

The API key stays on the SERVER (.env: ANTHROPIC_API_KEY); the browser never sees it.
Only what the analysis needs is sent to the model: question texts, answer distributions,
and per student the MSSV, answers and score. Names, the teacher id and the class code
are removed (data minimisation).

Without a key the endpoint answers 503 and /health reports "analyze": false;
ars-client.js then hides ARS.analyze, so the page simply skips this step.
"""
import json

import httpx

from app.config import settings

LETTERS = "ABCD"

SYSTEM = (
    "Bạn là trợ lý phân tích kết quả kiểm tra trắc nghiệm cho giảng viên đại học. "
    "Trả lời bằng tiếng Việt, ngắn gọn, dựa HOÀN TOÀN vào dữ liệu được cung cấp; không bịa số liệu. "
    "Chỉ dùng định dạng: tiêu đề bắt đầu bằng '# ', gạch đầu dòng '- ', chữ đậm **...**. Không dùng bảng."
)

INSTRUCTIONS = """Dữ liệu bài kiểm tra (JSON) ở dưới. Hãy viết:
# Tổng quan
- điểm trung bình, phân bố, số đạt (≥ 5), nhận xét chung
# Câu hỏi cần chú ý
- những câu có tỉ lệ đúng thấp; phương án nhiễu nào bị chọn nhiều và điều đó gợi ý sinh viên hiểu sai kiến thức gì
# Sinh viên cần hỗ trợ
- MSSV điểm thấp; MSSV bỏ trống nhiều câu (có thể do thiết bị mất kết nối, đề nghị giáo viên kiểm tra); các câu đã được giáo viên sửa tay
# Đề xuất ôn tập
- 3 đến 5 ý cụ thể

Dữ liệu:
"""


class AnalyzeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return bool(settings.anthropic_api_key)


def minimise(payload: dict) -> dict:
    """Keep only what the model needs. No names, no teacher id, no class code."""
    qs = payload.get("questions") or []
    students = payload.get("students") or []
    return {
        "mon": payload.get("subject"),
        "de": payload.get("exam"),
        "so_cau": len(qs),
        "thong_ke": payload.get("stats"),
        "cau_hoi": [
            {
                "cau": q.get("n"),
                "noi_dung": q.get("question"),
                "phuong_an": {LETTERS[i]: o for i, o in enumerate(q.get("options") or []) if i < 4},
                "dap_an_dung": q.get("answer"),
                "ti_le_dung": q.get("correct_rate"),
                "phan_bo": q.get("dist"),       # how many chose A/B/C/D, "none" = no answer
            }
            for q in qs
        ],
        "sinh_vien": [
            {
                "mssv": s.get("mssv"),
                # one character per question: the letter, or "-" for no answer
                "tra_loi": "".join(a or "-" for a in (s.get("answers") or [])),
                "dung": s.get("correct"),
                "bo_trong": s.get("missing"),
                "diem": s.get("score"),
                "cau_da_sua_tay": [f.get("q") for f in (s.get("fixes") or [])],
            }
            for s in students
        ],
    }


async def analyze(payload: dict) -> str:
    if not enabled():
        raise AnalyzeError(503, "Máy chủ chưa cấu hình ANTHROPIC_API_KEY: không phân tích được.")
    data = json.dumps(minimise(payload), ensure_ascii=False, separators=(",", ":"))
    body = {
        "model": settings.analyze_model,
        "max_tokens": 1500,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": INSTRUCTIONS + data}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            res = await client.post(settings.anthropic_base_url.rstrip("/") + "/v1/messages", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise AnalyzeError(502, f"Không gọi được dịch vụ AI: {exc.__class__.__name__}")
    if res.status_code != 200:
        try:
            msg = res.json()["error"]["message"]
        except Exception:
            msg = res.text[:200]
        raise AnalyzeError(502, f"Dịch vụ AI trả lỗi {res.status_code}: {msg}")
    out = res.json()
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise AnalyzeError(502, "Dịch vụ AI không trả về nội dung.")
    return text
