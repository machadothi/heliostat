"""The status LED on GPIO2: a heartbeat that means the control loop is alive.

It beats only while the control loop is actually completing cycles. A blink
task that ran on its own would keep blinking after the tracker had crashed or
hung -- a comforting light over a dead machine. So the pattern is chosen from
the time of the last completed control cycle:

    lub-dub, once a second   healthy: the tracker is cycling
    three quick pulses       BLE provisioning is on (advertising as my_heliostat)
    fast even blink (5 Hz)   latched fault or E-stop: needs a person
    dark                     the control loop has stopped cycling
"""

import asyncio

HEALTHY = "healthy"
PROVISIONING = "provisioning"
FAULT = "fault"
STALLED = "stalled"

# (on_ms, off_ms) steps, repeated.
PATTERNS = {
    HEALTHY: ((80, 120), (80, 720)),  # lub-dub ... pause
    PROVISIONING: ((60, 100), (60, 100), (60, 620)),
    FAULT: ((100, 100),),
    STALLED: ((0, 500),),
}

STALL_AFTER_S = 5.0


class Heartbeat:
    def __init__(self, pin_number, monotonic):
        from machine import Pin

        self._led = Pin(pin_number, Pin.OUT)
        self._led.value(0)
        self._monotonic = monotonic
        self.last_cycle = monotonic()

    def beat(self):
        """Called by the control loop at the end of every completed cycle."""
        self.last_cycle = self._monotonic()

    def pattern(self, latched, provisioning):
        if self._monotonic() - self.last_cycle > STALL_AFTER_S:
            return STALLED
        if latched:
            return FAULT
        return PROVISIONING if provisioning else HEALTHY

    async def run(self, is_latched, is_provisioning):
        while True:
            for on_ms, off_ms in PATTERNS[self.pattern(is_latched(), is_provisioning())]:
                if on_ms:
                    self._led.value(1)
                    await asyncio.sleep_ms(on_ms)
                self._led.value(0)
                await asyncio.sleep_ms(off_ms)
