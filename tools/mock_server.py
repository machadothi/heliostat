#!/usr/bin/env python3
"""Serve the real HTTP API from the laptop, against a simulated heliostat.

    python3 tools/mock_server.py                    # http://0.0.0.0:8080
    python3 tools/mock_server.py --port 8080 --rate 60

These are the firmware's own net/httpd.py and net/api.py, running on CPython and
driving the simulated servos -- not a hand-written imitation. Point the Android
app (or curl) at it and develop end to end with no ESP32 on the desk.

The simulated clock starts at the current time; --rate speeds it up so a day
of tracking can be watched from the phone in minutes. The config lives in
tools/mock_config.json (gitignored), so settings survive restarts.
"""

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "firmware"))

from config import ConfigStore  # noqa: E402
from control.clock import Clock  # noqa: E402
from control.tracker import Tracker  # noqa: E402
from hal import board  # noqa: E402
from net.api import Api  # noqa: E402
from net.httpd import HttpServer  # noqa: E402
from util import log  # noqa: E402

CONFIG = REPO / "tools" / "mock_config.json"
GREENWICH = {"lat": 51.4779, "lon": -0.0015}


def build(rate):
    store = ConfigStore.load(str(CONFIG))
    store.data["sim"] = True
    if store.data["site"]["lat"] == 0.0 and store.data["site"]["lon"] == 0.0:
        store.data["site"].update(GREENWICH)
    started = time.monotonic()
    clock = Clock(time.monotonic, epoch=time.time(), rate=rate)
    az, el = board.make_axes(store.data, time.monotonic)
    sensors = board.make_sensors(store.data, time.monotonic, (az, el))
    tracker = Tracker(store.data, az, el, clock, time.monotonic, sensors)
    api = Api(tracker, store, uptime=lambda: time.monotonic() - started)
    return tracker, api


async def control_loop(tracker):
    while True:
        await tracker.step()
        await asyncio.sleep(1.0)


async def main(args):
    tracker, api = build(args.rate)
    server = HttpServer(api, host=args.host, port=args.port)
    await server.start()
    print(f"mock heliostat on http://{args.host}:{args.port}  (sim clock x{args.rate})")
    print(f"  curl http://localhost:{args.port}/api/status")
    await control_loop(tracker)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--rate", type=float, default=1.0, help="simulated seconds per second")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    log.configure("info" if args.verbose else "warning")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args))
