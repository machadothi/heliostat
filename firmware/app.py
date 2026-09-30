"""Application wiring. The only place asyncio tasks are created.

main.py calls run(). Everything that can fail belongs here rather than in
boot.py, so the safe-boot guard in main.py can catch it and drop to the REPL.

Tasks:
    control_loop   the tracker, once a second. Runs whatever the network does.
    wifi.run       keep the stored home network joined (station mode only)
    timesync.run   NTP whenever the network is up
    provisioner    BLE provisioning, when BLE is on
    ble_manager    decides when BLE is on (see below)
    http           the JSON API on port 80
    heartbeat      the GPIO2 LED: lub-dub while the control loop is cycling

BLE is ON when: no network is stored yet; the stored network has been
unreachable for 5 minutes; or BOOT was held for 3 s (a 5-minute window). It
turns OFF shortly after a successful provisioning, once the phone has had time
to read the IP. BLE and WiFi therefore only run together during provisioning.
"""

import asyncio
import sys

from config import ConfigStore
from control.clock import Clock
from control.tracker import Tracker
from hal import board
from util import log
from util.mem import idf_heap
from util.monotime import monotonic

MAX_CYCLE_FAILURES = 3
BUTTON_HOLD_S = 3.0
BLE_WINDOW_S = 300.0
BLE_OFF_AFTER_JOIN_S = 15.0
MEM_LOG_EVERY = 60  # cycles

# Run, in order, whenever the app stops -- including on Ctrl-C. See _shutdown().
_on_exit = []


def build(config_store):
    """Construct the tracker and everything it depends on."""
    cfg = config_store.data
    clock = Clock(monotonic)
    az, el = board.make_axes(cfg, monotonic)
    sensors = board.make_sensors(cfg, monotonic, (az, el))
    return Tracker(cfg, az, el, clock, monotonic, sensors)


async def control_loop(tracker, period_s, heartbeat=None, server=None):
    import gc

    failures = 0
    cycles = 0
    while True:
        started = monotonic()
        try:
            await tracker.step()
            failures = 0
            if heartbeat is not None:
                heartbeat.beat()  # the LED beats only while cycles complete
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            failures += 1
            # The handler itself must not be able to fail: a MemoryError while
            # formatting this very message once escaped and killed the app.
            # Free what we can first, and never let logging raise.
            gc.collect()
            try:
                log.error(f"control cycle failed ({failures}/{MAX_CYCLE_FAILURES}): {exc}")
                _print_exception(exc)
            except Exception:  # noqa: BLE001
                pass
            if failures >= MAX_CYCLE_FAILURES:
                try:
                    tracker.software_fault("control loop failing")
                except Exception:  # noqa: BLE001
                    pass
        # Collect every cycle: short, frequent collections keep the heap from
        # fragmenting when WiFi and Bluetooth both hold large buffers.
        gc.collect()
        cycles += 1
        if cycles % MEM_LOG_EVERY == 1:
            try:
                idf = idf_heap()
                log.info("mem: {} bytes free; idf {}; http {}".format(
                    gc.mem_free(),
                    "{} free, {} largest".format(*idf) if idf else "n/a",
                    server.requests if server else "n/a"))
            except Exception:  # noqa: BLE001
                pass
        elapsed = monotonic() - started
        await asyncio.sleep(max(0.0, period_s - elapsed))


class BleManager:
    """Turns BLE on and off. The only thing that calls provisioner.start/stop."""

    def __init__(self, provisioner, wifi):
        self.provisioner = provisioner
        self.wifi = wifi
        self.window_until = 0.0

    def open_window(self):
        self.window_until = monotonic() + BLE_WINDOW_S
        log.info(f"ble: provisioning window open for {BLE_WINDOW_S:.0f} s")

    def wanted(self):
        now = monotonic()
        if now < self.window_until:
            return True
        joined_at = self.provisioner.provisioned_at
        if joined_at is not None and now - joined_at < BLE_OFF_AFTER_JOIN_S:
            return True  # let the phone read the IP before we vanish
        return self.wifi.wants_ble and not self.wifi.connected()

    async def run(self):
        from machine import Pin

        button = Pin(board.PROVISION_BUTTON, Pin.IN, Pin.PULL_UP)
        held = 0.0
        while True:
            held = held + 0.25 if not button.value() else 0.0
            if held >= BUTTON_HOLD_S:
                self.open_window()
                held = -60.0  # ignore the same press for a minute
            want = self.wanted()
            if want and not self.provisioner.active:
                self.provisioner.start()
            elif not want and self.provisioner.active and not self.provisioner.connections:
                self.provisioner.stop()
            await asyncio.sleep(0.25)


async def main(tracker, store):
    from ble.provisioning import Provisioner
    from hal.heartbeat import Heartbeat
    from net.api import Api
    from net.httpd import HttpServer
    from net.timesync import TimeSync
    from net.wifi import Wifi

    wifi = Wifi(store, monotonic)
    timesync = TimeSync(tracker.clock, wifi)
    provisioner = Provisioner(store, wifi, timesync, tracker)
    ble_manager = BleManager(provisioner, wifi)
    heartbeat = Heartbeat(board.STATUS_LED, monotonic)
    _on_exit.append(provisioner.stop)
    _on_exit.append(lambda: wifi.sta.active(False))
    api = Api(tracker, store, wifi, uptime=monotonic, timesync=timesync)
    server = HttpServer(api, port=80)
    await server.start()

    await asyncio.gather(
        control_loop(tracker, store["tracker"]["period_s"], heartbeat, server),
        heartbeat.run(lambda: tracker.latch is not None or tracker.mode == "estop",
                      lambda: provisioner.active),
        wifi.run(),
        timesync.run(),
        provisioner.run(),
        ble_manager.run(),
    )


def run():
    store = ConfigStore.load()
    tracker = build(store)
    log.configure(store["log"]["level"], clock=tracker.clock.now)
    log.info("{} starting ({})".format(
        store["ble"]["name"], "SIMULATED" if store["sim"] else "hardware"))
    try:
        asyncio.run(main(tracker, store))
    finally:
        _shutdown()


def _shutdown():
    """Switch the radios off before handing back to the REPL.

    mpremote interrupts a running program with Ctrl-C and then SOFT-resets the
    interpreter. On the ESP32 a soft reset with Bluetooth still active can hang
    the chip until the watchdog fires ("PRO CPU has been reset by WDT"), and it
    then refuses mpremote until someone presses the reset button. Turning BLE
    off on the way out removes the cause.

    Servo torque is deliberately left alone: an interrupted program must not
    let the mirror flop.
    """
    while _on_exit:
        try:
            _on_exit.pop(0)()
        except Exception:  # noqa: BLE001 - best effort, keep going
            pass
    try:
        import bluetooth

        bluetooth.BLE().active(False)  # belt and braces, whoever turned it on
    except Exception:  # noqa: BLE001
        pass
    log.info("radios off; back to the REPL")


def _print_exception(exc):
    printer = getattr(sys, "print_exception", None)  # MicroPython
    if printer is not None:
        printer(exc)
    else:
        import traceback

        traceback.print_exception(exc)
