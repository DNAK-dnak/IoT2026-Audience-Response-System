# API backend cần bổ sung cho Phòng Thi Clicker

`index.html` gọi các hàm dưới đây trên đối tượng `ARS` (trong `ars-client.js`).
Trang **kiểm tra từng hàm có tồn tại hay không**: nếu chưa có, trang vẫn chạy ở chế độ cục bộ như mô tả ở cột "Khi chưa có".
Các hàm cũ (`saveQuiz`, `listClasses`, `uploadClass`, `createSession`, `start`, `next`, `close`, `end`, `finish`, `activeSession`, `getQuiz`, `pageDevices`, `live`, …) giữ nguyên.

| Hàm ARS | Endpoint gợi ý | Trả về | Khi chưa có |
|---|---|---|---|
| `login(id, password)` | `POST /api/auth/login` `{id,password}` | `{ok:true, user:{id,name}, token?}` hoặc `{ok:false, reason:'BAD'\|'LOCKED'}` | Tài khoản lưu trong trình duyệt (băm SHA-256 + salt), mặc định GV001/123456, GV002/abc123 |
| `changePassword(id, oldPw, newPw)` | `POST /api/auth/password` | `{ok:true}` hoặc `{ok:false, reason:'BAD_OLD'\|'WEAK'}` | Đổi trong kho tài khoản cục bộ |
| `removeDevice(sessionId, dev)` | `DELETE /api/sessions/:id/devices/:dev` | bất kỳ; lỗi thì `throw` | Chỉ ẩn thiết bị trên trang |
| `clearDevices(sessionId)` | `DELETE /api/sessions/:id/devices` | bất kỳ | Chỉ ẩn trên trang |
| `setAnswer(sessionId, data)` | `PUT /api/sessions/:id/answers` | bất kỳ | Giữ trong hàng đợi, hiện "Chờ gửi" |
| `saveResults(sessionId, payload)` | `POST /api/sessions/:id/results` | bất kỳ | Giữ trong hàng đợi; giáo viên tải Excel dự phòng |
| `analyze(sessionId, payload)` | `POST /api/sessions/:id/analyze` | chuỗi văn bản, hoặc `{text}` | Bỏ qua bước phân tích, chỉ kết thúc và lưu điểm |

## Ghi chú cho từng hàm

**login / changePassword**: lưu mật khẩu dạng băm (bcrypt/argon2) ở server, giới hạn số lần thử sai theo tài khoản và IP. Trang có khóa 30 giây sau 5 lần sai nhưng đó chỉ là lớp phía trình duyệt.

**removeDevice / clearDevices**: chỉ cho phép khi phiên đang ở `REGISTER`. Sau khi xóa, server nên gửi tin nhắn MQTT cho thiết bị (VD `ars/room1/dev/<dev>/unregistered`) để thiết bị quay lại màn hình nhập MSSV, và giải phóng MSSV để đăng ký lại được. Khi thiết bị đăng ký lại, server phát lại sự kiện `onDevice` như bình thường.

**setAnswer** — `data`:
```json
{ "session": 12, "dev": 7, "mssv": "22110001", "q": 3, "answer": "C",
  "reason": "Thiết bị mất kết nối giữa chừng", "note": "...", "manual": true, "by": "GV001" }
```
`answer: null` nghĩa là xóa đáp án. Nên lưu thành bản ghi riêng (bảng `answer_overrides`) thay vì ghi đè đáp án gốc, để còn đối chiếu. Hàm phải **idempotent** (gửi lại cùng nội dung không tạo bản ghi trùng) vì hàng đợi có thể gửi lại nhiều lần. `pageDevices()` nên trả về đáp án đã áp dụng override.

**saveResults** — `payload` (cũng là dữ liệu gửi cho `analyze`):
```json
{
  "session_id": 12, "quiz_id": 5, "class_id": 2, "class_code": "DH22CNTT01",
  "subject": "...", "exam": "...", "teacher": "GV001", "finished_at": "2026-10-07T04:30:00Z", "timer_mode": "auto",
  "questions": [{ "n": 1, "question": "...", "options": ["..."], "answer": "B", "time": 20,
                  "correct_rate": 0.62, "dist": { "A": 3, "B": 25, "C": 6, "D": 4, "none": 2 } }],
  "students": [{ "dev": 7, "device_id": "07", "mssv": "22110001", "name": "...",
                 "answers": ["B", null, "C"], "correct": 2, "missing": 1, "score": 6.67,
                 "fixes": [{ "q": 2, "from": null, "to": "C", "reason": "...", "note": "...", "manual": true, "by": "GV001", "at": 1759811400000 }] }],
  "stats": { "count": 40, "avg": 6.1, "max": 10, "min": 1.67, "pass": 29 },
  "analysis": "văn bản phân tích AI hoặc null"
}
```
Có thể bị gửi lại nhiều lần (hàng đợi, hoặc giáo viên sửa khiếu nại sau khi lưu): xử lý dạng **upsert theo `session_id`**.

**analyze**: server giữ API key và gọi model (VD Claude Messages API). Gợi ý prompt: tóm tắt kết quả cả lớp; câu nào tỉ lệ đúng thấp và phương án nhiễu nào bị chọn nhiều (gợi ý hiểu sai kiến thức gì); sinh viên nào bỏ trống nhiều (có thể do thiết bị); đề xuất ôn tập. Trả về văn bản; trang hiển thị được tiêu đề `#`, gạch đầu dòng `-` và chữ đậm `**`. Không nên gửi họ tên sinh viên cho model nếu không cần, chỉ MSSV hoặc mã ẩn danh.

## Kết nối trực tiếp (`ARS.live`)
Trang dựa vào `onConnection(ok)` để biết mất/có kết nối và vào `onSnapshot` sau mỗi lần nối lại để dựng lại bảng. `ARS.live` cần **tự nối lại** (backoff 1s → 2s → 5s…) và gửi snapshot ngay khi nối lại.

---

## Trạng thái: đã có ở backend (vòng 4)

Tất cả hàm trên đã có trong `ars-client.js`. Đường dẫn không có tiền tố `/api` (trang được backend phục vụ ở `/ui/`, API ở gốc).

| Hàm ARS | Endpoint thật | Ghi chú |
|---|---|---|
| `login` | `POST /auth/login` | băm scrypt; sai 5 lần → khóa tài khoản 5 phút (`LOCKED`); >20 lần sai/10 phút từ một IP → HTTP 429 |
| `changePassword` | `POST /auth/password` | `BAD_OLD`, `WEAK` (≥ 6 ký tự, có chữ và số, khác mật khẩu cũ), `LOCKED` |
| `removeDevice` / `clearDevices` | `DELETE /sessions/:id/devices[/:dev]` | chỉ khi `REGISTER` (khác → 409). Thiết bị nhận ack `{"seq":null,"code":"REMOVED"}` trên topic ack sẵn có; bridge BLE đổi thành `MODE_2`. Trang khác nhận sự kiện live `removed` (`onRemoved`) |
| `setAnswer` | `PUT /sessions/:id/answers` | bảng `answer_overrides` (chỉ thêm, không sửa đáp án gốc). Gửi lại y hệt → không tạo bản ghi mới. Câu đang mở hoặc chưa hỏi → 409. MSSV không khớp thiết bị → 409 |
| `pageDevices` | `GET /sessions/:id/answers` | trả đáp án đã áp dụng sửa tay + `fix` (`from`, `to`, `reason`, `note`, `manual`, `by`, `at`) nên tải lại trang vẫn thấy khiếu nại |
| `saveResults` | `POST /sessions/:id/results` | upsert theo `session_id`, có `version`; đọc lại: `GET /sessions/:id/saved-results` |
| `analyze` | `POST /sessions/:id/analyze` → `{text}` | cần `ANTHROPIC_API_KEY` trong `.env`. Không có key: `ARS.analyze` bị gỡ, trang tự bỏ bước AI. Không gửi họ tên, mã GV, mã lớp |
| `live` | WebSocket | nối lại 1s → 2s → 4s → 8s, snapshot mỗi lần nối lại |

Bảng xếp hạng và thống kê trên server (`GET /sessions/:id/results`) cũng đã tính cả các sửa tay (view `v_effective_answers`).
