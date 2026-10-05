#!/usr/bin/env python3
"""Run the real tracker on the real servos, from the laptop.

    python3 tools/hw_track.py                      # noon sun, 60x, 45 s
    python3 tools/hw_track.py --hour 9 --rate 120 --seconds 60
    python3 tools/hw_track.py --now                # the actual current sun

Everything that runs is the firmware as deployed -- tracker, supervisor, servo
driver -- on the ESP32. The only thing this script supplies is the clock: by
default a noon sun at 60x, so each real second is a solar minute and the motion
is visible on the bench. --now uses the true current time (at night that just
means a stowed mirror, which is the correct behaviour).

The run ends by stowing, then releasing torque.

Deploy first:  python3 tools/deploy.py
"""

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from port import default_port  # noqa: E402

BOARD_SCRIPT = r"""
import asyncio, gc
from config import ConfigStore
from control.clock import Clock
from control.tracker import Tracker
from hal import board
from util import log
from util.monotime import monotonic

log.configure("info")
cfg = ConfigStore(data={
    "sim": False,
    "site": {"lat": %(lat)r, "lon": %(lon)r},
    "target": {"mode": "azel", "az": %(taz)r, "el": %(tel)r},
}).data
az, el = board.make_axes(cfg, monotonic)
sensors = board.make_sensors(cfg, monotonic, (az, el))
clock = Clock(monotonic, epoch=%(epoch)r, rate=%(rate)r)
tr = Tracker(cfg, az, el, clock, monotonic, sensors)

def f(v, fmt="{:6.1f}"):
    return "   -  " if v is None else fmt.format(v)

def row(tag):
    s = tr.status()
    sun = s["sun"] or (None, None)
    plan = s["plan"] or (None, None)
    a, e = s["axes"]
    trips = ",".join(t["rule"] for t in s["trips"])
    print("{:>4} {:<7} sun {} {}  plan {} {}  pos {} {}  mv {}{}  V {}/{}  {}".format(
        tag, s["mode"], f(sun[0]), f(sun[1]), f(plan[0]), f(plan[1]),
        f(a["position_deg"]), f(e["position_deg"]),
        int(a["moving"]), int(e["moving"]),
        f(a["volts"], "{:.1f}"), f(e["volts"], "{:.1f}"), trips))

async def go():
    tr.touch()
    await tr.step()
    row("t0")
    tr.request_mode("track")
    for i in range(%(seconds)d):
        tr.touch()
        await tr.step()
        row(str(i))
        await asyncio.sleep(1)
    print("--- stowing")
    tr.request_mode("stow") if tr.mode != "stow" else None
    for i in range(15):
        tr.touch()
        await tr.step()
        row("s" + str(i))
        if tr.az.settled and tr.el.settled and i > 2:
            break
        await asyncio.sleep(1)
    tr.request_mode("idle")
    tr.az.torque(False); tr.el.torque(False)
    gc.collect()
    print("--- done; torque released; RAM free", gc.mem_free())
    print("missed replies: az", tr.az.state["missed"], "el", tr.el.state["missed"])

asyncio.run(go())
"""


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--port", default=default_port())
    parser.add_argument("--lat", type=float, default=51.4779)
    parser.add_argument("--lon", type=float, default=-0.0015)
    parser.add_argument("--target-az", type=float, default=180.0)
    parser.add_argument("--target-el", type=float, default=10.0)
    parser.add_argument("--hour", type=float, default=12.0, help="UTC hour of the simulated sun")
    parser.add_argument("--rate", type=float, default=60.0, help="solar seconds per real second")
    parser.add_argument("--seconds", type=int, default=45, help="real seconds of tracking")
    parser.add_argument("--now", action="store_true", help="use the true current time, rate 1")
    args = parser.parse_args()

    today = datetime.datetime.now(datetime.timezone.utc)
    if args.now:
        epoch, rate = today.timestamp(), 1.0
    else:
        start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        epoch = (start + datetime.timedelta(hours=args.hour)).timestamp()
        rate = args.rate

    script = BOARD_SCRIPT % {
        "lat": args.lat, "lon": args.lon, "taz": args.target_az, "tel": args.target_el,
        "epoch": int(epoch), "rate": rate, "seconds": args.seconds,
    }
    return subprocess.call(
        [sys.executable, "-m", "mpremote", "connect", args.port, "exec", script]
    )


if __name__ == "__main__":
    raise SystemExit(main())
