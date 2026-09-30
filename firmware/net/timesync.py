"""Clock sources: NTP over the home network, and the phone.

NTP is what lets the machine recover alone. After a power cut the ESP32 boots
with no idea of the time -- and without time there is no sun vector, so the
supervisor refuses to track. With the network up, the clock is set within
seconds of boot and tracking resumes with nobody around.

The phone can still set the time (POST /api/time, or BLE during provisioning).
Whichever source spoke last wins; both are far better than the ESP32's own
oscillator, which drifts by seconds a day.

EPOCH: `ntptime.time()` counts from the PORT's epoch, which on the ESP32 is
2000-01-01, not 1970. It is converted here, once, using the constant in
solar/julian.py. tools/check_board.py confirmed this board's epoch.
"""

import asyncio
import time

from solar.julian import UNIX_TO_2000_OFFSET
from util import log

NTP_HOST = "pool.ntp.org"
RESYNC_S = 6 * 3600
RETRY_S = 60
PLAUSIBLE_AFTER = 1.6e9  # September 2020; anything earlier is a broken clock

_PORT_EPOCH_YEAR = time.gmtime(0)[0]


def to_unix(port_seconds):
    """Seconds on this port's epoch -> Unix seconds."""
    return port_seconds + UNIX_TO_2000_OFFSET if _PORT_EPOCH_YEAR == 2000 else port_seconds


def ntp_unix(host=NTP_HOST):
    """One NTP query. Returns Unix seconds, or raises. Blocks for up to ~1 s."""
    import ntptime

    ntptime.host = host
    unix = to_unix(ntptime.time())
    if unix < PLAUSIBLE_AFTER:
        raise ValueError(f"NTP returned an implausible time: {unix}")
    return unix


class TimeSync:
    def __init__(self, clock, wifi):
        self.clock = clock
        self.wifi = wifi
        self.last_source = None
        self.last_sync_unix = None
        self._wanted = True
        wifi.on_up(self.request)

    def request(self):
        """Ask for a sync at the next opportunity (called when WiFi comes up)."""
        self._wanted = True

    def from_phone(self, unix):
        self.clock.set_epoch(unix)
        self.last_source, self.last_sync_unix = "phone", unix

    async def run(self):
        next_due = 0.0
        while True:
            now = time.time()
            if self.wifi.connected() and (self._wanted or now >= next_due):
                try:
                    unix = ntp_unix()
                    self.clock.set_epoch(unix)
                    self.last_source, self.last_sync_unix = "ntp", unix
                    self._wanted = False
                    next_due = now + RESYNC_S
                    log.info("time: NTP sync ok")
                except Exception as exc:  # noqa: BLE001 - the network is not guaranteed
                    log.warning(f"time: NTP failed ({exc}), retry in {RETRY_S} s")
                    next_due = now + RETRY_S
            await asyncio.sleep(5)

    def status(self):
        return {"source": self.last_source, "last_sync": self.last_sync_unix,
                "age_s": self.clock.age()}
