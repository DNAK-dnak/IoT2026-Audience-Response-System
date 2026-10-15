"""BLE <-> MQTT bridge: connects the hardware team's PicoRemote clickers to the backend.

Built on the hardware team's ble_receiver.py, with THEIR protocol unchanged:
  - clickers advertise as "PicoRemote_<dev>", GATT service 0x1234
  - TX characteristic 0x5678 (Pico -> laptop, notify): JSON
        {"dev":7,"mssv":"22134007","hw":"...","seq":3}   register
        {"dev":7,"q":1,"choice":2,"seq":4}               answer
        {"dev":7,"key":"5"}                               keystroke while typing the MSSV (only logged)
  - RX characteristic 0x9abc (laptop -> Pico, write): JSON {"cmd": "..."}
        MODE_1 = wait   MODE_2 = enter MSSV   MODE_3 = answer   UNLOCK = keys usable again

What the bridge adds:
  1. Every register/answer is published to MQTT (ars/<room>/dev/NN/register|answer), exactly
     like any other device. The backend decides everything (class list, deadline, lock).
  2. The session state from the backend (retained ars/<room>/state) becomes MODE_x commands:
        IDLE / ENDED / no session   -> MODE_1
        REGISTER                    -> MODE_2 (MODE_1 once the MSSV was accepted)
        QUESTION_OPEN               -> MODE_3 + {"q","n","deadline_ms"} (only registered devices)
        QUESTION_CLOSED             -> MODE_1
  3. The backend's ack is written back to the clicker:
        {"cmd":"ACK","kind":"answer","seq":4,"code":"OK","q":1,"choice":2}
     The clicker should show "locked" ONLY from this ack (code OK / LOCKED, using "choice").
     If the answer was refused while the question is still open, the bridge also sends UNLOCK.
     Clickers that do not know ACK yet ignore it (they read only "cmd"), so nothing breaks.
  4. When a clicker (re)connects, it gets the right mode at once (state recovery), using the
     backend's real registration list instead of the mock database.
  5. --fill-q (default on): the current firmware always sends "q":1. The bridge replaces it with
     the question that is open now. Turn it off (--no-fill-q) once the firmware uses the "q"
     it receives with MODE_3.

PHONE TEST MODE (--phone): a phone browser can only CONNECT to a Bluetooth device (Web Bluetooth),
it cannot BE one. So for phones the roles swap: the laptop advertises the same service 0x1234
(with bless, like mock_pico.py) and web/phone_ble.html on the phone connects to it.
Same characteristics, same JSON, same commands:
    phone -> laptop : write 0x5678   {"dev":59,"hello":true} on connect, then register / answer
    laptop -> phone : notify 0x9abc  {"dev":59,"cmd":"MODE_3",...}  (every command carries "dev";
                                      several phones share the characteristic and keep only theirs)

Run on the laptop that has Bluetooth (NOT in Docker), backend folder, venv active:
    python -m pip install bleak paho-mqtt httpx
    python scripts/ble_bridge.py                     # broker localhost:1884, room1
    python scripts/ble_bridge.py --room room1 --port 1884 --api http://127.0.0.1:8000
    python -m pip install bless                      # once, for phones
    python scripts/ble_bridge.py --phone             # phones connect to the laptop (phone_ble.html)
Use device numbers that the fake devices do not use (e.g. fake devices -n 50, clickers 51..60).
"""
import argparse
import asyncio
import json
import logging
import sys

import httpx
import paho.mqtt.client as paho

SERVICE_UUID = "00001234-0000-1000-8000-00805f9b34fb"
TX_CHAR_UUID = "00005678-0000-1000-8000-00805f9b34fb"   # Pico -> laptop (notify)
RX_CHAR_UUID = "00009abc-0000-1000-8000-00805f9b34fb"   # laptop -> Pico (write)
NAME_PREFIX = "PicoRemote"

log = logging.getLogger("bridge")


# ===================================================================== bridge logic (no BLE here)
class Bridge:
    """Everything between the clickers and MQTT. The BLE part only calls
    link_up / from_device / link_down, so this class can be tested without Bluetooth."""

    def __init__(self, loop: asyncio.AbstractEventLoop, args):
        self.loop, self.a = loop, args
        self.links: dict[int, "Link"] = {}          # dev -> connected clicker
        self.registered: set[int] = set()           # devs whose MSSV the backend accepted
        self.state: dict = {}                       # last retained state message
        self.session_id: int | None = None
        self.mqtt = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"ble-bridge-{args.room}-{'phone' if getattr(args, 'phone', False) else 'pico'}")
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = lambda c, u, m: loop.call_soon_threadsafe(self._on_mqtt, m.topic, m.payload)
        self.mqtt.max_inflight_messages_set(100)

    # ---------- MQTT (paho runs its own network thread; messages are handed to asyncio)
    def start_mqtt(self) -> None:
        self.mqtt.connect_async(self.a.broker, self.a.port, keepalive=30)
        self.mqtt.loop_start()

    def _on_connect(self, client, userdata, flags, reason, props) -> None:
        log.info("MQTT connected to %s:%s (%s)", self.a.broker, self.a.port, reason)
        client.subscribe([(f"ars/{self.a.room}/state", 1), (f"ars/{self.a.room}/dev/+/ack", 1)])

    def publish(self, dev: int, kind: str, payload: dict) -> None:
        self.mqtt.publish(f"ars/{self.a.room}/dev/{dev:02d}/{kind}", json.dumps(payload), qos=1)

    def _on_mqtt(self, topic: str, raw: bytes) -> None:
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return
        if topic.endswith("/state"):
            self._on_state(data)
        elif topic.endswith("/ack"):
            self._on_ack(int(topic.split("/")[3]), data)

    # ---------- state -> MODE commands
    def command_for(self, dev: int) -> dict:
        phase = self.state.get("phase")
        if phase == "REGISTER":
            return {"cmd": "MODE_1"} if dev in self.registered else {"cmd": "MODE_2"}
        if phase == "QUESTION_OPEN" and dev in self.registered:
            cmd = {"cmd": "MODE_3", "q": self.state.get("q"), "n": self.state.get("n_choices", 4)}
            if self.state.get("deadline_ms") is not None:
                cmd["deadline_ms"] = self.state["deadline_ms"]
            return cmd
        return {"cmd": "MODE_1"}                    # IDLE, ENDED, CLOSED, or not registered

    def _on_state(self, s: dict) -> None:
        old = self.state.get("phase")
        if s.get("phase") == "REGISTER" and old != "REGISTER":
            self.registered.clear()                 # a new session: everybody registers again
            self.loop.create_task(self.refresh_session())
        self.state = s
        log.info("state %s%s -> %d clicker(s)", s.get("phase"), f" q{s['q']}" if s.get("q") else "", len(self.links))
        for dev, link in list(self.links.items()):
            link.send(self.command_for(dev))

    # ---------- acks -> clickers
    def _on_ack(self, dev: int, ack: dict) -> None:
        link = self.links.get(dev)
        if link is None:
            return                                  # not one of our clickers (fake device, phone...)
        kind = link.sent.pop(ack.get("seq"), None)
        if kind is None:
            return                                  # an ack for a message we did not send
        code = ack.get("code")
        link.send({"cmd": "ACK", "kind": kind, **ack})
        phase = self.state.get("phase")
        if kind == "register":
            if code == "OK":
                self.registered.add(dev)
                link.send(self.command_for(dev))    # MODE_1 (wait) or MODE_3 if a question is open
            else:
                link.send({"cmd": "MODE_2"})        # let the student type the MSSV again
        else:  # answer
            if code in ("OK", "LOCKED"):
                return                              # locked: keys stay locked on the clicker
            if code in ("UNREGISTERED", "DUP"):
                self.registered.discard(dev)        # DUP here = seq behind after a reboot
                link.send({"cmd": "MODE_2"} if code == "DUP" else {"cmd": "MODE_1"})
            elif phase == "QUESTION_OPEN":
                link.send({"cmd": "UNLOCK"})        # e.g. BAD: let the student try again
        log.info("ack dev %02d %s seq %s -> %s%s", dev, kind, ack.get("seq"), code,
                 f" choice {ack['choice']}" if "choice" in ack else "")

    # ---------- clickers -> MQTT
    def from_device(self, link: "Link", data: bytes) -> None:
        try:
            p = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            log.warning("clicker %s sent something that is not JSON: %r", link.name, data[:60])
            return
        dev = p.get("dev")
        if not isinstance(dev, int):
            log.warning("clicker %s: no device number in %s", link.name, p)
            return
        if link.dev != dev:                         # trust the message's dev, remember the link under it
            self.links.pop(link.dev, None)
            link.dev = dev
        self.links[dev] = link
        if "mssv" in p:
            kind = "register"
        elif "choice" in p:
            kind = "answer"
            if self.a.fill_q and self.state.get("phase") == "QUESTION_OPEN":
                p["q"] = self.state.get("q")        # firmware sends q=1 for every question
        elif "key" in p:
            log.debug("dev %02d typing %s", dev, p["key"])
            return
        else:
            log.warning("dev %02d: unknown message %s", dev, p)
            return
        if isinstance(p.get("seq"), int):
            link.sent[p["seq"]] = kind
        self.publish(dev, kind, p)
        log.info("dev %02d %s %s", dev, kind, json.dumps(p))

    def from_phone(self, data: bytes, hub: "PhoneHub") -> None:
        """A write from a phone (phone mode). The phone is identified by the "dev" in its JSON."""
        try:
            p = json.loads(data.decode("utf-8"))
            dev = int(p["dev"])
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            log.warning("phone sent something unusable: %r", data[:60])
            return
        link = self.links.get(dev)
        new = not isinstance(link, PhoneLink)
        if new:
            link = PhoneLink(dev, hub)
            self.links[dev] = link
            log.info("[+] phone clicker %02d", dev)
        if p.get("hello"):
            self.loop.create_task(self.link_up(link))   # tell it the current mode
            return
        if new:
            self.loop.create_task(self.link_up(link))
        self.from_device(link, data)

    async def link_up(self, link: "Link") -> None:
        if link.dev is not None:
            self.links[link.dev] = link
            if self.session_id is None:
                await self.refresh_session()
        link.send(self.command_for(link.dev) if link.dev is not None else {"cmd": "MODE_1"})

    def link_down(self, link: "Link") -> None:
        if link.dev is not None and self.links.get(link.dev) is link:
            del self.links[link.dev]

    async def refresh_session(self) -> None:
        """Ask the backend which devices are registered (state recovery after a reconnect)."""
        try:
            async with httpx.AsyncClient(base_url=self.a.api, timeout=3) as http:
                s = (await http.get("/sessions/active", params={"room": self.a.room})).json()
                self.session_id = s["id"] if s else None
                if s:
                    devs = (await http.get(f"/sessions/{s['id']}/devices")).json()
                    self.registered = {d["device_no"] for d in devs}
        except Exception as exc:
            log.warning("could not read the session from %s: %s", self.a.api, exc)


class Link:
    """One connected clicker. send() queues a command; a writer task writes them in order."""

    def __init__(self, name: str, dev: int | None, writer):
        self.name, self.dev = name, dev
        self.sent: dict[int, str] = {}              # seq -> kind, to route the ack back
        self.queue: asyncio.Queue = asyncio.Queue()
        self._writer = writer                       # async function(bytes)

    def send(self, cmd: dict) -> None:
        self.queue.put_nowait(cmd)

    async def write_loop(self) -> None:
        while True:
            cmd = await self.queue.get()
            try:
                await self._writer(json.dumps(cmd, separators=(",", ":")).encode())
                log.debug("-> %s %s", self.name, cmd)
            except Exception as exc:
                log.warning("write to %s failed: %s", self.name, exc)


class PhoneLink(Link):
    """A phone in phone mode: commands go through the shared notify characteristic, tagged with dev."""

    def __init__(self, dev: int, hub: "PhoneHub"):
        super().__init__(f"phone_{dev}", dev, None)
        self.hub = hub

    def send(self, cmd: dict) -> None:
        self.hub.queue.put_nowait({"dev": self.dev, **cmd})


class PhoneHub:
    """One notify characteristic shared by all phones: send commands one by one, in order."""

    def __init__(self, notify):
        self.queue: asyncio.Queue = asyncio.Queue()
        self._notify = notify                       # function(bytes): set the value and notify

    async def run(self) -> None:
        while True:
            cmd = await self.queue.get()
            try:
                self._notify(json.dumps(cmd, separators=(",", ":")).encode())
                log.debug("-> phone %s", cmd)
            except Exception as exc:
                log.warning("notify failed: %s", exc)
            await asyncio.sleep(0.03)               # let each notification go out before the next


def dev_from_name(name: str | None) -> int | None:
    """'PicoRemote_7' -> 7"""
    try:
        return int((name or "").rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None


# ===================================================================== BLE (bleak)
async def run_ble(bridge: Bridge) -> None:
    from bleak import BleakClient, BleakScanner     # imported here: the logic above works without bleak

    busy: set[str] = set()                          # MAC addresses being handled

    async def handle(device) -> None:
        busy.add(device.address)
        link = None
        try:
            async with BleakClient(device) as client:
                async def write(data: bytes) -> None:
                    await client.write_gatt_char(RX_CHAR_UUID, data, response=True)

                link = Link(device.name, dev_from_name(device.name), write)
                writer = asyncio.create_task(link.write_loop())
                log.info("[+] connected %s (%s)", device.name, device.address)
                await client.start_notify(TX_CHAR_UUID, lambda _s, data: bridge.from_device(link, bytes(data)))
                await bridge.link_up(link)
                while client.is_connected:
                    await asyncio.sleep(0.5)
                writer.cancel()
        except Exception as exc:
            log.info("connection to %s ended: %s", device.name, exc)
        finally:
            if link:
                bridge.link_down(link)
            busy.discard(device.address)
            log.info("[-] lost %s (%s); waiting for it to come back", device.name, device.address)

    log.info("scanning for %s_* clickers ...", NAME_PREFIX)
    while True:
        for d in await BleakScanner.discover(timeout=2.0):
            if d.name and d.name.startswith(NAME_PREFIX) and d.address not in busy:
                log.info("found %s (%s), connecting", d.name, d.address)
                asyncio.create_task(handle(d))
        await asyncio.sleep(0.5)


# ===================================================================== phone mode (bless)
async def run_phone_gateway(bridge: Bridge) -> None:
    from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

    server = BlessServer(name=bridge.a.name, loop=asyncio.get_running_loop())

    def notify(data: bytes) -> None:
        server.get_characteristic(RX_CHAR_UUID).value = bytearray(data)
        server.update_value(SERVICE_UUID, RX_CHAR_UUID)

    hub = PhoneHub(notify)

    def on_write(characteristic, value, **kwargs) -> None:
        if str(characteristic.uuid).lower() == TX_CHAR_UUID:
            bridge.loop.call_soon_threadsafe(bridge.from_phone, bytes(value), hub)

    def on_read(characteristic, **kwargs) -> bytearray:
        return characteristic.value or bytearray()

    server.write_request_func = on_write
    server.read_request_func = on_read
    await server.add_new_service(SERVICE_UUID)
    # argument order: service, characteristic, properties, value, permissions
    await server.add_new_characteristic(
        SERVICE_UUID, TX_CHAR_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
        None, GATTAttributePermissions.writeable)
    await server.add_new_characteristic(
        SERVICE_UUID, RX_CHAR_UUID,
        GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
        None, GATTAttributePermissions.readable)
    await server.start()
    log.info("phone mode: advertising service %s as '%s'. Open phone_ble.html on the phone.",
             SERVICE_UUID[4:8], bridge.a.name)
    await hub.run()


async def main(args) -> None:
    bridge = Bridge(asyncio.get_running_loop(), args)
    bridge.start_mqtt()
    await bridge.refresh_session()
    if args.phone:
        await run_phone_gateway(bridge)
    else:
        await run_ble(bridge)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--room", default="room1")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1884)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--no-fill-q", dest="fill_q", action="store_false",
                   help="send the clicker's own q (only when the firmware tracks the question number)")
    p.add_argument("--phone", action="store_true",
                   help="phone test mode: the laptop advertises, phones connect with phone_ble.html")
    p.add_argument("--name", default="ARS-Gateway", help="advertised name in phone mode")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("bleak").setLevel(logging.WARNING)
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        pass
