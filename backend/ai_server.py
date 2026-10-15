"""
Máy chủ phân tích AI cho Phòng Thi Clicker (chỉ dùng thư viện chuẩn Python 3.8+).

Trang giáo viên gửi kết quả bài kiểm tra tới POST /api/sessions/<id>/analyze,
máy chủ này gọi Gemini bằng API key giữ ở phía server rồi trả về văn bản phân tích.

Chạy:
    Windows (PowerShell):  $env:GEMINI_API_KEY="AIza..." ; python ai_server.py
    macOS / Linux:         GEMINI_API_KEY="AIza..." python3 ai_server.py

Biến môi trường (tùy chọn):
    GEMINI_MODEL   mặc định gemini-3.6-flash (hết hạn mức thì đổi sang gemini-3.5-flash-lite)
    AI_PORT        mặc định 8787
    ALLOW_ORIGIN   origin của trang giáo viên được phép gọi, mặc định *  (VD http://192.168.1.10:8080)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
PORT = int(os.environ.get("AI_PORT", "8787"))
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
MAX_BODY = 2_000_000  # 2 MB là quá đủ cho 60 sinh viên

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


def slim(payload):
    """Chỉ gửi những gì cần cho phân tích; bỏ họ tên sinh viên (chỉ giữ MSSV)."""
    qs = [
        {k: q.get(k) for k in ("n", "question", "options", "answer", "correct_rate", "dist")}
        for q in payload.get("questions", [])
    ]
    sts = [
        {
            "mssv": s.get("mssv"),
            "answers": s.get("answers"),
            "correct": s.get("correct"),
            "missing": s.get("missing"),
            "score": s.get("score"),
            "manual_fixes": [f.get("q") for f in s.get("fixes", []) if f.get("manual")],
        }
        for s in payload.get("students", [])
    ]
    return {
        "subject": payload.get("subject"),
        "exam": payload.get("exam"),
        "stats": payload.get("stats"),
        "questions": qs,
        "students": sts,
    }


def call_gemini(payload):
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": PROMPT.format(data=json.dumps(slim(payload), ensure_ascii=False))}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
    }
    req = urllib.request.Request(
        f"{GEMINI_BASE}/models/{MODEL}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            detail = detail[:300]
        hint = {
            400: "Yêu cầu không hợp lệ hoặc API key sai.",
            403: "API key không có quyền hoặc bị chặn.",
            404: f"Không tìm thấy model '{MODEL}'. Đổi GEMINI_MODEL.",
            429: "Hết hạn mức (quota) của Gemini. Đợi một lúc hoặc đổi sang gemini-3.5-flash-lite.",
        }.get(e.code, "")
        raise RuntimeError(f"Gemini lỗi {e.code}. {hint} ({detail})")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Không kết nối được Gemini: {e.reason}")

    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "không rõ")
        raise RuntimeError(f"Gemini không trả về nội dung (lý do: {reason}).")
    text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []))
    if not text.strip():
        raise RuntimeError(f"Gemini trả về rỗng (finishReason: {cands[0].get('finishReason')}).")
    return text.strip()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path == "/api/health":
            self._send(200, {"ok": True, "model": MODEL, "key": bool(API_KEY)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not re.fullmatch(r"/api/sessions/[\w-]+/analyze|/api/analyze", self.path.split("?")[0]):
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": "Dữ liệu rỗng hoặc quá lớn."})
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "JSON không hợp lệ."})
        try:
            self._send(200, {"text": call_gemini(payload), "model": MODEL})
        except RuntimeError as e:
            self._send(502, {"error": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write("[ai] " + (fmt % args) + "\n")


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("Thiếu GEMINI_API_KEY. Đặt biến môi trường rồi chạy lại (xem đầu file).")
    print(f"Máy chủ AI chạy ở http://0.0.0.0:{PORT}  ·  model {MODEL}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
