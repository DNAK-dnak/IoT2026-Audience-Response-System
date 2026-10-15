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
    python scripts/ble_bridge.py --check             # what can this laptop's Bluetooth do?
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
# phone mode only: fallback service if Windows refuses to advertise 0x1234 (phone_ble.html knows both)
ALT_SERVICE_UUID = "a5e1c0de-1234-4b1d-8e3a-5a2c7f0e1234"

log = logging.getLogger("bridge")

# --broken-key1: clickers whose physical key 1 does not work. The bridge shifts their keys
# (physical 2 -> 1, 3 -> 2, ..., 9 -> 8; 0, ENTER and BACKSPACE unchanged; digit 9 is not available).
# The firmware only sends keys 1-4 in MODE_3, so with key 1 broken it could never send D.
# These clickers therefore answer in MODE_2: type the (physical) key, then ENTER. The bridge turns
# that entry into a normal answer message, so the backend sees nothing special.
KEY_SHIFT = str.maketrans({"2": "1", "3": "2", "4": "3", "5": "4", "6": "5",
                           "7": "6", "8": "7", "9": "8", "1": None})   # a ghost "1" is dropped


def parse_dev_list(text: str | None) -> set[int] | str:
    """"all" -> "all";  "7,12" -> {7, 12};  None/"" -> empty set."""
    if not text:
        return set()
    if text.strip().lower() == "all":
        return "all"
    return {int(x) for x in text.replace(" ", "").split(",") if x}


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
        self.server_seq: dict[int, int] = {}        # dev -> last_seq the backend holds (this session)
        self.fwd_max: dict[int, int] = {}           # dev -> highest seq the bridge has forwarded
        self.broken_key1 = parse_dev_list(getattr(args, "broken_key1", None))
        self.locked_q: dict[int, int] = {}          # --broken-key1 clickers: dev -> question already locked
        if self.broken_key1:
            log.info("key 1 broken on %s: keys shift 2->1 ... 9->8, answers are typed + ENTER",
                     "all clickers" if self.broken_key1 == "all" else "devices " + ",".join(map(str, sorted(self.broken_key1))))
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
        if phase == "QUESTION_OPEN" and dev in self.registered and self.key1_broken(dev):
            # answer by typing one key + ENTER (MODE_2); wait once this question is locked
            return {"cmd": "MODE_1"} if self.locked_q.get(dev) == self.state.get("q") else {"cmd": "MODE_2"}
        if phase == "QUESTION_OPEN" and dev in self.registered:
            cmd = {"cmd": "MODE_3", "q": self.state.get("q"), "n": self.state.get("n_choices", 4)}
            if self.state.get("deadline_ms") is not None:
                cmd["deadline_ms"] = self.state["deadline_ms"]
            return cmd
        return {"cmd": "MODE_1"}                    # IDLE, ENDED, CLOSED, or not registered

    def key1_broken(self, dev: int | None) -> bool:
        return dev is not None and (self.broken_key1 == "all" or dev in self.broken_key1)

    def _on_state(self, s: dict) -> None:
        old = self.state.get("phase")
        if s.get("phase") == "REGISTER" and old != "REGISTER":
            self.registered.clear()                 # a new session: everybody registers again
            self.server_seq.clear()                 # ... and seq numbering starts again at 1
            self.fwd_max.clear()
            self.locked_q.clear()
            for link in self.links.values():
                link.rebase_pending = True
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
        if ack.get("code") == "REMOVED":            # the teacher removed this device: ask for the MSSV again
            self.registered.discard(dev)
            link.send({"cmd": "MODE_2"})
            log.info("dev %02d removed by the teacher -> MODE_2", dev)
            return
        entry = link.sent.pop(ack.get("seq"), None)
        if entry is None:
            return                                  # an ack for a message we did not send
        kind, device_seq = entry
        code = ack.get("code")
        link.send({"cmd": "ACK", "kind": kind, **ack, "seq": device_seq})   # the clicker's own numbering
        phase = self.state.get("phase")
        if kind == "register":
            if code == "OK":
                self.registered.add(dev)
                link.send(self.command_for(dev))    # MODE_1 (wait) or MODE_3 if a question is open
            else:
                link.send({"cmd": "MODE_2"})        # let the student type the MSSV again
        else:  # answer
            if code in ("OK", "LOCKED"):
                if self.key1_broken(dev):           # MODE_2 does not lock the keys: switch to wait
                    self.locked_q[dev] = ack.get("q", self.state.get("q"))
                    link.send({"cmd": "MODE_1"})
                return                              # locked: keys stay locked on the clicker
            if self.key1_broken(dev) and phase == "QUESTION_OPEN" and code not in ("UNREGISTERED", "DUP"):
                link.send({"cmd": "MODE_2"})        # e.g. BAD (key 6 for a 4-option question): type again
                log.info("ack dev %02d answer seq %s -> %s (type again)", dev, ack.get("seq"), code)
                return
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
        learned = link.dev is None
        if link.dev != dev:                         # trust the message's dev, remember the link under it
            self.links.pop(link.dev, None)
            link.dev = dev
        self.links[dev] = link
        if learned:                                 # connected without a number in its name: now we know it
            log.info("clicker %s is device %02d", link.name, dev)
            link.send(self.command_for(dev))
        if self.key1_broken(dev) and "mssv" in p:
            typed = str(p["mssv"])
            shifted = typed.translate(KEY_SHIFT)
            if self.state.get("phase") == "QUESTION_OPEN" and dev in self.registered:
                # an answer typed in MODE_2: one key + ENTER -> a normal answer message
                choice = int(shifted) if shifted.isdigit() else 0          # 0 / 57 -> the backend says BAD
                p = {"dev": dev, "q": self.state.get("q"), "choice": choice, "seq": p.get("seq")}
                log.info("dev %02d (key 1 broken) typed %s -> answer %s", dev, typed, choice)
            else:
                p["mssv"] = shifted
                log.info("dev %02d (key 1 broken) typed MSSV %s -> %s", dev, typed, shifted)
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
            # The clicker restarts seq at 1 after a reconnect/reboot. The backend would refuse that as
            # DUP, so the bridge continues the numbering: forwarded = (base at connect) + clicker's seq.
            device_seq = p["seq"]
            if link.remap_seq and link.rebase_pending:
                # first message after a (re)connect or a new test: continue right after the last number
                # the backend has for this device in THIS test (1 in a new test)
                start = max(self.server_seq.get(dev, 0), self.fwd_max.get(dev, 0)) + 1
                link.seq_base = start - device_seq
                link.rebase_pending = False
                log.info("dev %02d: clicker seq %d -> forwarded as %d", dev, device_seq, start)
            fwd = link.seq_base + device_seq if link.remap_seq else device_seq
            p["seq"] = fwd
            link.sent[fwd] = (kind, device_seq)
            self.fwd_max[dev] = max(self.fwd_max.get(dev, 0), fwd)
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
            await self.refresh_session()            # registration list + each device's last seq
            link.rebase_pending = True              # numbering continues inside the current test
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
                    self.server_seq = {d["device_no"]: d.get("last_seq") or 0 for d in devs}
        except Exception as exc:
            log.warning("could not read the session from %s: %s", self.a.api, exc)


class Link:
    """One connected clicker. send() queues a command; a writer task writes them in order."""

    def __init__(self, name: str, dev: int | None, writer, max_write: int = 0):
        self.name, self.dev = name, dev
        self.sent: dict[int, tuple] = {}            # forwarded seq -> (kind, clicker's seq), to route the ack
        self.remap_seq = True                       # continue the numbering across reconnects
        self.seq_base = 0
        self.rebase_pending = True                  # set the base on the next message
        self.queue: asyncio.Queue = asyncio.Queue()
        self._writer = writer                       # async function(bytes)
        self.max_write = max_write                  # 0 = no limit

    def send(self, cmd: dict) -> None:
        self.queue.put_nowait(cmd)

    async def write_loop(self) -> None:
        while True:
            cmd = await self.queue.get()
            data = fit(cmd, self.max_write)
            if data is None:
                log.debug("-> %s skipped (longer than %d bytes): %s", self.name, self.max_write, cmd)
                continue
            try:
                await self._writer(data)
                log.debug("-> %s %s", self.name, data.decode())
            except Exception as exc:
                log.warning("write to %s failed: %s", self.name, exc)


def fit(cmd: dict, limit: int) -> bytes | None:
    """Encode a command so that it fits the clicker's receive buffer (BLE: 20 bytes by default).
    Too long: MODE_x is sent without its extra fields (q, n, deadline_ms; the current firmware does
    not use them), an ACK is shortened to {"cmd":"OK","c":2} style, and is skipped if even that is
    too long. limit 0 = send everything as is."""
    enc = lambda d: json.dumps(d, separators=(",", ":")).encode()
    data = enc(cmd)
    if not limit or len(data) <= limit:
        return data
    if str(cmd.get("cmd", "")).startswith("MODE_"):
        return enc({"cmd": cmd["cmd"]})
    if cmd.get("cmd") == "ACK":
        short = {"cmd": cmd.get("code", "?")}
        if cmd.get("choice") is not None:
            short["c"] = cmd["choice"]
        data = enc(short)
        return data if len(data) <= limit else None
    return None


class PhoneLink(Link):
    """A phone in phone mode: commands go through the shared notify characteristic, tagged with dev."""

    def __init__(self, dev: int, hub: "PhoneHub"):
        super().__init__(f"phone_{dev}", dev, None)
        self.hub = hub
        self.remap_seq = False                      # phones keep their seq across reloads

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
            # use_cached_services=False: Windows reads the clicker's services fresh on every connection.
            # With its cache, a clicker that restarted fails with "Could not get GATT characteristics ... Unreachable".
            async with BleakClient(device, winrt={"use_cached_services": False}) as client:
                async def write(data: bytes) -> None:
                    await client.write_gatt_char(RX_CHAR_UUID, data, response=True)

                link = Link(device.name or device.address, dev_from_name(device.name), write, bridge.a.max_write)
                writer = asyncio.create_task(link.write_loop())
                log.info("[+] connected %s (%s)", device.name, device.address)
                await bridge.link_up(link)          # seq base + current mode, before any message arrives
                await client.start_notify(TX_CHAR_UUID, lambda _s, data: bridge.from_device(link, bytes(data)))
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
        found = await BleakScanner.discover(timeout=2.0, return_adv=True)
        for d, adv in found.values():
            name = d.name or adv.local_name or ""
            # only our clickers: the name must be PicoRemote_<number>
            if name.startswith(NAME_PREFIX) and d.address not in busy:
                log.info("found %s (%s), connecting", name, d.address)
                asyncio.create_task(handle(d))
        await asyncio.sleep(0.5)


# ===================================================================== phone mode (bless)
class WinGattServer:
    """Windows: the laptop as a Bluetooth device, written directly on the Windows API (pywinrt 3.x,
    the same packages bleak installs). Replaces bless, whose Windows part does not install on
    Python 3.13+ (it needs winrt 2.0 / bleak_winrt, which stop at 3.12)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_write):
        self.loop, self.on_write = loop, on_write
        self.provider = None
        self.rx_char = None
        self._tokens = []                            # keep event registrations alive

    async def start(self) -> None:
        """Start advertising. Windows sometimes refuses (status ABORTED) depending on the adapter and
        driver, so several known-good variants are tried in order until one is STARTED."""
        from winrt.windows.devices.bluetooth import BluetoothAdapter

        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            raise RuntimeError("no Bluetooth adapter found (is Bluetooth turned on?)")
        if not adapter.is_peripheral_role_supported:
            raise RuntimeError("this Bluetooth adapter cannot act as a device (no 'peripheral role'). "
                               "Use another laptop or a USB Bluetooth adapter that supports it.")

        attempts = [
            ("service 0x1234, discoverable", SERVICE_UUID, "discoverable"),
            ("custom 128-bit service, discoverable", ALT_SERVICE_UUID, "discoverable"),
            ("service 0x1234, default advertising", SERVICE_UUID, "default"),
            ("service 0x1234, connectable only", SERVICE_UUID, "connectable"),
            ("custom 128-bit service, default advertising", ALT_SERVICE_UUID, "default"),
        ]
        discoverable_refused = False
        for label, service_uuid, mode in attempts:
            status = await self._try(service_uuid, mode)
            log.info("advertising attempt '%s' -> %s", label, status)
            if status.startswith("STARTED"):
                self.service_uuid = service_uuid
                return
            if status == "CREATED":
                # not refused, just never confirmed: on some drivers it advertises anyway
                self.service_uuid = service_uuid
                log.warning("Windows did not confirm advertising (status CREATED) but did not refuse it either. "
                            "Keeping it running: check with nRF Connect on the phone (scan for service %s).",
                            service_uuid)
                if discoverable_refused:
                    log.warning("Tip: 'discoverable' was refused. Turn on 'Allow Bluetooth devices to find this PC' "
                                "(Settings > Bluetooth & devices > Devices > More Bluetooth settings > Options), "
                                "then restart the bridge.")
                return
            if status == "ABORTED" and mode == "discoverable":
                discoverable_refused = True
            self._stop()
            await asyncio.sleep(0.5)
        raise RuntimeError("Windows refused to advertise in every variant (ABORTED). Turn on 'Allow Bluetooth "
                           "devices to find this PC' (Settings > Bluetooth & devices > Devices > More Bluetooth "
                           "settings > Options). Close every other "
                           "bridge window (.\\stop.ps1 -KeepDocker), turn Bluetooth off and on, then run ONE bridge. "
                           "If it still fails, this adapter/driver cannot host a Bluetooth service: "
                           "update the Bluetooth driver or use a USB Bluetooth adapter.")

    async def _try(self, service_uuid: str, mode: str) -> str:
        import uuid
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCharacteristicProperties as Props, GattLocalCharacteristicParameters,
            GattProtectionLevel, GattServiceProvider, GattServiceProviderAdvertisementStatus as Adv,
            GattServiceProviderAdvertisingParameters)

        res = await GattServiceProvider.create_async(uuid.UUID(service_uuid))
        if res.service_provider is None:
            return f"CREATE_FAILED (error {res.error})"
        self.provider = res.service_provider
        self._tokens = []

        async def add_char(char_uuid: str, props):
            params = GattLocalCharacteristicParameters()
            params.characteristic_properties = props
            params.read_protection_level = GattProtectionLevel.PLAIN
            params.write_protection_level = GattProtectionLevel.PLAIN
            r = await self.provider.service.create_characteristic_async(uuid.UUID(char_uuid), params)
            return r.characteristic

        tx = await add_char(TX_CHAR_UUID, Props.WRITE | Props.WRITE_WITHOUT_RESPONSE)   # phone -> laptop
        self.rx_char = await add_char(RX_CHAR_UUID, Props.NOTIFY | Props.READ)           # laptop -> phone
        if tx is None or self.rx_char is None:
            return "CHARACTERISTIC_FAILED"
        self._tokens.append(tx.add_write_requested(self._write_requested))
        self._tokens.append(self.rx_char.add_read_requested(self._read_requested))
        self._tokens.append(self.provider.add_advertisement_status_changed(
            lambda sender, args: log.debug("advertising status: %s (error %s)", Adv(args.status).name, args.error)))

        if mode == "default":
            self.provider.start_advertising()
        else:
            adv = GattServiceProviderAdvertisingParameters()
            adv.is_discoverable = mode == "discoverable"
            adv.is_connectable = True
            self.provider.start_advertising_with_parameters(adv)
        for _ in range(100):                         # wait up to 10 s
            st = self.provider.advertisement_status
            if st in (Adv.STARTED, Adv.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA, Adv.ABORTED):
                break
            await asyncio.sleep(0.1)
        return Adv(self.provider.advertisement_status).name

    def _stop(self) -> None:
        try:
            if self.provider is not None:
                self.provider.stop_advertising()
        except Exception:
            pass
        self.provider = None

    # WinRT calls these on its own threads: hand the work to the asyncio loop
    def _write_requested(self, sender, args) -> None:
        deferral = args.get_deferral()
        asyncio.run_coroutine_threadsafe(self._handle_write(args, deferral), self.loop)

    async def _handle_write(self, args, deferral) -> None:
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattWriteOption
        from winrt.windows.storage.streams import DataReader
        try:
            request = await args.get_request_async()
            if request is None:
                return
            reader = DataReader.from_buffer(request.value)
            data = bytearray(reader.unconsumed_buffer_length)
            reader.read_bytes(data)
            if request.option == GattWriteOption.WRITE_WITH_RESPONSE:
                request.respond()
            self.on_write(bytes(data))
        except Exception as exc:
            log.warning("write from phone failed: %s", exc)
        finally:
            deferral.complete()

    def _read_requested(self, sender, args) -> None:
        deferral = args.get_deferral()

        async def answer():
            from winrt.windows.storage.streams import DataWriter
            try:
                request = await args.get_request_async()
                if request is not None:
                    w = DataWriter()
                    w.write_bytes(b"{}")
                    request.respond_with_value(w.detach_buffer())
            finally:
                deferral.complete()
        asyncio.run_coroutine_threadsafe(answer(), self.loop)

    def notify(self, data: bytes) -> None:
        from winrt.windows.storage.streams import DataWriter
        w = DataWriter()
        w.write_bytes(data)
        buf = w.detach_buffer()

        async def send():
            try:
                await self.rx_char.notify_value_async(buf)
            except Exception as exc:
                log.warning("notify failed: %s", exc)
        self.loop.create_task(send())


async def run_phone_gateway(bridge: Bridge) -> None:
    if sys.platform == "win32":
        server = WinGattServer(asyncio.get_running_loop(),
                               lambda data: bridge.from_phone(data, hub))
        hub = PhoneHub(server.notify)
        await server.start()
        log.info("phone mode: advertising service %s. Open phone_ble.html on the phone "
                 "(it may show the laptop's own Bluetooth name).", server.service_uuid)
        await hub.run()
        return
    await run_phone_gateway_bless(bridge)


async def run_phone_gateway_bless(bridge: Bridge) -> None:
    """Linux / macOS: bless (as in the hardware team's mock_pico.py)."""
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


async def check_adapter() -> None:
    """--check: what can this laptop's Bluetooth do?"""
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    try:
        import bleak
        print(f"bleak        : installed ({getattr(bleak, '__version__', '?')})")
    except ImportError:
        print("bleak        : MISSING  -> python -m pip install -r requirements-bridge.txt")
    if sys.platform != "win32":
        print("(adapter details are only checked on Windows)")
        return
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
    except ImportError:
        print("winrt        : MISSING  -> python -m pip install -r requirements-bridge.txt")
        return
    a = await BluetoothAdapter.get_default_async()
    if a is None:
        print("adapter      : NONE FOUND (Bluetooth off, or no adapter)")
        return
    print(f"adapter      : found")
    print(f"Bluetooth LE : {'yes' if a.is_low_energy_supported else 'NO'}")
    print(f"central role : {'yes' if a.is_central_role_supported else 'NO'}   (needed for real Pico clickers)")
    print(f"peripheral   : {'yes' if a.is_peripheral_role_supported else 'NO'}   (needed for --phone)")


async def main(args) -> None:
    if args.check:
        await check_adapter()
        return
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
    p.add_argument("--max-write", type=int, default=20,
                   help="longest command the clicker can receive, bytes (default 20 = BLE default; "
                        "0 = no limit, once the firmware enlarges its buffer and MTU)")
    p.add_argument("--no-fill-q", dest="fill_q", action="store_false",
                   help="send the clicker's own q (only when the firmware tracks the question number)")
    p.add_argument("--broken-key1", metavar="DEVS",
                   help='clickers whose key 1 is broken, e.g. "7,12" or "all": keys shift 2->1 ... 9->8, '
                        'answers are typed as one key + ENTER')
    p.add_argument("--phone", action="store_true",
                   help="phone test mode: the laptop advertises, phones connect with phone_ble.html")
    p.add_argument("--name", default="ARS-Gateway", help="advertised name in phone mode (Linux only)")
    p.add_argument("--check", action="store_true", help="only report what this laptop's Bluetooth supports")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("bleak").setLevel(logging.ERROR)     # hides "unhandled services changed event"
    for noisy in ("httpx", "httpcore"):                    # -v without the HTTP connection chatter
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        pass
