"""MQTT: connection, the ingestion worker, and publishing helpers.

Flow for every incoming message (the heart of the data pipeline):

    1. parse the topic           ars/<room>/dev/<NN>/<register|answer>
    2. parse the payload          JSON -> dict, or None if it is not JSON
    3. RAW LAYER FIRST            insert into raw_events, whatever it is
    4. rules                      app/rules.py decides OK / BAD / DUP / CLOSED / UNREGISTERED
    5. CLEANED LAYER              OK -> session_devices (register) or answers (answer)
    6. record the final status    raw_events.status
    7. ack                        publish {"seq": ..., "code": ...} to .../ack
    8. live update                tell the teacher page (WebSocket, app/live.py)

The worker reconnects by itself if the broker restarts (aiomqtt 2.x does not).
"""
import asyncio
import json
import logging
import ssl
from dataclasses import dataclass

import aiomqtt
import psycopg
from psycopg.types.json import Jsonb

from app.config import settings
from app.queries import Q
from app.rules import check_answer, check_register

log = logging.getLogger("ars.mqtt")

# Subscribe ONLY to what devices send. A wildcard like ars/+/dev/+/+ would also match
# .../ack, and the backend would receive (and store) its own acks.
SUBSCRIPTIONS = ["ars/+/dev/+/register", "ars/+/dev/+/answer"]


def make_client() -> aiomqtt.Client:
    return aiomqtt.Client(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        identifier=settings.mqtt_client_id,
        username=settings.mqtt_user or None,   # "" -> None = no username
        password=settings.mqtt_pass or None,
        tls_context=ssl.create_default_context() if settings.mqtt_tls else None,
        # QoS 1 messages "in flight" (sent, ack not back yet). The default 20 makes a burst of
        # 60 acks wait in a queue; found with: fake_devices.py --test burst
        max_inflight_messages=100,
    )


# --------------------------------------------------------------------- publishing

async def publish(app, topic: str, payload: dict, retain: bool = False) -> None:
    """Publish JSON if the broker is connected; log instead of crashing if not."""
    client: aiomqtt.Client | None = getattr(app.state, "mqtt", None)
    if client is None:
        log.warning("MQTT not connected, dropped message for %s", topic)
        return
    await client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=retain)


async def publish_state(app, conn, session_id: int, room: str) -> dict:
    """Read the session state from the DB, publish it as the RETAINED state message
    for the devices, and send the same state to the teacher page."""
    cur = await conn.execute(Q["state_message"], {"session_id": session_id})
    state = await cur.fetchone()
    payload = {k: v for k, v in state.items() if v is not None}   # drop empty fields
    await publish(app, f"ars/{room}/state", payload, retain=True)
    await app.state.hub.send(session_id, {"type": "state", **payload})
    return payload


# --------------------------------------------------------------------- parsing

@dataclass
class Topic:
    room: str
    dev: int
    dev_str: str      # "07" as it appeared, so the ack goes back to the same topic
    kind: str         # "register" | "answer"


def parse_topic(topic: str) -> Topic | None:
    parts = topic.split("/")
    if (
        len(parts) == 5
        and parts[0] == "ars"
        and parts[2] == "dev"
        and parts[3].isdigit()
        and parts[4] in ("register", "answer")
    ):
        return Topic(room=parts[1], dev=int(parts[3]), dev_str=parts[3], kind=parts[4])
    return None


def parse_payload(raw: bytes) -> tuple[str, object | None]:
    text = raw.decode("utf-8", errors="replace")
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


# --------------------------------------------------------------------- handling one message

async def handle_message(app, topic_str: str, raw_payload: bytes) -> str:
    """Process one message end to end. Returns the final status (useful for tests/logs)."""
    pool = app.state.pool
    topic = parse_topic(topic_str)
    payload_text, payload = parse_payload(raw_payload)

    async with pool.connection() as conn:
        # which session does this belong to?
        session = None
        if topic is not None:
            cur = await conn.execute(Q["find_active_session"], {"room": topic.room})
            session = await cur.fetchone()

        # 3. RAW LAYER FIRST — even garbage is kept
        cur = await conn.execute(
            Q["insert_raw_event"],
            {
                "topic": topic_str,
                "dev": topic.dev if topic else None,
                "kind": topic.kind if topic else None,
                "payload_text": payload_text,
                "payload": Jsonb(payload) if payload is not None else None,
                "session_id": session["id"] if session else None,
            },
        )
        event = await cur.fetchone()

        # 4-5. rules + cleaned layer
        live: dict | None = None      # what to tell the teacher page afterwards
        if topic is None:
            status = "BAD"            # we cannot even tell which device sent it: no ack
        elif session is None:
            status = "NO_SESSION"
        elif topic.kind == "register":
            status, live = await _handle_register(conn, session, topic, payload)
        else:
            status, live = await _handle_answer(conn, session, topic, payload, event["received_at"])

        # 6. final status on the raw row
        await conn.execute(Q["set_event_status"], {"status": status, "event_id": event["id"]})

    # 7. ack back to the device (NO_SESSION is reported to the device as CLOSED)
    if topic is not None:
        seq = payload.get("seq") if isinstance(payload, dict) else None
        code = "CLOSED" if status == "NO_SESSION" else status
        await publish(app, f"ars/{topic.room}/dev/{topic.dev_str}/ack", {"seq": seq, "code": code})

    # 8. live update for the teacher page
    if session is not None and topic is not None:
        if live is None:              # rejected message: show it, so the teacher can help
            live = {"type": "ack", "dev": topic.dev, "kind": topic.kind, "code": status}
        await app.state.hub.send(session["id"], live)

    log.info("%s -> %s", topic_str, status)
    return status


async def _handle_register(conn, session, topic: Topic, payload) -> tuple[str, dict | None]:
    mssv = payload.get("mssv") if isinstance(payload, dict) else None
    owner, in_class, full_name = None, True, None
    if isinstance(mssv, str):
        params = {"session_id": session["id"], "mssv": mssv}
        row = await (await conn.execute(Q["mssv_owner"], params)).fetchone()
        owner = row["device_no"] if row else None
        roster = await (await conn.execute(Q["roster_check"], params)).fetchone()
        in_class = (not roster["has_roster"]) or roster["in_class"]
        full_name = roster["full_name"]

    status = check_register(payload, topic.dev, session["phase"], owner, in_class)
    if status == "OK":
        try:
            await conn.execute(
                Q["register_device"],
                {"session_id": session["id"], "dev": topic.dev, "mssv": mssv, "seq": payload["seq"]},
            )
        except psycopg.errors.UniqueViolation:
            status = "DUP"            # two devices raced for the same MSSV
        except psycopg.errors.ForeignKeyViolation:
            status = "BAD"            # device number not in the devices table
    if status == "OK":
        return status, {"type": "device", "dev": topic.dev, "mssv": mssv, "full_name": full_name}
    return status, None


async def _handle_answer(conn, session, topic: Topic, payload, received_at) -> tuple[str, dict | None]:
    ctx = None
    if isinstance(payload, dict) and isinstance(payload.get("q"), int):
        cur = await conn.execute(
            Q["answer_context"], {"session_id": session["id"], "q": payload["q"], "dev": topic.dev}
        )
        ctx = await cur.fetchone()

    status = check_answer(payload, topic.dev, ctx, received_at)
    if status == "OK":
        params = {
            "session_id": session["id"],
            "dev": topic.dev,
            "question_id": ctx["question_id"],
            "choice": payload["choice"],
            "seq": payload["seq"],
        }
        # both writes succeed or neither does
        async with conn.transaction():
            await conn.execute(Q["save_answer"], params)
            await conn.execute(Q["bump_last_seq"], params)
        count = await (await conn.execute(Q["answer_count"], {"session_id": session["id"]})).fetchone()
        # the choice itself is NOT sent while the question is open (the board shows only ✓)
        return status, {"type": "answer", "dev": topic.dev, "q": payload["q"], "answered": count["answered"]}
    return status, None


# --------------------------------------------------------------------- the worker task

async def run_worker(app) -> None:
    """Runs for the whole life of the app: connect, subscribe, handle messages, reconnect."""
    while True:
        try:
            async with make_client() as client:
                app.state.mqtt = client
                for topic in SUBSCRIPTIONS:
                    await client.subscribe(topic, qos=1)
                log.info("MQTT connected to %s:%s", settings.mqtt_host, settings.mqtt_port)
                async for message in client.messages:
                    try:
                        await handle_message(app, message.topic.value, message.payload)
                    except Exception:
                        # one bad message must never stop the worker
                        log.exception("failed to handle message on %s", message.topic.value)
        except aiomqtt.MqttError as exc:
            app.state.mqtt = None
            log.warning("MQTT connection lost (%s); retrying in 3 s", exc)
            await asyncio.sleep(3)
