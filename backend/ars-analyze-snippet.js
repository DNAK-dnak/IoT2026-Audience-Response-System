// Dán đoạn này vào CUỐI file ars-client.js (sau khi đối tượng ARS đã được tạo).
// Khi ARS.analyze tồn tại, trang index.html tự hiện ô "Phân tích kết quả bằng AI".
// Đổi AI_URL nếu máy chủ AI chạy ở máy/cổng khác (VD 'http://192.168.1.10:8787').
(function () {
  const AI_URL = 'http://localhost:8787';
  ARS.analyze = async function (sessionId, payload) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 100000);
    try {
      const r = await fetch(`${AI_URL}/api/sessions/${encodeURIComponent(sessionId ?? 'local')}/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || 'Máy chủ AI lỗi ' + r.status);
      return data.text;
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('Máy chủ AI phản hồi quá lâu.');
      if (e instanceof TypeError) throw new Error('Không kết nối được máy chủ AI (' + AI_URL + '). Đã chạy ai_server.py chưa?');
      throw e;
    } finally {
      clearTimeout(t);
    }
  };
})();
