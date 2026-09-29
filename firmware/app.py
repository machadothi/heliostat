"""Application wiring. The only place asyncio tasks are created.

main.py calls run(). Everything that can fail belongs here rather than in
boot.py, so the safe-boot guard in main.py can catch it and drop to the REPL.

The network (milestone M6) and BLE provisioning (M7) plug in here as further
tasks alongside the control loop.
"""

import asyncio
import sys

from config import ConfigStore
from control.clock import Clock
from control.tracker import Tracker
from hal import board
from util import log
from util.monotime import monotonic

# Consecutive failed control cycles tolerated before latching a fault. One bad
# cycle can be a transient; a loop that keeps throwing is not controlling
# anything, and the mirror must not be left to drift unattended.
MAX_CYCLE_FAILURES = 3


def build(config_store):
    """Construct the tracker and everything it depends on."""
    cfg = config_store.data
    clock = Clock(monotonic)
    az, el = board.make_axes(cfg, monotonic)
    sensors = board.make_sensors(cfg, monotonic, (az, el))
    return Tracker(cfg, az, el, clock, monotonic, sensors)


async def control_loop(tracker, period_s):
    failures = 0
    while True:
        started = monotonic()
        try:
            await tracker.step()
            failures = 0
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            failures += 1
            log.error(f"control cycle failed ({failures}/{MAX_CYCLE_FAILURES}): {exc}")
            _print_exception(exc)
            if failures >= MAX_CYCLE_FAILURES:
                tracker.software_fault(f"control loop failing: {exc}")
        elapsed = monotonic() - started
        await asyncio.sleep(max(0.0, period_s - elapsed))


async def main(tracker, cfg):
    tasks = [asyncio.create_task(control_loop(tracker, cfg["tracker"]["period_s"]))]
    # M6: tasks.append(asyncio.create_task(net.httpd.serve(tracker, ...)))
    # M7: tasks.append(asyncio.create_task(ble.provisioning.run(...)))
    await asyncio.gather(*tasks)


def run():
    store = ConfigStore.load()
    tracker = build(store)
    log.configure(store["log"]["level"], clock=tracker.clock.now)
    log.info(
        "heliostat starting ({} mode)".format("SIMULATED" if store["sim"] else "hardware")
    )
    asyncio.run(main(tracker, store.data))


def _print_exception(exc):
    printer = getattr(sys, "print_exception", None)  # MicroPython
    if printer is not None:
        printer(exc)
    else:
        import traceback

        traceback.print_exception(exc)
