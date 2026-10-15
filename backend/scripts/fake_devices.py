"""Fake clickers: realistic simulation of the real devices, with test scenarios and a report.

Each fake device behaves like a Pico clicker behind the BLE gateway:
  - BLE link: every message waits a random BLE delay (log-normal + connection-interval jitter)
    on the way up AND on the way down (ack), and can be lost (--ble-loss)
  - the device waits for its ack; with no ack after --ack-timeout it resends the SAME seq
    (up to --tries times), exactly like the firmware should
  - students react at random times (log-normal), in random order; they pick A-D on the device
    (nothing sent) and press CONFIRM: that sends the answer. The first confirmed answer is
    LOCKED by the server; the ack says which choice is locked, and the device shows that

Pick what to test with --test:
  normal      realistic class: random reaction times, 8 % never answer,
              10 % try to change after the lock (expect LOCKED)
  burst       every device registers at the same instant and answers at the same instant
  lossy       bad radio: 20 % of BLE packets lost both ways -> resends with the same seq
              (the server confirms a resend of the locked answer with OK again)
  wrong_mssv  typo / not in the class / another student's MSSV, then the student corrects it
  reboot      the clicker reboots mid-quiz (seq back to 1); half re-register, half do not
  late        answers after the deadline, and an answer for the wrong question
  dropout     the device disappears for some questions (out of range, battery), then comes back
  spam        the student presses CONFIRM 3-6 times quickly: one OK, the rest LOCKED
  random      one of the above, chosen at random
  all         every scenario at once: devices are split between all the scenarios

Two ways to run:
  1. Follow the teacher page (default): open the page, run this, click through the quiz.
     A report is printed every time a session ends.
         python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test all
  2. Self-driving (--drive): the script creates a short quiz + session itself, runs it,
     prints the report and exits with code 0 (PASS) or 1 (FAIL). No browser needed.
         python scripts/fake_devices.py -n 60 --mssv-from-class 1 --test all --drive

Note: --drive ends any running session in the room first (it needs the room to itself).
Stop with Ctrl+C.
"""
import argparse
import asyncio
import json
import logging
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

import aiomqtt
import httpx

SCENARIOS = ["normal", "burst", "lossy", "wrong_mssv", "reboot", "late", "dropout", "spam"]


# ---------------------------------------------------------------- data
@dataclass
class Msg:
    """One message a device sends (register or answer), and what happened to it."""
    dev: int
    kind: str                       # register | answer
    payload: dict
    expect: set                     # ack codes that count as correct behaviour
    note: str = ""                  # e.g. "late", "stolen MSSV", "known issue"
    known_issue: bool = False       # expected to fail until round 2
    deadline: float | None = None   # local monotonic deadline of the question (answers)
    t_press: float = 0.0            # button press (start of the BLE uplink)
    t_publish: float | None = None  # first time it reached MQTT
    t_last_publish: float | None = None  # last time it reached MQTT (after resends)
    t_mqtt_ack: float | None = None # ack seen on MQTT (at the bridge)
    t_ack: float | None = None      # ack received by the device (after BLE downlink)
    tries: int = 0
    code: str | None = None         # first ack the device received; NO_ACK if none
    ack_choice: int | None = None   # answers: the choice the SERVER says is locked (from the ack)
    press_no: int = 0               # order of presses on this device (seq resets on reboot)
    event: asyncio.Event = field(default_factory=asyncio.Event)


class Device:
    def __init__(self, dev: int, mssv: str, scenario: str):
        self.dev, self.mssv, self.scenario = dev, mssv, scenario
        self.seq = 0
        self.presses = 0
        self.registered = False
        self.rebooted = False
        self.reg_unknown = False    # register never acked: the device cannot know if it worked
        self.stale_seq = False      # rebooted without re-registering: seq is behind the server

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


# ---------------------------------------------------------------- simulator
class Sim:
    def __init__(self, client: aiomqtt.Client, args, devices: list[Device]):
        self.c, self.a, self.devices = client, args, devices
        self.room = args.room
        self.by_dev = {d.dev: d for d in devices}
        self.pending: dict[tuple[int, int], Msg] = {}
        self.msgs: list[Msg] = []
        self.session_id: int | None = None
        self.has_class = False
        self.tasks: set[asyncio.Task] = set()
        self.register_closed_at: float | None = None   # when the teacher left the REGISTER phase
        # --drive: ignore any session that was already running; react only to the one we create
        self.armed = not args.drive

    # ---------- random models
    def ble_delay(self) -> float:
        """One BLE hop: log-normal around --ble-ms, plus waiting for the next connection event."""
        if self.a.ble_ms <= 0:
            return 0.0
        d = random.lognormvariate(math.log(self.a.ble_ms), 0.5) + random.uniform(0, self.a.ble_interval)
        return min(d, 1500) / 1000

    def loss(self, d: Device) -> float:
        return max(self.a.ble_loss, 0.20) if d.scenario == "lossy" else self.a.ble_loss

    @staticmethod
    def reaction(window_s: float) -> float:
        """Student reaction time: most answer in the first half, a few near the end."""
        return min(random.lognormvariate(math.log(max(window_s * 0.35, 0.5)), 0.6), window_s * 0.9)

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    # ---------- sending with BLE + retries
    async def send(self, d: Device, kind: str, payload: dict, expect: set, note: str = "",
                   known_issue: bool = False, deadline: float | None = None, ble: bool = True) -> Msg:
        d.presses += 1
        m = Msg(d.dev, kind, payload, expect, note, known_issue, deadline,
                t_press=time.monotonic(), press_no=d.presses)
        self.msgs.append(m)
        key = (d.dev, payload.get("seq"))
        for attempt in range(1, self.a.tries + 1):
            m.tries = attempt
            if ble:
                await asyncio.sleep(self.ble_delay())                  # BLE uplink
            if not ble or random.random() >= self.loss(d):              # else: lost on the radio
                self.pending[key] = m
                if m.t_publish is None:
                    m.t_publish = time.monotonic()
                m.t_last_publish = time.monotonic()
                await self.c.publish(f"ars/{self.room}/dev/{d.dev:02d}/{kind}", json.dumps(payload), qos=1)
            try:
                await asyncio.wait_for(m.event.wait(), self.a.ack_timeout)
                break
            except asyncio.TimeoutError:
                continue                                               # resend the SAME seq
        if m.code is None:
            m.code = "NO_ACK"
        if self.pending.get(key) is m:
            del self.pending[key]
        return m

    def on_ack(self, dev: int, data: dict) -> None:
        m = self.pending.get((dev, data.get("seq")))
        if m is None or m.code is not None:
            return
        if m.t_mqtt_ack is None:
            m.t_mqtt_ack = time.monotonic()
        d = self.by_dev[dev]
        if random.random() < self.loss(d):
            return                                                     # ack lost on the radio

        async def downlink():
            await asyncio.sleep(self.ble_delay())
            if m.code is None:
                m.code, m.t_ack = data.get("code", "?"), time.monotonic()
                m.ack_choice = data.get("choice")
                m.event.set()
        self.spawn(downlink())

    # ---------- behaviour: registration
    async def register(self, d: Device, mssv: str | None = None, expect: set | None = None,
                       note: str = "", delay: float | None = None) -> Msg:
        burst = d.scenario == "burst"
        if delay is None:
            delay = 0 if burst else min(random.lognormvariate(math.log(1.2), 0.5), 5)   # typing the MSSV
        await asyncio.sleep(delay)
        m = await self.send(d, "register", {"dev": d.dev, "mssv": mssv or d.mssv, "seq": d.next_seq()},
                            expect or {"OK"}, note, ble=not burst)
        if mssv is None or mssv == d.mssv:
            d.registered = m.code == "OK"
            d.reg_unknown = m.code == "NO_ACK"
        return m

    async def on_register(self, d: Device) -> None:
        d.registered = d.reg_unknown = False
        if d.scenario != "wrong_mssv":
            await self.register(d)
            return
        variant = random.choice(["not_in_class", "malformed", "stolen"])
        if variant == "stolen":
            await asyncio.sleep(random.uniform(2.0, 3.0))              # let others register first
            others = [o for o in self.devices if o.registered and o is not d]
            if not others:
                variant = "not_in_class"
        if variant == "not_in_class":
            wrong = "99" + "".join(random.choices("0123456789", k=6))
            m = await self.register(d, wrong, {"NOT_IN_CLASS"} if self.has_class else {"OK"}, "MSSV not on the class list")
        elif variant == "malformed":
            m = await self.register(d, d.mssv[:-1], {"BAD"}, "MSSV with 7 digits")
        else:
            m = await self.register(d, random.choice(others).mssv, {"DUP"}, "another student's MSSV", delay=0)
        if m.code != "NO_ACK":
            await self.register(d, note="corrected MSSV", delay=random.uniform(1.0, 2.0))

    # ---------- behaviour: answering
    def answer_expect(self, d: Device) -> set:
        if d.registered:
            return {"OK"}
        return {"OK", "UNREGISTERED"} if d.reg_unknown else {"UNREGISTERED"}

    async def answer(self, d: Device, q: int, n: int, deadline: float, delay: float,
                     expect: set | None = None, note: str = "", known_issue: bool = False,
                     q_sent: int | None = None, ble: bool = True) -> Msg:
        await asyncio.sleep(max(0.0, delay))
        payload = {"dev": d.dev, "q": q_sent or q, "choice": random.randint(1, n), "seq": d.next_seq()}
        if not d.registered:                       # registration failed or never confirmed
            expect = self.answer_expect(d) | ({"DUP", "CLOSED", "LOCKED"} if d.reg_unknown else set())
        if d.stale_seq and not known_issue:
            known_issue, note = True, "after reboot without re-register (seq still behind)"
        return await self.send(d, "answer", payload, expect or self.answer_expect(d), note,
                               known_issue, deadline, ble)

    async def on_question(self, d: Device, q: int, n: int, window_s: float, n_questions: int | None) -> None:
        deadline = time.monotonic() + window_s
        s = d.scenario

        if s == "burst":
            await self.answer(d, q, n, deadline, 0, note="burst", ble=False)
            return
        if s == "late":
            if q == 1:   # one answer for the wrong question, sent on time
                await self.answer(d, q, n, deadline, self.reaction(window_s), {"CLOSED"}, "wrong question", q_sent=q + 1)
            await self.answer(d, q, n, deadline, deadline - time.monotonic() + random.uniform(0.3, 1.5),
                              {"CLOSED"}, "after the deadline")
            return
        if s == "dropout" and random.random() < 0.4:
            return                                                     # out of range this question
        if s == "reboot" and not d.rebooted and q == max(1, min(2, n_questions or 2)):
            d.rebooted, d.seq = True, 0                                # power cycle: seq restarts at 1
            await asyncio.sleep(random.uniform(0.5, 1.5))              # boot time
            if random.random() < 0.5:
                await self.register(d, note="re-register after reboot", delay=0)
                await self.answer(d, q, n, deadline, self.reaction(window_s) * 0.5, note="after reboot")
            else:
                d.stale_seq = True
                await self.answer(d, q, n, deadline, self.reaction(window_s) * 0.5, {"DUP"},
                                  "reboot without re-register (seq reset)", known_issue=True)
            return
        if s == "spam":   # presses CONFIRM 3-6 times quickly: only one can lock, the rest get LOCKED
            delay = self.reaction(window_s) * 0.7
            for _ in range(random.randint(3, 6)):
                self.spawn(self.answer(d, q, n, deadline, delay, {"OK", "LOCKED"}, "rapid confirms"))
                delay += random.uniform(0.05, 0.25)
            return
        if s == "normal" and random.random() < 0.08:
            return                                                     # this student never answers
        # the student moves between A-D on the device (nothing is sent), then presses CONFIRM once
        m = await self.answer(d, q, n, deadline, self.reaction(window_s))
        if random.random() < 0.10 and m.code == "OK":                  # tries to change a locked answer
            remaining = deadline - time.monotonic()
            if remaining > 1.0:
                await self.answer(d, q, n, deadline, random.uniform(0.3, remaining * 0.6),
                                  {"LOCKED"}, "change after lock")

    # ---------- following the session state
    async def find_session(self) -> None:
        try:
            async with httpx.AsyncClient(base_url=self.a.api, timeout=5) as http:
                s = (await http.get("/sessions/active", params={"room": self.room})).json()
            if s:
                self.session_id, self.has_class = s["id"], s["class_id"] is not None
        except Exception as exc:
            print(f"(could not read the session from {self.a.api}: {exc})")

    async def run(self, done: asyncio.Event | None = None) -> None:
        last = None
        n_questions = None
        async for msg in self.c.messages:
            topic = msg.topic.value
            try:
                data = json.loads(msg.payload or b"{}")
            except ValueError:
                continue
            if topic.endswith("/ack"):
                self.on_ack(int(topic.split("/")[3]), data)
                continue
            if not self.armed:
                continue
            phase, q = data.get("phase"), data.get("q")
            if (phase, q) == last:
                continue
            last = (phase, q)
            print(f"state: {phase}{f' q{q}' if phase == 'QUESTION_OPEN' else ''}")
            if phase != "REGISTER" and self.register_closed_at is None:
                self.register_closed_at = time.monotonic()
            if phase == "REGISTER":
                self.register_closed_at = None
                self.msgs = []
                for d in self.devices:
                    d.rebooted = d.stale_seq = False
                await self.find_session()
                if self.session_id:
                    async with httpx.AsyncClient(base_url=self.a.api, timeout=5) as http:
                        n_questions = (await http.get(f"/sessions/{self.session_id}")).json()["n_questions"]
                for d in random.sample(self.devices, len(self.devices)):      # random order
                    self.spawn(self.on_register(d))
            elif phase == "QUESTION_OPEN" and q:
                window = (data.get("deadline_ms") or 20000) / 1000
                for d in random.sample(self.devices, len(self.devices)):
                    self.spawn(self.on_question(d, q, data.get("n_choices", 4), window, n_questions))
            elif phase == "ENDED" and self.msgs:
                # report in the background: the devices must keep listening, because the teacher
                # may open the next session right away (waiting here made them register too late)
                snap = (self.msgs, self.session_id, self.has_class, self.register_closed_at)
                self.msgs = []
                self.spawn(self.finish_report(snap, done))
                if done:
                    return

    async def finish_report(self, snap, done) -> None:
        await asyncio.sleep(self.a.ack_timeout * self.a.tries + 1)            # let retries finish
        ok = await self.report(*snap)
        if done:
            done.ok = ok
            done.set()
        else:
            print("waiting for the next session (Ctrl+C to stop)\n")

    # ---------- report
    async def report(self, msgs: list, sid: int | None, has_class: bool, reg_closed: float | None) -> bool:
        a = self.a
        print("\n" + "=" * 78)
        print(f"TEST REPORT  test={a.test}  devices={len(self.devices)}  session={sid}  "
              f"class list={'yes' if has_class else 'no'}  BLE {a.ble_ms} ms, loss {a.ble_loss:.0%}")
        print("=" * 78)
        codes = ["OK", "LOCKED", "DUP", "CLOSED", "NOT_IN_CLASS", "BAD", "UNREGISTERED", "NO_ACK"]
        used = sorted({d.scenario for d in self.devices}, key=SCENARIOS.index)
        print(f"{'scenario':<11}{'dev':>4}{'msgs':>6}{'retry':>6}" + "".join(f"{c[:6]:>8}" for c in codes) + f"{'wrong':>7}")
        unexpected, known = [], []
        for s in used:
            ms = [m for m in msgs if self.by_dev[m.dev].scenario == s]
            row = {c: sum(1 for m in ms if m.code == c) for c in codes}
            bad = []
            for m in ms:
                good = m.code in m.expect
                # an answer sent in the last moment may legitimately be too late
                if not good and m.code == "CLOSED" and m.deadline and m.t_last_publish and m.t_last_publish > m.deadline - 0.3:
                    good = True
                # registration still being (re)sent when the teacher started the quiz: too late, normal
                if (not good and m.kind == "register" and m.code == "CLOSED" and reg_closed
                        and m.t_last_publish and m.t_last_publish >= reg_closed - 0.3):
                    good = True
                if not good and m.code == "NO_ACK" and a.ble_loss + (0.2 if s == "lossy" else 0) > 0:
                    good = True                                        # every try lost on the radio
                if not good:
                    (known if m.known_issue else bad).append(m)
            unexpected += bad
            retries = sum(m.tries - 1 for m in ms)
            n_dev = sum(1 for d in self.devices if d.scenario == s)
            print(f"{s:<11}{n_dev:>4}{len(ms):>6}{retries:>6}" + "".join(f"{row[c]:>8}" for c in codes) + f"{len(bad):>7}")

        # latency
        def pct(xs, p):
            xs = sorted(xs)
            return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]
        e2e = [(m.t_ack - m.t_press) * 1000 for m in msgs if m.t_ack and m.tries == 1]
        srv = [(m.t_mqtt_ack - m.t_publish) * 1000 for m in msgs if m.t_mqtt_ack and m.t_publish and m.tries == 1]
        print()
        if e2e:
            print(f"Latency press -> ack on the device (BLE up + server + BLE down), first try, n={len(e2e)}:")
            print(f"   p50 {pct(e2e, 50):.0f} ms   p95 {pct(e2e, 95):.0f} ms   p99 {pct(e2e, 99):.0f} ms   "
                  f"max {max(e2e):.0f} ms   over 1 s: {sum(1 for x in e2e if x > 1000)}")
        if srv:
            print(f"Server only (MQTT publish -> ack on MQTT), n={len(srv)}:")
            print(f"   p50 {pct(srv, 50):.0f} ms   p95 {pct(srv, 95):.0f} ms   max {max(srv):.0f} ms")
        retried = [m for m in msgs if m.tries > 1]
        if retried:
            print(f"Retries: {len(retried)} messages needed a resend; "
                  f"{sum(1 for m in retried if m.code in ('OK', 'LOCKED'))} got through in the end")

        # burst: everything pressed at the same instant
        for kind in ("register", "answer"):
            b = [m for m in msgs if self.by_dev[m.dev].scenario == "burst" and m.kind == kind
                 and m.t_ack and m.tries == 1]
            if len(b) > 1:
                groups: dict[int, list[Msg]] = {}
                for m in b:
                    groups.setdefault(m.payload.get("q", 0), []).append(m)
                worst = max(groups.values(), key=len)
                t0 = min(m.t_press for m in worst)
                print(f"Burst {kind}: {len(worst)} sent at the same instant -> all acked (first try) within "
                      f"{(max(m.t_ack for m in worst) - t0) * 1000:.0f} ms")

        # integrity: the server is the only judge. For every device x question:
        #   - every ack that names a locked choice (OK / LOCKED) names the SAME choice
        #   - that choice is what the database stored
        #   - at most one message (one seq) was accepted as OK
        mismatches = []
        if sid:
            async with httpx.AsyncClient(base_url=a.api, timeout=5) as http:
                grid = (await http.get(f"/sessions/{sid}/answers")).json()
            stored = {(g["device_no"], g["q"]): g["choice"] for g in grid}
            by_key: dict[tuple[int, int], list[Msg]] = {}
            for m in msgs:
                if m.kind == "answer" and m.code in ("OK", "LOCKED"):
                    by_key.setdefault((m.dev, m.payload["q"]), []).append(m)
            for k, ms in by_key.items():
                told = {m.ack_choice for m in ms}
                ok_seqs = {m.payload["seq"] for m in ms if m.code == "OK"}
                if told != {stored.get(k)} or len(ok_seqs) > 1:
                    mismatches.append((k, sorted(told, key=str), stored.get(k)))
            print(f"Integrity: {len(by_key)} device x question locked answers checked "
                  f"(ack == database, one lock per question), {len(mismatches)} mismatch(es)")
            for (dev, q), told, got in mismatches[:5]:
                print(f"   device {dev:02d} ({self.by_dev[dev].scenario}) q{q}: acks said {told}, database has {got}")
                for m in msgs:
                    if m.dev == dev and m.kind == "answer" and m.payload["q"] == q:
                        print(f"      press {m.press_no} seq {m.payload['seq']} choice {m.payload['choice']} "
                              f"tries {m.tries} -> {m.code} (locked {m.ack_choice})")

        if known:
            print(f"Known issue (round 2): {len(known)} message(s): "
                  + "; ".join(sorted({m.note for m in known})))
        for m in unexpected[:8]:
            print(f"   UNEXPECTED dev {m.dev:02d} {m.kind} {json.dumps(m.payload)} {m.note or ''}: "
                  f"got {m.code}, expected {'/'.join(sorted(m.expect))}")
        ok = not unexpected and not mismatches
        print(f"\nRESULT: {'PASS' if ok else 'FAIL'}" + (f"  ({len(unexpected)} unexpected ack(s))" if unexpected else ""))
        print("=" * 78 + "\n")
        return ok


# ---------------------------------------------------------------- self-driving teacher
async def drive(args, class_id: int | None, sim: "Sim") -> None:
    async with httpx.AsyncClient(base_url=args.api, timeout=10) as http:
        old = (await http.get("/sessions/active", params={"room": args.room})).json()
        if old:                                   # end it from whatever phase it is in
            if old["phase"] == "IDLE":
                await http.post(f"/sessions/{old['id']}/start")
            if old["phase"] == "QUESTION_OPEN":
                await http.post(f"/sessions/{old['id']}/close")
            r = await http.post(f"/sessions/{old['id']}/end")
            if r.is_success:
                print(f"(ended the running session {old['id']} in {args.room})")
            else:
                sys.exit(f"cannot end session {old['id']}: {r.text}")
        opts = ["A", "B", "C", "D"]
        r = await http.post("/quizzes", json={
            "title": f"fake_devices test ({args.test})", "subject": "test",
            "questions": [{"text": f"Test question {i}", "options": opts, "correct_choice": random.randint(1, 4),
                           "time_limit_s": args.q_time, "review_delay_s": 0} for i in range(1, args.questions + 1)]})
        r.raise_for_status()
        quiz = r.json()
        r = await http.post("/sessions", json={"quiz_id": quiz["id"], "room": args.room, "class_id": class_id})
        r.raise_for_status()
        sid = r.json()["id"]
        sim.armed = True            # from now on the devices follow THIS session only
        print(f"drive: quiz {quiz['id']}, session {sid}, {args.questions} questions x {args.q_time} s")
        await asyncio.sleep(1)
        await http.post(f"/sessions/{sid}/start")
        await asyncio.sleep(args.reg_wait)
        for _ in range(args.questions):
            await http.post(f"/sessions/{sid}/next")
            await asyncio.sleep(args.q_time + 2.5)          # the server closes the question itself
        await http.post(f"/sessions/{sid}/end")


# ---------------------------------------------------------------- main
async def main(args) -> int:
    if args.seed is not None:
        random.seed(args.seed)
    mssvs = [f"221340{str(i).zfill(2)}" for i in range(1, args.n + 1)]
    if args.mssv_from_class:
        rows = httpx.get(f"{args.api}/classes/{args.mssv_from_class}/students").json()
        mssvs = [r["mssv"] for r in rows][: args.n]
        print(f"using {len(mssvs)} MSSVs from class {args.mssv_from_class}")
        if len(mssvs) < args.n:
            print(f"WARNING: you asked for {args.n} devices but the class list has only {len(mssvs)} students")

    test = random.choice(SCENARIOS) if args.test == "random" else args.test
    if args.test == "random":
        print(f"random test chosen: {test}")
    args.test = test
    if test == "all":
        plan = [SCENARIOS[i % len(SCENARIOS)] for i in range(len(mssvs))]
        random.shuffle(plan)
    else:
        plan = [test] * len(mssvs)
    # device numbers 1, 2, 3 ... skipping the real clickers' numbers (--avoid-devs), so a real
    # Pico (e.g. PicoRemote_7) never shares a number with a fake device
    avoid = {int(x) for x in str(args.avoid_devs or "").replace(" ", "").split(",") if x}
    numbers = [n for n in range(1, 61) if n not in avoid][: len(mssvs)]
    if len(numbers) < len(mssvs):
        print(f"WARNING: only {len(numbers)} free device numbers (1-60) - using {len(numbers)} fake devices")
        mssvs, plan = mssvs[: len(numbers)], plan[: len(numbers)]
    devices = [Device(n, m, s) for n, m, s in zip(numbers, mssvs, plan)]
    if avoid:
        print(f"device numbers {', '.join(f'{n:02d}' for n in sorted(avoid))} left free for real clickers")
    counts = {s: plan.count(s) for s in SCENARIOS if s in plan}
    print("devices per scenario: " + ", ".join(f"{s} {c}" for s, c in counts.items()))

    quiet = logging.getLogger("fake_devices.mqtt")
    quiet.setLevel(logging.ERROR)              # hide "There are N pending publish calls" during bursts
    async with aiomqtt.Client(hostname=args.broker, port=args.port, identifier=f"fake-devices-{args.room}",
                              max_inflight_messages=100, logger=quiet) as client:
        await client.subscribe(f"ars/{args.room}/state", qos=1)
        await client.subscribe(f"ars/{args.room}/dev/+/ack", qos=1)
        print(f"{len(devices)} fake devices on ars/{args.room}/... (broker {args.broker}:{args.port})")
        sim = Sim(client, args, devices)
        if not args.drive:
            await sim.run()
            return 0
        done = asyncio.Event()
        done.ok = False
        runner = asyncio.create_task(sim.run(done))
        await drive(args, args.class_id if args.class_id is not None else args.mssv_from_class, sim)
        await asyncio.wait_for(done.wait(), timeout=args.ack_timeout * args.tries + 30)
        runner.cancel()
        return 0 if done.ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", type=int, default=30, help="number of devices (default 30)")
    p.add_argument("--test", default="normal", choices=SCENARIOS + ["random", "all"], help="scenario (default normal)")
    p.add_argument("--avoid-devs", metavar="NUMS", help='device numbers used by real clickers, e.g. "7" or "7,12": fake devices skip them')
    p.add_argument("--room", default="room1")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1884)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--mssv-from-class", type=int, default=None, help="class id: use its student list")
    p.add_argument("--ble-ms", type=float, default=40, help="median BLE delay per hop in ms (default 40, 0 = off)")
    p.add_argument("--ble-interval", type=float, default=30, help="BLE connection interval jitter in ms (default 30)")
    p.add_argument("--ble-loss", type=float, default=0.02, help="share of BLE packets lost per hop (default 0.02)")
    p.add_argument("--ack-timeout", type=float, default=1.5, help="seconds before a device resends (default 1.5)")
    p.add_argument("--tries", type=int, default=3, help="sends per message incl. resends (default 3)")
    p.add_argument("--seed", type=int, default=None, help="random seed, to repeat exactly the same run")
    g = p.add_argument_group("self-driving mode")
    g.add_argument("--drive", action="store_true", help="create and run the session without the page")
    g.add_argument("--class-id", type=int, default=None, help="class list for the session (default: --mssv-from-class)")
    g.add_argument("--questions", type=int, default=3)
    g.add_argument("--q-time", type=int, default=8, help="seconds per question (min 5)")
    g.add_argument("--reg-wait", type=float, default=6, help="seconds of registration before question 1")
    a = p.parse_args()
    try:
        if sys.platform == "win32":
            code = asyncio.run(main(a), loop_factory=asyncio.SelectorEventLoop)
        else:
            code = asyncio.run(main(a))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)
