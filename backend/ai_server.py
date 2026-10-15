"""
Máy chủ phân tích AI cho Phòng Thi Clicker (chỉ dùng thư viện chuẩn Python 3.8+).

Phân công:
  - CODE tính mọi số liệu: điểm thang 10, tỉ lệ đúng từng câu / từng cụm, phương án nhiễu,
    sinh viên dưới 5, sinh viên bỏ trống nhiều, đáp án nhập tay.
  - GEMINI chỉ làm phần cần đến hiểu nội dung: gom câu hỏi thành cụm kiến thức, giải thích
    sinh viên hiểu sai điều gì (dựa vào nội dung phương án sai), và đề xuất ôn tập.
    Model trả JSON theo schema, code ghép lại thành văn bản → không bịa số, không bị lạc định dạng.

Chạy:
    Windows (PowerShell):  $env:GEMINI_API_KEY="AIza..." ; python ai_server.py
    macOS / Linux:         GEMINI_API_KEY="AIza..." python3 ai_server.py

Biến môi trường (tùy chọn):
    GEMINI_MODEL     mặc định gemini-3.6-flash (hết hạn mức thì đổi sang gemini-3.5-flash-lite)
    GEMINI_THINKING  mức suy nghĩ của model: minimal | low | medium | high, mặc định low
    AI_PORT          mặc định 8787
    ALLOW_ORIGIN     origin của trang giáo viên được phép gọi, mặc định *
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import median

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
THINKING = os.environ.get("GEMINI_THINKING", "low").strip()
PORT = int(os.environ.get("AI_PORT", "8787"))
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
MAX_BODY = 2_000_000
MAX_SCORE = 10       # điểm tối đa
PASS_SCORE = 5       # ngưỡng đạt
LOW_RATE = 50        # câu / cụm có tỉ lệ đúng dưới mức này (%) là cần chú ý
MAX_OUTPUT_TOKENS = 200000   # bao gồm cả token suy nghĩ của model
LETTERS = "ABCD"


# ======================= 1. CODE: tính số liệu =======================

def prepare(payload):
    qs_in = payload.get("questions") or []
    sts_in = payload.get("students") or []
    nq, ns = len(qs_in), len(sts_in)

    students = []
    for s in sts_in:
        a = list(s.get("answers") or [])[:nq]
        a += [None] * (nq - len(a))
        students.append({
            "mssv": str(s.get("mssv") or ""),
            "answers": [str(x).upper() if x else None for x in a],
            "manual": sorted({int(f["q"]) for f in (s.get("fixes") or []) if f.get("manual") and f.get("q")}),
        })

    questions = []
    for i, q in enumerate(qs_in):
        opts = list(q.get("options") or [])[:4]
        ans = str(q.get("answer") or "").upper()
        picks = Counter(st["answers"][i] or "blank" for st in students)
        right = picks.get(ans, 0)
        wrong = sorted(((k, v) for k, v in picks.items() if k in LETTERS and k != ans and v), key=lambda kv: -kv[1])
        questions.append({
            "n": int(q.get("n") or i + 1),
            "topic": (q.get("topic") or "").strip() or None,
            "question": q.get("question") or "",
            "options": opts,
            "correct": ans,
            "rate": round(right / ns * 100) if ns else 0,
            "picks": {k: picks.get(k, 0) for k in list(LETTERS[:len(opts)]) + ["blank"]},
            "top_wrong": wrong[0] if wrong else None,
        })

    for st in students:
        st["correct"] = sum(1 for i, q in enumerate(questions) if st["answers"][i] == q["correct"])
        st["score"] = round(st["correct"] / nq * MAX_SCORE, 2) if nq else 0
        st["blank"] = [questions[i]["n"] for i in range(nq) if not st["answers"][i]]

    scores = sorted(st["score"] for st in students)
    stats = {
        "max_score": MAX_SCORE, "pass_score": PASS_SCORE, "students": ns, "questions": nq,
        "avg": round(sum(scores) / ns, 2) if ns else 0,
        "median": round(median(scores), 2) if ns else 0,
        "max": scores[-1] if scores else 0, "min": scores[0] if scores else 0,
        "passed": sum(1 for x in scores if x >= PASS_SCORE),
    }
    return questions, students, stats


def opt_text(q, letter):
    i = LETTERS.find(letter or "")
    return q["options"][i] if 0 <= i < len(q["options"]) else ""


def short(t, n=90):
    t = re.sub(r"\s+", " ", str(t)).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ======================= 2. GEMINI: gom cụm + giải thích =======================

SYSTEM = (
    "Bạn là chuyên gia sư phạm, giúp giảng viên đại học Việt Nam hiểu sinh viên sai ở đâu trong một bài trắc nghiệm. "
    "Đọc kỹ nội dung câu hỏi, đáp án đúng và các phương án sai mà sinh viên đã chọn. "
    "Chỉ trả về JSON đúng schema. Không đưa ra số liệu thống kê (phần đó đã có). Viết tiếng Việt, ngắn gọn, cụ thể."
)

PROMPT = """Bài kiểm tra môn "{subject}". Danh sách câu hỏi kèm số sinh viên chọn từng phương án ("blank" = bỏ trống):

{questions}

Yêu cầu:
1. "clusters": gom TẤT CẢ câu hỏi thành các cụm kiến thức (mỗi câu thuộc đúng 1 cụm). Nếu câu có "topic" thì dùng topic làm tên cụm. Mỗi cụm:
   - "name": tên cụm kiến thức ngắn (2–6 từ).
   - "questions": danh sách số câu.
   - "misconception": 1–2 câu nêu sinh viên đang hiểu sai điều gì trong cụm này, dựa vào nội dung các phương án sai được chọn nhiều (trích ngắn phương án đó). Nếu cụm làm tốt, ghi chuỗi rỗng.
2. "question_notes": với MỖI câu trong danh sách cần chú ý {low_list}, giải thích 1 câu vì sao phương án sai chính dễ gây nhầm. Nếu một phương án sai được chọn nhiều hơn cả đáp án đúng, cân nhắc khả năng đề hoặc đáp án có vấn đề và nói rõ.
3. "suggestions": 3–5 đề xuất ôn tập hoặc chỉnh đề, cụ thể theo cụm kiến thức."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "clusters": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"},
            "questions": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            "misconception": {"type": "STRING"},
        }, "required": ["name", "questions", "misconception"]}},
        "question_notes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "n": {"type": "INTEGER"}, "note": {"type": "STRING"},
        }, "required": ["n", "note"]}},
        "suggestions": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["clusters", "question_notes", "suggestions"],
}


def model_input(questions, subject):
    lines = []
    for q in questions:
        opts = "; ".join(f"{LETTERS[j]}. {o}" for j, o in enumerate(q["options"]))
        picks = ", ".join(f"{k}: {v}" for k, v in q["picks"].items())
        lines.append(json.dumps({
            "n": q["n"], "topic": q["topic"], "question": q["question"], "options": opts,
            "correct": q["correct"], "picks": picks, "correct_rate_pct": q["rate"],
        }, ensure_ascii=False))
    low = [q["n"] for q in questions if q["rate"] < LOW_RATE]
    return PROMPT.format(subject=subject or "", questions="\n".join(lines),
                         low_list=json.dumps(low) if low else "[] (không có câu nào, trả mảng rỗng)")


class GeminiError(RuntimeError):
    pass


def _post(body):
    req = urllib.request.Request(
        f"{GEMINI_BASE}/models/{MODEL}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            detail = detail[:300]
        err = GeminiError(detail)
        err.code = e.code
        raise err
    except urllib.error.URLError as e:
        raise RuntimeError(f"Không kết nối được Gemini: {e.reason}")


def call_model(prompt):
    base = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    gen = {"temperature": 0.3, "maxOutputTokens": MAX_OUTPUT_TOKENS,
           "responseMimeType": "application/json", "responseSchema": SCHEMA}
    # thử với cấu hình suy nghĩ; model nào không hỗ trợ thinkingLevel thì thử lại không có nó
    attempts = [dict(gen, thinkingConfig={"thinkingLevel": THINKING})] if THINKING else []
    attempts.append(gen)
    last = None
    for g in attempts:
        try:
            res = _post(dict(base, generationConfig=g))
            break
        except GeminiError as e:
            last = e
            if e.code == 400 and "think" in str(e).lower():
                continue
            hint = {
                400: "Yêu cầu không hợp lệ hoặc API key sai.",
                403: "API key không có quyền hoặc bị chặn.",
                404: f"Không tìm thấy model '{MODEL}'. Đổi GEMINI_MODEL.",
                429: "Hết hạn mức (quota) của Gemini. Đợi một lúc hoặc đổi sang gemini-3.5-flash-lite.",
            }.get(e.code, "")
            raise RuntimeError(f"Gemini lỗi {e.code}. {hint} ({e})")
    else:
        raise RuntimeError(f"Gemini lỗi: {last}")

    cands = res.get("candidates") or []
    if not cands:
        reason = (res.get("promptFeedback") or {}).get("blockReason", "không rõ")
        raise RuntimeError(f"Gemini không trả về nội dung (lý do: {reason}).")
    finish = cands[0].get("finishReason")
    text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []) if not p.get("thought"))
    if finish == "MAX_TOKENS":
        used = (res.get("usageMetadata") or {}).get("thoughtsTokenCount")
        raise RuntimeError("Gemini dừng giữa chừng vì hết giới hạn token"
                           + (f" ({used} token dùng cho suy nghĩ)" if used else "")
                           + ". Đặt GEMINI_THINKING=minimal hoặc low rồi thử lại.")
    try:
        return json.loads(text)
    except Exception:
        raise RuntimeError(f"Gemini trả về dữ liệu không đúng định dạng (finishReason: {finish}).")


# ======================= 3. CODE: ghép kết quả =======================

def build_report(questions, students, ai):
    byn = {q["n"]: q for q in questions}

    # cụm: lấy tên + giải thích từ AI, số liệu tính lại từ dữ liệu thật
    clusters, seen = [], set()
    for c in ai.get("clusters") or []:
        qn = [n for n in dict.fromkeys(int(x) for x in c.get("questions") or []) if n in byn and n not in seen]
        if not qn:
            continue
        seen.update(qn)
        clusters.append({"name": short(c.get("name") or "Cụm", 60), "questions": qn,
                         "misconception": (c.get("misconception") or "").strip()})
    # câu AI bỏ sót: gom theo chủ đề giáo viên ghi (nếu có), còn lại vào "Khác"
    rest = {}
    for q in questions:
        if q["n"] not in seen:
            rest.setdefault(q["topic"] or "Khác", []).append(q["n"])
    for name, qn in rest.items():
        same = next((c for c in clusters if c["name"].lower() == name.lower()), None)
        if same:
            same["questions"] += qn
        else:
            clusters.append({"name": name, "questions": qn, "misconception": ""})
    for c in clusters:
        c["rate"] = round(sum(byn[n]["rate"] for n in c["questions"]) / len(c["questions"]))
    clusters.sort(key=lambda c: c["rate"])
    q_cluster = {n: c["name"] for c in clusters for n in c["questions"]}
    qi = {q["n"]: i for i, q in enumerate(questions)}

    out = []
    weak = [c for c in clusters if c["rate"] < LOW_RATE + 10]
    good = [c for c in clusters if c["rate"] >= LOW_RATE + 10]
    out.append("## Các cụm kiến thức hay sai")
    if weak:
        for c in weak:
            line = f"- **{c['name']}** (Câu {', '.join(map(str, c['questions']))}): đúng trung bình **{c['rate']}%**."
            if c["misconception"]:
                line += f" {c['misconception']}"
            out.append(line)
    else:
        out.append("- Không có cụm kiến thức nào có tỉ lệ đúng dưới " + str(LOW_RATE + 10) + "%.")
    if good:
        out.append("- Làm tốt: " + "; ".join(f"**{c['name']}** ({c['rate']}%)" for c in reversed(good)) + ".")

    notes = {int(x.get("n")): (x.get("note") or "").strip() for x in ai.get("question_notes") or [] if x.get("n") is not None}
    low = sorted((q for q in questions if q["rate"] < LOW_RATE), key=lambda q: q["rate"])
    out.append("")
    out.append("## Câu hỏi cần chú ý")
    if low:
        for q in low:
            line = f"- **Câu {q['n']}** ({short(q['question'])}): {q['rate']}% đúng, đáp án **{q['correct']}. {short(opt_text(q, q['correct']), 50)}**."
            if q["top_wrong"]:
                k, v = q["top_wrong"]
                line += f" Nhiều nhất chọn sai **{k}. {short(opt_text(q, k), 50)}** ({v} SV)."
            if q["picks"].get("blank"):
                line += f" {q['picks']['blank']} SV bỏ trống."
            if notes.get(q["n"]):
                line += f" {notes[q['n']]}"
            out.append(line)
    else:
        out.append(f"- Không có câu nào dưới {LOW_RATE}% đúng.")

    out.append("")
    out.append("## Sinh viên cần hỗ trợ")
    below = sorted((s for s in students if s["score"] < PASS_SCORE), key=lambda s: s["score"])
    if below:
        for s in below:
            weak_c = []
            for c in clusters:
                ok = sum(1 for n in c["questions"] if s["answers"][qi[n]] == byn[n]["correct"])
                if ok / len(c["questions"]) < 0.5:
                    weak_c.append(c["name"])
            out.append(f"- {s['mssv']}: {s['score']:.2f}/{MAX_SCORE}".replace(".", ",")
                       + (f", yếu: {', '.join(weak_c)}" if weak_c else "") + ".")
    else:
        out.append(f"- Không có sinh viên dưới {PASS_SCORE} điểm.")
    nblank = max(2, round(len(questions) * 0.3))
    many_blank = [s for s in students if len(s["blank"]) >= nblank]
    if many_blank:
        out.append("- Bỏ trống nhiều (có thể do lỗi thiết bị / mất kết nối): "
                   + "; ".join(f"{s['mssv']} ({len(s['blank'])} câu)" for s in many_blank) + ".")
    manual = [s for s in students if s["manual"]]
    if manual:
        out.append("- Có đáp án giáo viên nhập tay: "
                   + "; ".join(f"{s['mssv']} (câu {', '.join(map(str, s['manual']))})" for s in manual) + ".")

    sug = [str(x).strip() for x in ai.get("suggestions") or [] if str(x).strip()]
    if sug:
        out.append("")
        out.append("## Đề xuất")
        out += [f"- {x}" for x in sug[:6]]

    return "\n".join(out), [{k: c[k] for k in ("name", "questions", "rate", "misconception")} for c in clusters]


def analyze(payload):
    questions, students, stats = prepare(payload)
    if not questions or not students:
        raise RuntimeError("Không có câu hỏi hoặc sinh viên nào để phân tích.")
    ai = call_model(model_input(questions, payload.get("subject")))
    text, clusters = build_report(questions, students, ai)
    return {"text": text, "stats": stats, "clusters": clusters, "model": MODEL}


# ======================= HTTP =======================

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
            self._send(200, {"ok": True, "model": MODEL, "thinking": THINKING, "key": bool(API_KEY), "max_score": MAX_SCORE})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not re.fullmatch(r"/api/sessions/[\w-]+/analyze|/api/analyze|/api/preview", path):
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": "Dữ liệu rỗng hoặc quá lớn."})
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "JSON không hợp lệ."})
        try:
            if path == "/api/preview":   # xem số liệu và đề bài sẽ gửi cho Gemini, không gọi model
                questions, students, stats = prepare(payload)
                return self._send(200, {"stats": stats, "prompt": model_input(questions, payload.get("subject"))})
            self._send(200, analyze(payload))
        except RuntimeError as e:
            self._send(502, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"Lỗi xử lý dữ liệu: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[ai] " + (fmt % args) + "\n")


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("Thiếu GEMINI_API_KEY. Đặt biến môi trường rồi chạy lại (xem đầu file).")
    print(f"Máy chủ AI chạy ở http://0.0.0.0:{PORT}  ·  model {MODEL}  ·  thinking {THINKING}  ·  thang điểm {MAX_SCORE}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()