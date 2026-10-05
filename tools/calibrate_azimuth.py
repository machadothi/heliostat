#!/usr/bin/env python3
"""Find true north for the yaw axis with the AtomS3R's magnetometer.

    python3 tools/calibrate_azimuth.py                 # measure and report
    python3 tools/calibrate_azimuth.py --apply         # ... and write the new offset
    python3 tools/calibrate_azimuth.py --declination 6.6

Before running: tape the AtomS3R FLAT on the mirror, with its cables slack
enough for the whole yaw travel and for tipping the mirror to vertical, and
keep hands clear. The heliostat must be running from that Atom.

What it does, over the HTTP API (the board must be reachable):
  1. mirror face-up; the yaw steps across its whole range and back, reading the
     magnetometer at rest at every stop (GET /api/imu);
  2. at mid yaw, the mirror tips from face-up to near-vertical in steps;
  3. the fit (tools/azimuth_fit.py) finds magnetic north and the mirror's
     front in the sensor's frame, then the yaw servo angle that faces true
     north -- the azimuth axis's offset_deg;
  4. with --apply, and only if the per-stop spread is small, it posts the new
     offset and shifts the azimuth limits by the same amount, so they stay on
     the same physical stops.

Declination (true minus magnetic north) comes from the World Magnetic Model
for the site in the board's config if `pygeomag` is installed; otherwise pass
--declination (NOAA's calculator gives it for any place and date).
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import azimuth_fit  # noqa: E402

MAX_SPREAD_DEG = 2.0  # per-stop disagreement above this: magnets nearby; do not apply
HORIZONTAL_FIELD_UT = (8.0, 30.0)  # plausible range for the fitted circle's radius


class Heliostat:
    def __init__(self, host):
        self.base = f"http://{host}/api"

    def get(self, path, timeout=10):
        return json.load(urllib.request.urlopen(self.base + path, timeout=timeout))

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return json.load(urllib.request.urlopen(req, timeout=10))

    def move(self, axis, deg, settle_s):
        """Jog one axis to an absolute angle and wait until it has stopped."""
        self.post("/jog", {"axis": axis, "abs_deg": deg})
        time.sleep(0.5)
        for _ in range(120):
            t = self.get("/telemetry")
            if not any(t["moving"]):
                break
            time.sleep(0.25)
        time.sleep(settle_s)
        return self.get("/telemetry")["pos"]

    def field(self, n=20):
        reading = self.get(f"/imu?n={n}", timeout=20)
        if reading["mag_ut"] is None:
            raise SystemExit("no magnetometer: this needs the heliostat running on the AtomS3R")
        return reading["mag_ut"]


def declination_for(site, given):
    if given is not None:
        return given, "given"
    try:
        from pygeomag import GeoMag
    except ImportError:
        raise SystemExit(
            "declination needed: pip install pygeomag, or pass --declination DEG "
            "(east positive; Stockholm 2026 is about +6.6)"
        ) from None
    year = datetime.date.today()
    decimal_year = year.year + (year.timetuple().tm_yday - 1) / 365.25
    result = GeoMag().calculate(glat=site["lat"], glon=site["lon"],
                                alt=site.get("elev_m", 0.0) / 1000.0, time=decimal_year)
    return result.d, "WMM"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("HELIOSTAT_HOST", "192.168.50.105"))
    ap.add_argument("--declination", type=float, help="degrees, east positive")
    ap.add_argument("--step", type=float, default=10.0, help="yaw step, degrees")
    ap.add_argument("--settle", type=float, default=1.5, help="seconds at rest before reading")
    ap.add_argument("--apply", action="store_true", help="write the new offset to the board")
    ap.add_argument("--save", help="also write the raw sweep data to this JSON file")
    args = ap.parse_args()

    h = Heliostat(args.host)
    status = h.get("/status")
    cfg = h.get("/config")
    az_axis = cfg["axes"][0]
    declination, source = declination_for(cfg["site"], args.declination)
    print(f"declination {declination:+.2f} deg ({source}); current az offset {az_axis['offset_deg']}")
    h.field(n=3)  # fails early without a magnetometer
    if status["latch"]:
        raise SystemExit(f"latched fault: {status['latch']['reason']} -- clear it first")

    h.post("/mode", {"mode": "idle"})
    h.post("/mode", {"mode": "manual"})
    lo, hi = az_axis["min_deg"] + 1.0, az_axis["max_deg"] - 1.0
    direction, gear = az_axis["direction"], az_axis["gear_ratio"]

    def servo_of(mech):  # the raw yaw servo angle, from the current calibration
        return az_axis["offset_deg"] + direction * gear * mech

    data = {"yaw": [], "tilt": []}
    try:
        print("mirror face-up")
        h.move(1, 90.0, args.settle)
        stops = []
        a = lo
        while a <= hi + 1e-6:
            stops.append(round(a, 2))
            a += args.step
        for leg, sequence in (("out", stops), ("back", stops[::-1])):
            for az in sequence:
                pos = h.move(0, az, args.settle)
                m = h.field()
                data["yaw"].append({"az": pos[0], "servo": servo_of(pos[0]), "mag": m})
                print(f"  yaw {leg:4s} az {pos[0]:7.2f}  field {m[0]:7.2f} {m[1]:7.2f} {m[2]:7.2f} uT")
        mid = 0.5 * (lo + hi)
        h.move(0, mid, args.settle)
        for el in range(90, 5, -10):
            pos = h.move(1, float(el), args.settle)
            m = h.field()
            data["tilt"].append({"el": pos[1], "mag": m})
            print(f"  tilt el {pos[1]:6.2f}  field {m[0]:7.2f} {m[1]:7.2f} {m[2]:7.2f} uT")
    finally:
        try:
            h.move(1, 90.0, 0.5)
            h.post("/mode", {"mode": "idle"})
        except Exception as exc:  # noqa: BLE001
            print(f"could not return to face-up/idle: {exc}")

    if args.save:
        Path(args.save).write_text(json.dumps(data, indent=1))

    result = azimuth_fit.solve(
        [s["mag"] for s in data["yaw"]], [s["az"] for s in data["yaw"]],
        [s["servo"] for s in data["yaw"]],
        [s["mag"] for s in data["tilt"]], [s["el"] for s in data["tilt"]],
        direction=direction, gear_ratio=gear, declination_deg=declination,
    )
    current = az_axis["offset_deg"]
    new = round(azimuth_fit.wrap_offset(result["offset_deg"], current), 2)
    delta = new - current
    print()
    print(f"horizontal field {result['radius_uT']:.1f} uT, vertical {result['vertical_uT']:.1f} uT")
    print(f"per-stop spread {result['spread_deg']:.2f} deg over {len(data['yaw'])} stops; "
          f"field turned {result['tilt_turned_deg']:.0f} deg in the tilt sweep")
    print(f"az offset: current {current}, measured {new}  ({delta:+.2f} deg)")

    problems = []
    tilt_range = abs(data["tilt"][0]["el"] - data["tilt"][-1]["el"])
    if result["tilt_turned_deg"] < 0.5 * tilt_range:
        problems.append(f"the field turned only {result['tilt_turned_deg']:.0f} deg while the mirror "
                        f"tipped {tilt_range:.0f} deg: the Atom is not riding on the mirror")
    if result["spread_deg"] > MAX_SPREAD_DEG:
        problems.append(f"spread {result['spread_deg']:.1f} deg > {MAX_SPREAD_DEG}: magnetic "
                        "disturbance moving with the yaw (servo magnets, steel nearby)")
    if not HORIZONTAL_FIELD_UT[0] <= result["radius_uT"] <= HORIZONTAL_FIELD_UT[1]:
        problems.append(f"horizontal field {result['radius_uT']:.1f} uT is implausible here")
    if problems:
        print("NOT APPLIED:\n  " + "\n  ".join(problems))
        return 1
    if not args.apply:
        print("(report only; rerun with --apply to write it)")
        return 0

    # mech = (servo - offset) / (direction * gear): the limits keep their servo angles.
    shift = -delta / (direction * gear)
    az_axis.update(offset_deg=new, min_deg=round(az_axis["min_deg"] + shift, 2),
                   max_deg=round(az_axis["max_deg"] + shift, 2))
    reply = h.post("/config", {"axes": [az_axis, cfg["axes"][1]]})
    print(f"applied: offset {new}, limits {az_axis['min_deg']} .. {az_axis['max_deg']}"
          + ("  (restart the board to use it)" if reply.get("restart_required") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
