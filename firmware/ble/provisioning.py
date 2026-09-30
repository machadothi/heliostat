"""BLE provisioning: hand the ESP32 the credentials of the phone's own network.

The flow (docs/protocol.md has the byte-level detail):

    1. Advertise as `ble.name` -- "my_heliostat" -- with the service UUID in the
       scan response, since name + flags + a 128-bit UUID overflow the 31-byte
       advertisement.
    2. The phone connects, optionally asks for a SCAN of visible networks (the
       ESP32 is 2.4 GHz only, so a phone on 5 GHz needs to pick the 2.4 GHz
       sibling), writes SSID and PSK, then COMMAND=join.
    3. The ESP32 tries to join WITH BLE STILL UP and notifies the outcome on
       WIFI_STATE: joined + IP, or a reason (wrong password, not found, timeout).
    4. On success the credentials are saved, and app.py turns BLE off shortly
       after, once the phone has had time to read the IP.

The IRQ handler does nothing but record what arrived. The slow work -- a WiFi
join takes up to 20 s -- runs in `run()`, an asyncio task, so the Bluetooth
stack is never blocked.

SECURITY: while advertising, anyone in radio range could provision the board.
BLE is only on while unprovisioned, during the 5-minute BOOT-button window, or
after the stored network has been unreachable for 5 minutes -- short exposure,
acceptable at home. Not acceptable for a product.
"""

import asyncio
import json
import struct

import bluetooth
from util import log
from version import FW_VERSION

from ble import gatt_service as g

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21

_ADV_INTERVAL_US = 250_000


def _field(kind, value):
    return struct.pack("BB", len(value) + 1, kind) + value


def advertising_payloads(name, service_uuid):
    """(advertisement, scan response). Name in one, 128-bit UUID in the other."""
    adv = _field(0x01, b"\x06") + _field(0x09, name.encode())
    resp = _field(0x07, bytes(service_uuid))
    if len(adv) > 31:
        raise ValueError(f"BLE name too long for the advertisement: {name}")
    return adv, resp


def _hex(value):
    return " ".join(f"{b:02X}" for b in bytes(value)[:24])


def _mac(addr):
    return ":".join(f"{b:02X}" for b in bytes(addr))


def _ip_bytes(ip):
    try:
        return bytes(int(part) for part in ip.split("."))
    except Exception:  # noqa: BLE001
        return b"\x00\x00\x00\x00"


class Provisioner:
    def __init__(self, store, wifi, timesync=None, tracker=None):
        self.store = store
        self.wifi = wifi
        self.timesync = timesync
        self.tracker = tracker
        self.name = store["ble"]["name"]
        self.active = False
        self.connections = set()
        self.provisioned_at = None  # monotonic time of the last successful join
        self._ble = None
        self._h = None  # characteristic value handles, by key
        self._chunks = {"ssid": bytearray(), "psk": bytearray()}
        self._values = {"ssid": None, "psk": None}
        self._pending = []  # commands queued by the IRQ, run by run()

    # -- lifecycle --------------------------------------------------------------

    def start(self):
        if self.active:
            return
        self._ble = bluetooth.BLE()
        self._ble.active(True)
        self._ble.config(gap_name=self.name)
        try:
            self._ble.config(mtu=247)
        except Exception:  # noqa: BLE001 - not every build lets us raise it
            pass
        self._ble.irq(self._irq)
        (registered,) = self._ble.gatts_register_services((g.SERVICE,))
        self._h, name_handles = g.map_handles(registered)
        self._by_handle = {handle: key for key, handle in self._h.items()}
        for key, size in g.BUFFER_SIZES.items():
            # append=False: reassembly of chunked SSID/PSK happens in _reassemble,
            # which needs each write on its own, sequence byte included.
            self._ble.gatts_set_buffer(self._h[key], size, False)
        for key, _, _, name in g.CHARACTERISTICS:
            self._ble.gatts_set_buffer(name_handles[key], g.NAME_BUFFER, False)
            self._ble.gatts_write(name_handles[key], name.encode())
        self._publish_info()
        self._set_state(g.STATE_IDLE, 0, None, notify=False)
        self._advertise()
        self.active = True
        log.info(f"ble: advertising as {self.name}")

    def stop(self):
        if not self.active:
            return
        try:
            self._ble.active(False)
        except Exception:  # noqa: BLE001
            pass
        self.active = False
        self.connections.clear()
        log.info("ble: off")

    # -- IRQ: record and return; never block here ------------------------------

    def _irq(self, event, data):
        # BLE IRQs are soft (scheduled) on the ESP32, so logging here is safe.
        # Any exception is caught and logged: an IRQ that dies silently looks,
        # from the phone, exactly like a board that ignores writes.
        try:
            if event == _IRQ_CENTRAL_CONNECT:
                conn, _addr_type, addr = data
                self.connections.add(conn)
                log.info(f"ble: phone connected (conn {conn}, {_mac(addr)})")
            elif event == _IRQ_CENTRAL_DISCONNECT:
                conn = data[0]
                self.connections.discard(conn)
                log.info(f"ble: phone disconnected (conn {conn})")
                if self.active:
                    self._advertise()
            elif event == _IRQ_MTU_EXCHANGED:
                log.info(f"ble: MTU {data[1]}")
            elif event == _IRQ_GATTS_WRITE:
                _conn, value_handle = data
                self._on_write(value_handle, self._ble.gatts_read(value_handle))
        except Exception as exc:  # noqa: BLE001
            log.error(f"ble: IRQ {event} failed: {exc}")

    def _on_write(self, value_handle, value):
        key = self._by_handle.get(value_handle)
        # Never log the password's content, only that it arrived.
        shown = "" if key in ("psk", None) else " " + _hex(value)
        log.info(f"ble: write {key or 'handle ' + str(value_handle)} ({len(value)} bytes){shown}")
        if key in ("ssid", "psk"):
            self._reassemble(key, value)
        elif key == "command" and value:
            self._pending.append(value[0])
        elif key == "time" and len(value) >= 6:
            unix, tz_minutes = struct.unpack("<Ih", value[:6])
            self._pending.append(("time", unix, tz_minutes))
        elif key == "location" and len(value) >= 8:
            lat, lon = struct.unpack("<ff", value[:8])
            elev = struct.unpack("<f", value[8:12])[0] if len(value) >= 12 else 0.0
            self._pending.append(("location", lat, lon, elev))

    def _reassemble(self, key, value):
        if not value:
            return
        if not g.is_framed(value):
            # Plain text, e.g. typed into nRF Connect: the whole value at once.
            self._values[key] = bytes(value).decode("utf-8")
            self._chunks[key] = bytearray()
            return
        seq = value[0]
        if (seq & 0x7F) == 0:
            self._chunks[key] = bytearray()  # a new value always starts at seq 0
        self._chunks[key].extend(value[1:])
        if seq & g.SEQ_FINAL:
            self._values[key] = bytes(self._chunks[key]).decode("utf-8")
            self._chunks[key] = bytearray()
            detail = repr(self._values[key]) if key == "ssid" else f"{len(self._values[key])} chars"
            log.info(f"ble: {key} complete: {detail}")

    # -- the slow work ------------------------------------------------------------

    async def run(self):
        while True:
            while self._pending:
                item = self._pending.pop(0)
                try:
                    await self._do(item)
                except Exception as exc:  # noqa: BLE001 - one bad request must not end BLE
                    log.error(f"ble: {item} failed: {exc}")
            await asyncio.sleep(0.1)

    async def _do(self, item):
        log.info(f"ble: handling {item if not isinstance(item, tuple) else item[0]}")
        if item == g.CMD_JOIN:
            await self._join()
        elif item == g.CMD_SCAN:
            self._set_state(g.STATE_SCANNING, 0, None)
            await asyncio.sleep(0)  # let the notification go out first
            networks = self.wifi.scan()
            import gc

            gc.collect()
            log.info(f"ble: wifi scan found {len(networks)}: " + ", ".join(n for n, _, _ in networks[:8])
                     + f"  (mem {gc.mem_free()} free, radio off)")
            text = "\n".join(f"{n}\t{c}\t{r}" for n, c, r in networks)
            limit = g.BUFFER_SIZES["scan"]
            self._ble.gatts_write(self._h["scan"], text.encode()[:limit])
            self._set_state(g.STATE_SCAN_READY, 0, None)
        elif item == g.CMD_FORGET:
            self.store.data["wifi"]["ssid"] = ""
            self.store.data["wifi"]["psk"] = ""
            self.store.save()
            self._publish_info()
            self._set_state(g.STATE_IDLE, 0, None)
        elif isinstance(item, tuple) and item[0] == "time":
            _, unix, tz_minutes = item
            if self.timesync is not None:
                self.timesync.from_phone(unix)
            elif self.tracker is not None:
                self.tracker.clock.set_epoch(unix)
            self.store.data["site"]["tz_offset_min"] = tz_minutes
            log.info("ble: time set from phone")
        elif isinstance(item, tuple) and item[0] == "location":
            _, lat, lon, elev = item
            self.store.update({"site": {"lat": lat, "lon": lon, "elev_m": elev}})
            self.store.save()
            if self.tracker is not None:
                self.tracker.cfg = self.store.data
            log.info(f"ble: location set to {lat:.4f}, {lon:.4f}")

    async def _join(self):
        from net import wifi as w

        ssid, psk = self._values["ssid"], self._values["psk"]
        if not ssid:
            self._set_state(g.STATE_FAILED, g.REASON_NOT_FOUND, None)
            return
        self._set_state(g.STATE_JOINING, 0, None)
        log.info(f"ble: joining {ssid}")
        reason, ip = await self.wifi.join(ssid, psk or "")
        import gc

        gc.collect()
        log.info(f"ble: join result {reason} {ip or ''}  (mem {gc.mem_free()} free)")
        if reason == w.JOINED:
            self.wifi.forget_and_store(ssid, psk or "")
            self._set_state(g.STATE_JOINED, 0, ip)
            self.provisioned_at = self.wifi.monotonic()
            self._values["psk"] = None  # do not keep the password around in RAM
        else:
            self._set_state(g.STATE_FAILED, w.REASON_CODES.get(reason, 4), None)
        self._publish_info()

    # -- characteristic values -------------------------------------------------------

    def _set_state(self, state, reason, ip, notify=True):
        value = struct.pack("BB", state, reason) + _ip_bytes(ip or "0.0.0.0")
        handle = self._h["wifi_state"]
        self._ble.gatts_write(handle, value)
        if notify:
            log.info(f"ble: notify wifi_state {_hex(value)} to {len(self.connections)} phone(s)")
            for conn in list(self.connections):
                try:
                    self._ble.gatts_notify(conn, handle)
                except Exception as exc:  # noqa: BLE001 - that central has gone
                    log.warning(f"ble: notify to conn {conn} failed: {exc}")
                    self.connections.discard(conn)

    def _publish_info(self):
        info = {"service": g.SERVICE_NAME, "fw": FW_VERSION, "name": self.name,
                "wifi": self.wifi.state, "ip": self.wifi.ip(), "ssid": self.store["wifi"]["ssid"]}
        self._ble.gatts_write(self._h["info"], json.dumps(info).encode())

    def _advertise(self):
        adv, resp = advertising_payloads(self.name, g.SERVICE_UUID)
        self._ble.gap_advertise(_ADV_INTERVAL_US, adv_data=adv, resp_data=resp)
