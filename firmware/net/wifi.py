"""WiFi in station mode only: the ESP32 joins the home network, never hosts one.

Credentials arrive over BLE provisioning (ble/provisioning.py) and are stored in
config.json. From then on every boot joins the stored network directly.

If the stored network cannot be joined for DOWN_BEFORE_BLE_S, `wants_ble` goes
True and app.py re-enables BLE advertising so the phone can hand over new
credentials -- a new router, a changed password -- while WiFi keeps retrying in
the background. There is no access-point fallback, by design.

The ESP32's radio is 2.4 GHz only. A 5 GHz network simply never appears in a
scan, so it surfaces here as "ssid_not_found"; the app checks the phone's band
and uses scan() to suggest the 2.4 GHz sibling before that can happen.
"""

import asyncio

import network
from util import log

JOIN_TIMEOUT_S = 20
RETRY_MIN_S = 10
RETRY_MAX_S = 300
DOWN_BEFORE_BLE_S = 300

# Join outcomes, reported to the phone. The BLE characteristic carries the code.
JOINED = "joined"
WRONG_PASSWORD = "wrong_password"
SSID_NOT_FOUND = "ssid_not_found"
TIMEOUT = "timeout"
FAILED = "failed"

REASON_CODES = {JOINED: 0, WRONG_PASSWORD: 1, SSID_NOT_FOUND: 2, TIMEOUT: 3, FAILED: 4}


def _stat(name):
    return getattr(network, name, None)


def _status_name(code):
    for name in dir(network):
        if name.startswith("STAT_") and getattr(network, name) == code:
            return name
    return "?"


class Wifi:
    def __init__(self, store, monotonic):
        self.store = store
        self.monotonic = monotonic
        self.sta = network.WLAN(network.STA_IF)
        self.state = "idle"  # idle | joining | up | down
        self.last_error = None
        self._down_since = monotonic()
        self._busy = False  # a join is in progress
        self._reboot = False
        self._on_up = []

    # -- queries ---------------------------------------------------------------

    def configured(self):
        return bool(self.store["wifi"]["ssid"])

    def connected(self):
        return self.sta.active() and self.sta.isconnected()

    def ip(self):
        return self.sta.ifconfig()[0] if self.connected() else None

    def rssi(self):
        try:
            return self.sta.status("rssi") if self.connected() else None
        except Exception:  # noqa: BLE001 - not every build supports it
            return None

    @property
    def wants_ble(self):
        """True when the phone should be able to reach us over BLE."""
        if not self.configured():
            return True
        return not self.connected() and self.monotonic() - self._down_since > DOWN_BEFORE_BLE_S

    def status(self):
        return {"state": self.state, "ssid": self.store["wifi"]["ssid"], "ip": self.ip(),
                "rssi": self.rssi(), "error": self.last_error,
                "hostname": self.store["wifi"]["hostname"]}

    def on_up(self, callback):
        """Register a callable run each time the link comes up (e.g. NTP sync)."""
        self._on_up.append(callback)

    # -- actions ---------------------------------------------------------------

    def scan(self):
        """Visible networks as [(ssid, channel, rssi)], strongest first, de-duplicated."""
        self.sta.active(True)
        try:
            seen = {}
            for ssid, _bssid, channel, rssi, _auth, _hidden in self.sta.scan():
                name = ssid.decode("utf-8", "ignore")
                if name and (name not in seen or rssi > seen[name][1]):
                    seen[name] = (channel, rssi)
            return sorted(((n, c, r) for n, (c, r) in seen.items()), key=lambda x: -x[2])
        finally:
            self.radio_off_if_idle()

    def radio_off_if_idle(self):
        """Switch the WiFi radio off unless it is carrying a connection.

        An active radio holds tens of KB of heap. With Bluetooth also on during
        provisioning, leaving it on after a scan or a failed join exhausted the
        board's RAM and crashed the app a minute later (found on the bench,
        MemoryError in the control loop). join() switches it back on.
        """
        if not self.connected():
            self.sta.active(False)

    async def join(self, ssid, psk, timeout_s=JOIN_TIMEOUT_S):
        """Try one network. Returns (reason, ip). Does NOT store anything."""
        self._busy = True
        self.state = "joining"
        try:
            self.sta.active(True)
            try:
                network.hostname(self.store["wifi"]["hostname"])
            except Exception:  # noqa: BLE001 - older builds lack network.hostname
                pass
            if self.sta.isconnected():
                self.sta.disconnect()
            log.info(f"wifi: connecting to {ssid!r} (password {len(psk)} chars)")
            self.sta.connect(ssid, psk)

            waited = 0.0
            last = None
            while waited < timeout_s:
                if self.sta.isconnected():
                    self._up()
                    return JOINED, self.ip()
                status = self.sta.status()
                if status != last:
                    log.info(f"wifi: {ssid!r} status {status} ({_status_name(status)}) at {waited:.1f} s")
                    last = status
                if status == _stat("STAT_WRONG_PASSWORD"):
                    return self._fail(WRONG_PASSWORD)
                # On NO_AP_FOUND the ESP32 keeps retrying by itself; give it a few
                # seconds before concluding the network really is not there.
                if status == _stat("STAT_NO_AP_FOUND") and waited > 6:
                    return self._fail(SSID_NOT_FOUND)
                await asyncio.sleep(0.25)
                waited += 0.25
            return self._fail(TIMEOUT)
        finally:
            self._busy = False

    def forget_and_store(self, ssid, psk):
        """Persist credentials that have just been proven to work."""
        self.store.data["wifi"]["ssid"] = ssid
        self.store.data["wifi"]["psk"] = psk
        self.store.save()

    def schedule_reboot(self):
        self._reboot = True

    async def run(self):
        """Keep the stored network joined, forever, with exponential backoff."""
        delay = RETRY_MIN_S
        while True:
            if self._reboot:
                await asyncio.sleep(1)
                import machine

                machine.reset()
            if self.configured() and not self.connected() and not self._busy:
                if self.state == "up":
                    self._down("link lost")
                wifi = self.store["wifi"]
                reason, ip = await self.join(wifi["ssid"], wifi["psk"])
                if reason == JOINED:
                    log.info("wifi: joined {} as {}".format(wifi["ssid"], ip))
                    delay = RETRY_MIN_S
                else:
                    log.warning("wifi: {} ({}), retry in {} s".format(wifi["ssid"], reason, delay))
                    await asyncio.sleep(delay)
                    delay = min(RETRY_MAX_S, delay * 2)
                    continue
            await asyncio.sleep(2)

    # -- internals --------------------------------------------------------------

    def _up(self):
        self.state = "up"
        self.last_error = None
        for callback in self._on_up:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001
                log.warning(f"wifi on_up callback failed: {exc}")

    def _down(self, why):
        self.state = "down"
        self._down_since = self.monotonic()
        log.warning(f"wifi: down ({why})")

    def _fail(self, reason):
        self.last_error = reason
        self.sta.disconnect()
        self.radio_off_if_idle()
        if self.state != "down":
            self._down(reason)
        return reason, None
