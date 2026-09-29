#!/usr/bin/env python3
"""Run a full simulated day through the real tracker code, on the laptop.

    python3 tools/sim_day.py
    python3 tools/sim_day.py --lat 38.72 --lon -9.14 --date 2026-06-21
    python3 tools/sim_day.py --target-az 200 --target-el 5 --every 30
    python3 tools/sim_day.py --no-comms      # phone out of range all day

Nothing here is a model of the tracker: it is firmware/control/tracker.py itself,
driving the simulated servos from firmware/hal, with time accelerated. Each loop
iteration is one second of servo time and `--step` seconds of solar time, so the
servos slew at their real speed while the sun races across the sky.

What to look for:
  * the machine sits stowed overnight, and starts tracking by itself once the
    sun clears the minimum elevation -- nobody pressed a button at dawn;
  * the mirror azimuth moves at about HALF the sun's rate;
  * the beam stays on target while tracking, within the servos' resolution;
  * it stows again at dusk;
  * the deadband keeps the number of servo commands far below the number of
    cycles.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware"))

from config import ConfigStore  # noqa: E402
from control import geometry as geo  # noqa: E402
from control.clock import Clock  # noqa: E402
from control.tracker import Tracker  # noqa: E402
from hal import board  # noqa: E402
from solar.julian import unix_from_civil  # noqa: E402
from util import log  # noqa: E402

GREENWICH = (51.4779, -0.0015)


class FakeMonotonic:
    """Real-time seconds that advance only when told to."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class CommandCounter:
    """Wraps a driver's move_deg to count how often the servo is commanded."""

    def __init__(self, driver):
        self.count = 0
        original = driver.move_deg

        async def counted(*args, **kwargs):
            self.count += 1
            return await original(*args, **kwargs)

        driver.move_deg = counted


def build(args):
    overlay = {
        "sim": True,
        "site": {"lat": args.lat, "lon": args.lon},
        "target": {"mode": "azel", "az": args.target_az, "el": args.target_el},
    }
    cfg = ConfigStore(data=overlay).data
    fake = FakeMonotonic()
    year, month, day = (int(part) for part in args.date.split("-"))
    midnight = unix_from_civil(year, month, day)
    clock = Clock(fake, epoch=midnight, rate=args.step)
    az, el = board.make_axes(cfg, fake)
    sensors = board.make_sensors(cfg, fake, (az, el))
    tracker = Tracker(cfg, az, el, clock, fake, sensors)
    return tracker, fake, midnight


def true_beam_error(tracker, target):
    """Beam error from where the joints REALLY are, bypassing encoder noise."""
    normal = tracker.mount.normal_from_joints(
        tracker.az.true_mech_deg(), tracker.el.true_mech_deg()
    )
    s = geo.sun_vector(*tracker._sun)
    return geo.angle_between_deg(geo.beam_direction(s, normal), target)


def percentiles(values):
    ordered = sorted(values)
    pick = lambda q: ordered[int(q * (len(ordered) - 1))]  # noqa: E731
    return f"median {pick(0.5):.2f}, 95th pct {pick(0.95):.2f}, max {ordered[-1]:.2f} deg"


def hhmm(seconds_after_midnight):
    minutes = int(seconds_after_midnight // 60) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_pair(pair, width=6):
    if pair is None or pair[0] is None:
        return " " * (2 * width + 1)
    return "{:{w}.1f} {:{w}.1f}".format(pair[0], pair[1], w=width)


async def simulate(args):
    tracker, fake, midnight = build(args)
    az_count = CommandCounter(tracker.az)
    el_count = CommandCounter(tracker.el)

    tracker.request_mode("track")  # armed at midnight; the sun is down

    target = geo.target_vector_from_azel(args.target_az, args.target_el)
    cycles = int(86400 / args.step)
    print_every = max(1, int(args.every * 60 / args.step))

    transitions = []
    beam_errors = []  # from MEASURED positions: includes encoder noise
    true_errors = []  # from TRUE positions: what the light actually does
    previous_mode = tracker.mode
    tracking_minutes = 0.0

    print(
        "UTC    mode      sun az   el    mirror az  el    beam az   el    eff   trips"
    )
    print("-" * 80)

    for cycle in range(cycles):
        fake.advance(1.0)
        if not args.no_comms:
            tracker.touch()
        await tracker.step()

        elapsed = tracker.clock.now() - midnight
        if tracker.mode != previous_mode:
            transitions.append((hhmm(elapsed), previous_mode, tracker.mode))
            previous_mode = tracker.mode

        if tracker.mode == "track":
            tracking_minutes += args.step / 60.0
            beam = tracker._beam
            if beam is not None:
                beam_errors.append(
                    geo.angle_between_deg(geo.vector_from_azel(*beam), target)
                )
                true_errors.append(true_beam_error(tracker, target))

        if cycle % print_every == 0:
            status = tracker.status()
            pos = (tracker.az.position_deg(), tracker.el.position_deg())
            eff = status["efficiency"]
            print(
                "{}  {:<8} {}  {}  {}  {:>5}  {}".format(
                    hhmm(elapsed),
                    tracker.mode,
                    fmt_pair(status["sun"]),
                    fmt_pair(pos),
                    fmt_pair(status["beam"]),
                    "" if eff is None else f"{eff:.2f}",
                    ",".join(t["rule"] for t in status["trips"]),
                )
            )

    print("-" * 80)
    print("mode changes:")
    for when, old, new in transitions:
        print(f"  {when}  {old} -> {new}")
    print(f"tracking time          {tracking_minutes:.0f} min")
    if beam_errors:
        print("beam error on target   (sun's own diameter is 0.53 deg)")
        print("  true pointing        " + percentiles(true_errors))
        print("  as measured          " + percentiles(beam_errors) + "  <- adds encoder noise")
    print(
        f"servo commands         az {az_count.count}, el {el_count.count}  over {cycles} cycles"
    )
    return transitions, beam_errors


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--lat", type=float, default=GREENWICH[0])
    parser.add_argument("--lon", type=float, default=GREENWICH[1])
    parser.add_argument("--date", default="2026-06-21", help="UTC date, YYYY-MM-DD")
    parser.add_argument("--target-az", type=float, default=180.0)
    parser.add_argument("--target-el", type=float, default=10.0)
    parser.add_argument("--step", type=float, default=60.0, help="solar seconds per cycle")
    parser.add_argument("--every", type=float, default=60.0, help="print interval, solar minutes")
    parser.add_argument("--no-comms", action="store_true", help="never touch(); phone absent")
    parser.add_argument("--verbose", action="store_true", help="show tracker log lines")
    args = parser.parse_args()

    log.configure("info" if args.verbose else "error")
    asyncio.run(simulate(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
