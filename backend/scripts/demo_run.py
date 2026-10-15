"""End-to-end demo: drives a whole quiz through the REAL backend (HTTP + MQTT).

Plays both roles:
  - the teacher, via the REST API (create quiz, session, start, next, close, end)
  - 5 fake clickers, via MQTT (register, answer, duplicates, garbage, a late answer)
and checks every ack code and the final results against what they should be.

Also checks the round-1 features: class list (NOT_IN_CLASS), option texts, the WebSocket
live events for the teacher page, the answer grid and the score distribution.

Run it while the server is running (second terminal, backend folder, .venv active):
    python scripts/demo_run.py
Optional: python scripts/demo_run.py --api http://127.0.0.1:8000 --broker localhost --port 1884
"""
import argparse
import asyncio
import json
import sys
import time

import aiomqtt
import httpx
from websockets.asyncio.client import connect as ws_connect

EXPECTED_RANKING = [("22134001", 3), ("22134002", 2), ("22134003", 1), ("22134004", 0)]


class Clickers:
    """Fake devices: publish messages and remember the acks that come back."""

    def __init__(self, client: aiomqtt.Client, room: str):
        self.client, self.room = client, room
        self.acks: dict[tuple[int, int | None], str] = {}
        self.ack_data: dict[tuple[int, int | None], dict] = {}
        self.states: list[dict] = []

    async def listen(self):
        async for msg in self.client.messages:
            data = json.loads(msg.payload)
            if msg.topic.value.endswith("/ack"):
                dev = int(msg.topic.value.split("/")[3])
                self.acks[(dev, data.get("seq"))] = data["code"]
                self.ack_data[(dev, data.get("seq"))] = data
            elif msg.topic.value.endswith("/state"):
                self.states.append(data)

    async def send(self, dev: int, kind: str, payload) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        await self.client.publish(f"ars/{self.room}/dev/{dev:02d}/{kind}", body, qos=1)

    async def expect(self, dev: int, seq: int | None, code: str, label: str) -> bool:
        for _ in range(50):                       # wait up to ~2.5 s for the ack
            if (dev, seq) in self.acks:
                break
            await asyncio.sleep(0.05)
        got = self.acks.get((dev, seq), "no ack")
        ok = got == code
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<55} expected {code:<12} got {got}")
        return ok


async def main(api: str, broker: str, port: int) -> int:
    room = f"demo{int(time.time()) % 100000}"     # a fresh room, so old sessions never conflict
    results: list[bool] = []
    http = httpx.AsyncClient(base_url=api, timeout=10)

    # ---------------- teacher: quiz + session ----------------
    opts = ["A text", "B text", "C text", "D text"]
    quiz = (await http.post("/quizzes", json={
        "title": "Demo quiz",
        "subject": "IoT",
        "questions": [
            {"text": "QoS for at-least-once?", "options": opts, "correct_choice": 2, "time_limit_s": 5},
            {"text": "Feature for last value?", "options": opts, "correct_choice": 3, "time_limit_s": 30},
            {"text": "What routes messages?", "options": opts, "correct_choice": 1, "time_limit_s": 30},
        ],
    }))
    if not quiz.is_success:
        print(f"\nSTOP: POST /quizzes returned HTTP {quiz.status_code}: {quiz.text[:500]}")
        print("The API is probably running OLD code or the database has the OLD schema.")
        print("Fix: docker compose up -d --build  then reload sql/schema.sql and sql/seed.sql (see RUN_AND_TEST.md).")
        return 1
    quiz = quiz.json()
    print(f"quiz {quiz['id']} created with {len(quiz['questions'])} questions")
    ok = quiz["questions"][0]["options"] == opts
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  option texts are stored and returned")

    bad = await http.post("/quizzes", json={"title": "x",
                                            "questions": [{"text": "q", "options": opts, "correct_choice": 7}]})
    results.append(bad.status_code == 422)
    print(f"  {'PASS' if bad.status_code == 422 else 'FAIL'}  quiz with correct_choice 7 of 4 is rejected (422)")
    bad = await http.post("/quizzes", json={"title": "x",
                                            "questions": [{"text": "q", "options": opts[:3], "correct_choice": 1}]})
    results.append(bad.status_code == 422)
    print(f"  {'PASS' if bad.status_code == 422 else 'FAIL'}  quiz with only 3 options is rejected (422)")

    # class list: devices 1-4 are on it, 22134099 is not
    cls = (await http.post("/classes", json={
        "code": f"DEMO-{room}", "name": "Demo class",
        "students": [{"mssv": f"2213400{i}", "full_name": f"Student {i}"} for i in range(1, 6)],
    })).json()
    print(f"class {cls['code']} uploaded with {cls['students']} students")

    session = (await http.post("/sessions", json={"quiz_id": quiz["id"], "room": room, "class_id": cls["id"]})).json()
    sid = session["id"]
    print(f"session {sid} in room {room}: {session['phase']}")

    wrong = await http.post(f"/sessions/{sid}/next")
    results.append(wrong.status_code == 409)
    print(f"  {'PASS' if wrong.status_code == 409 else 'FAIL'}  'next' before 'start' is refused (409): {wrong.json()['detail']}")

    # the teacher page's live connection
    ws_url = api.replace("http", "ws", 1) + f"/sessions/{sid}/live"
    live_events: list[dict] = []

    async def teacher_page():
        async with ws_connect(ws_url) as ws:
            async for text in ws:
                live_events.append(json.loads(text))

    page = asyncio.create_task(teacher_page())

    async with aiomqtt.Client(hostname=broker, port=port, identifier=f"demo-{room}") as client:
        clickers = Clickers(client, room)
        await client.subscribe(f"ars/{room}/dev/+/ack", qos=1)
        await client.subscribe(f"ars/{room}/state", qos=1)
        listener = asyncio.create_task(clickers.listen())
        await asyncio.sleep(0.3)

        # a register before the session is started -> CLOSED
        await clickers.send(1, "register", {"dev": 1, "mssv": "22134001", "seq": 1})
        results.append(await clickers.expect(1, 1, "CLOSED", "register while session is IDLE"))

        await http.post(f"/sessions/{sid}/start")
        print("\nREGISTER phase")
        clickers.acks.clear()
        for dev in range(1, 5):
            await clickers.send(dev, "register", {"dev": dev, "mssv": f"2213400{dev}", "seq": 1})
        for dev in range(1, 5):
            results.append(await clickers.expect(dev, 1, "OK", f"device {dev} registers"))
        await clickers.send(5, "register", {"dev": 5, "mssv": "22134001", "seq": 1})
        results.append(await clickers.expect(5, 1, "DUP", "device 5 uses device 1's MSSV"))
        await clickers.send(6, "register", {"dev": 6, "mssv": "12", "seq": 1})
        results.append(await clickers.expect(6, 1, "BAD", "device 6 sends a malformed MSSV"))
        await clickers.send(7, "register", "this is not json")
        results.append(await clickers.expect(7, None, "BAD", "device 7 sends text that is not JSON"))
        await clickers.send(8, "register", {"dev": 8, "mssv": "22134099", "seq": 1})
        results.append(await clickers.expect(8, 1, "NOT_IN_CLASS", "device 8: MSSV not on the class list"))

        # the teacher removes a device during registration: its MSSV becomes free again
        await clickers.send(11, "register", {"dev": 11, "mssv": "22134005", "seq": 1})
        results.append(await clickers.expect(11, 1, "OK", "device 11 registers with 22134005"))
        r = await http.delete(f"/sessions/{sid}/devices/11")
        results.append(r.status_code == 200)
        print(f"  {'PASS' if r.status_code == 200 else 'FAIL'}  teacher removes device 11 (HTTP {r.status_code})")
        results.append(await clickers.expect(11, None, "REMOVED", "device 11 is told REMOVED (back to MSSV entry)"))
        await clickers.send(12, "register", {"dev": 12, "mssv": "22134005", "seq": 1})
        results.append(await clickers.expect(12, 1, "OK", "the freed MSSV registers on device 12"))
        r = await http.delete(f"/sessions/{sid}/devices/11")
        results.append(r.status_code == 404)
        print(f"  {'PASS' if r.status_code == 404 else 'FAIL'}  removing device 11 again -> 404")
        await http.delete(f"/sessions/{sid}/devices/12")          # back to 4 devices for the ranking below

        # ---------------- question 1 (correct = 2, 5 s limit) ----------------
        await http.post(f"/sessions/{sid}/next")
        print("\nQUESTION 1 open (5 s)")
        r = await http.delete(f"/sessions/{sid}/devices/1")
        results.append(r.status_code == 409)
        print(f"  {'PASS' if r.status_code == 409 else 'FAIL'}  removing a device after the start is refused (409)")
        await clickers.send(1, "answer", {"dev": 1, "q": 1, "choice": 2, "seq": 2})
        await clickers.send(2, "answer", {"dev": 2, "q": 1, "choice": 2, "seq": 2})
        await clickers.send(3, "answer", {"dev": 3, "q": 1, "choice": 3, "seq": 2})
        results.append(await clickers.expect(1, 2, "OK", "device 1 answers 2 (right) -> locked"))
        results.append(await clickers.expect(2, 2, "OK", "device 2 answers 2 (right) -> locked"))
        results.append(await clickers.expect(3, 2, "OK", "device 3 answers 3 (wrong) -> locked"))
        ok = clickers.ack_data.get((2, 2), {}).get("choice") == 2
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  the OK ack tells device 2 its locked choice (2)")
        await clickers.send(2, "answer", {"dev": 2, "q": 1, "choice": 1, "seq": 3})
        results.append(await clickers.expect(2, 3, "LOCKED", "device 2 tries to change to 1 (seq 3)"))
        ok = clickers.ack_data.get((2, 3), {}).get("choice") == 2
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  the LOCKED ack says the locked choice is still 2")
        clickers.acks.pop((2, 2), None)
        await clickers.send(2, "answer", {"dev": 2, "q": 1, "choice": 2, "seq": 2})
        results.append(await clickers.expect(2, 2, "OK", "device 2 resends seq 2 (ack was lost): confirmed again"))
        await clickers.send(3, "answer", {"dev": 3, "q": 1, "choice": 9, "seq": 3})
        results.append(await clickers.expect(3, 3, "BAD", "device 3 sends choice 9 of 4"))
        await clickers.send(9, "answer", {"dev": 9, "q": 1, "choice": 2, "seq": 1})
        results.append(await clickers.expect(9, 1, "UNREGISTERED", "device 9 was never registered"))
        await clickers.send(1, "answer", {"dev": 1, "q": 2, "choice": 3, "seq": 3})
        results.append(await clickers.expect(1, 3, "CLOSED", "device 1 answers question 2 too early"))

        print("  ... waiting for the automatic close at the deadline")
        await asyncio.sleep(5.6)
        state = (await http.get(f"/sessions/{sid}")).json()
        results.append(state["phase"] == "QUESTION_CLOSED")
        print(f"  {'PASS' if state['phase'] == 'QUESTION_CLOSED' else 'FAIL'}  question 1 closed automatically")
        await clickers.send(4, "answer", {"dev": 4, "q": 1, "choice": 2, "seq": 2})
        results.append(await clickers.expect(4, 2, "CLOSED", "device 4 answers after the deadline"))

        # ---------------- question 2 (correct = 3) ----------------
        await http.post(f"/sessions/{sid}/next")
        print("\nQUESTION 2 open")
        await clickers.send(1, "answer", {"dev": 1, "q": 2, "choice": 3, "seq": 4})
        await clickers.send(2, "answer", {"dev": 2, "q": 2, "choice": 3, "seq": 4})
        results.append(await clickers.expect(1, 4, "OK", "device 1 answers 3 (right)"))
        results.append(await clickers.expect(2, 4, "OK", "device 2 answers 3 (right)"))
        await http.post(f"/sessions/{sid}/close")

        # ---------------- question 3 (correct = 1) ----------------
        await http.post(f"/sessions/{sid}/next")
        print("\nQUESTION 3 open")
        await clickers.send(1, "answer", {"dev": 1, "q": 3, "choice": 1, "seq": 5})
        await clickers.send(2, "answer", {"dev": 2, "q": 3, "choice": 4, "seq": 5})
        await clickers.send(3, "answer", {"dev": 3, "q": 3, "choice": 1, "seq": 4})
        results.append(await clickers.expect(1, 5, "OK", "device 1 answers 1 (right)"))
        results.append(await clickers.expect(2, 5, "OK", "device 2 answers 4 (wrong)"))
        results.append(await clickers.expect(3, 4, "OK", "device 3 answers 1 (right)"))
        await http.post(f"/sessions/{sid}/close")

        no_more = await http.post(f"/sessions/{sid}/next")
        results.append(no_more.status_code == 409)
        print(f"\n  {'PASS' if no_more.status_code == 409 else 'FAIL'}  'next' after the last question (409): {no_more.json()['detail']}")
        await http.post(f"/sessions/{sid}/end")

        await asyncio.sleep(0.3)
        phases = [s["phase"] for s in clickers.states]
        print(f"  state messages seen by the clickers: {' -> '.join(dict.fromkeys(phases))}")
        listener.cancel()

    # ---------------- what the teacher page received live ----------------
    page.cancel()
    kinds = [e["type"] for e in live_events]
    print("\nLIVE EVENTS (teacher page WebSocket)")
    print("  " + ", ".join(f"{k} {kinds.count(k)}" for k in dict.fromkeys(kinds)))
    checks = [
        (kinds[:1] == ["snapshot"], "first event is a snapshot"),
        (sum(1 for e in live_events if e["type"] == "device") == 6, "6 'device' events (registrations)"),
        (sum(1 for e in live_events if e["type"] == "removed") == 2, "2 'removed' events"),
        (any(e["type"] == "device" and e.get("full_name") == "Student 1" for e in live_events), "device event carries the student name"),
        (any(e["type"] == "ack" and e["code"] == "NOT_IN_CLASS" for e in live_events), "rejected NOT_IN_CLASS is shown to the teacher"),
        (any(e["type"] == "answer" for e in live_events), "'answer' events with a live count"),
        (all("choice" not in e for e in live_events if e["type"] == "answer"), "answer events do not reveal the choice"),
        ([e["phase"] for e in live_events if e["type"] == "state"][-1:] == ["ENDED"], "last state event is ENDED"),
    ]
    for ok, label in checks:
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")

    grid = (await http.get(f"/sessions/{sid}/answers")).json()
    ok = len(grid) == 8 and {(r["device_no"], r["q"], r["choice"]) for r in grid} >= {(2, 1, 2), (1, 3, 1)}
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  answer grid has 8 answers, device 2 kept its locked answer")

    # ---------------- results ----------------
    res = (await http.get(f"/sessions/{sid}/results")).json()
    print("\nRANKING")
    for r in res["ranking"]:
        print(f"  {r['rank']}. {r['mssv']}  device {r['device_no']:>2}  score {r['score']}  answered {r['answered']}")
    got_ranking = [(r["mssv"], r["score"]) for r in res["ranking"]]
    results.append(got_ranking == EXPECTED_RANKING)
    print(f"  {'PASS' if got_ranking == EXPECTED_RANKING else 'FAIL'}  ranking matches the expected result")

    print("QUESTIONS")
    for q in res["questions"]:
        print(f"  Q{q['position']}: answered {q['answered']}/{q['registered']}, correct {q['correct']}, "
              f"{q['pct_of_answered']}% of answered, {q['pct_of_registered']}% of registered")
    dist = {d["score"]: d["students"] for d in res["distribution"]}
    ok = dist == {0: 1, 1: 1, 2: 1, 3: 1}
    results.append(ok)
    print(f"SCORE DISTRIBUTION  {dist}")
    print(f"  {'PASS' if ok else 'FAIL'}  one student at each score 0..3")
    print("RAW EVENT STATUSES")
    print("  " + ", ".join(f"{e['status']} {e['n']}" for e in res["events"]))

    # ---------------- teacher corrections (complaints / manual entry) ----------------
    print("\nCORRECTIONS")
    fix = {"dev": 4, "mssv": "22134004", "q": 1, "answer": "B", "reason": "Thiết bị hết pin", "manual": True, "by": "GV001"}
    r1 = (await http.put(f"/sessions/{sid}/answers", json=fix)).json()
    r2 = (await http.put(f"/sessions/{sid}/answers", json=fix)).json()          # the page's outbox resends
    ok = r1.get("duplicate") is False and r2.get("duplicate") is True and r1["id"] == r2["id"]
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  manual entry device 4 q1 = B; the same request again adds nothing")
    r = await http.put(f"/sessions/{sid}/answers", json={**fix, "mssv": "22134001"})
    results.append(r.status_code == 409)
    print(f"  {'PASS' if r.status_code == 409 else 'FAIL'}  fix with the wrong MSSV for the device -> 409")
    await http.put(f"/sessions/{sid}/answers", json={"dev": 2, "mssv": "22134002", "q": 1, "answer": None,
                                                     "reason": "Sinh viên khiếu nại, đã xác minh", "by": "GV001"})
    grid = {(g["device_no"], g["q"]): g for g in (await http.get(f"/sessions/{sid}/answers")).json()}
    ok = (grid[(4, 1)]["choice"] == 2 and grid[(4, 1)]["device_choice"] is None and grid[(4, 1)]["manual"]
          and grid[(2, 1)]["choice"] is None and grid[(2, 1)]["device_choice"] == 2)
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  grid shows the fixes and keeps what the devices sent")
    ranking = [(r["mssv"], r["score"]) for r in (await http.get(f"/sessions/{sid}/results")).json()["ranking"]]
    expected = [("22134001", 3), ("22134002", 1), ("22134003", 1), ("22134004", 1)]
    results.append(ranking == expected)
    print(f"  {'PASS' if ranking == expected else 'FAIL'}  ranking counts the fixes: {ranking}")

    # ---------------- saved final result ----------------
    payload = {"session_id": sid, "teacher": "GV001", "students": [{"mssv": "22134001", "score": 10}], "analysis": None}
    v1 = (await http.post(f"/sessions/{sid}/results", json=payload)).json()
    v2 = (await http.post(f"/sessions/{sid}/results", json={**payload, "students": []})).json()
    saved = (await http.get(f"/sessions/{sid}/saved-results")).json()
    ok = v1.get("version") == 1 and v2.get("version") == 2 and saved["payload"]["students"] == []
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  results saved, re-saving replaces them (version {v2.get('version')})")
    r = await http.post(f"/sessions/{sid}/analyze", json=payload)
    print(f"  INFO  AI analysis: HTTP {r.status_code} ({'no ANTHROPIC_API_KEY on the server' if r.status_code == 503 else 'ok'})")

    await http.aclose()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1884)
    args = parser.parse_args()
    coro = main(args.api, args.broker, args.port)
    if sys.platform == "win32":
        # aiomqtt needs the selector event loop on Windows
        sys.exit(asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop))
    sys.exit(asyncio.run(coro))
