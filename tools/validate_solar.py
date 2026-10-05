#!/usr/bin/env python3
"""Validate the firmware's solar math, on the host and optionally on the board.

    python3 tools/validate_solar.py            # host only
    python3 tools/validate_solar.py --board    # also run it on the ESP32

The host pass compares firmware/solar against the frozen pvlib reference. The
board pass runs the SAME code on the ESP32 and diffs the two results.

Why the board pass matters: a divergence between host and board means the
board's float implementation is hurting us, and it is the only way to find that
out short of watching the mirror point at the wrong part of the sky. Anything
above about 0.001 degrees is worth investigating; see the single-precision note
in firmware/solar/julian.py.

The error metric is the angular separation of the sun VECTOR, not the azimuth
difference. Azimuth is ill-conditioned near the zenith and exaggerates
differences that barely exist in physical terms.
"""

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "tools" / "reference" / "noaa_cases.json"
sys.path.insert(0, str(REPO / "firmware"))

from solar.sunpos import sun_position  # noqa: E402  (needs the path set first)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from port import default_port  # noqa: E402

VECTOR_TOLERANCE_DEG = 0.02
# Host vs board. The ESP32 build uses SINGLE-precision floats (confirmed by
# tools/check_board.py), which leaves a few thousandths of a degree between
# the two; the pass criterion is the board against NREL SPA, below.
BOARD_TOLERANCE_DEG = 0.01


def unit_vector(azimuth_deg, elevation_deg):
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return (math.cos(el) * math.sin(az), math.cos(el) * math.cos(az), math.sin(el))


def angle_between(a, b):
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))
    return math.degrees(math.acos(dot))


def load_cases():
    return json.loads(REFERENCE.read_text())["cases"]


def run_on_board(cases, port):
    """Execute firmware/solar on the ESP32 and return {label: (az, el)}."""
    payload = json.dumps([[c["label"], c["unix"], c["lat"], c["lon"]] for c in cases])
    script = (
        "import json\n"
        "from solar.sunpos import sun_position\n"
        f"cases = json.loads('{payload}')\n"
        "for label, unix, lat, lon in cases:\n"
        "    az, el, _ = sun_position(unix, lat, lon)\n"
        "    print('R', label, az, el)\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "mpremote", "connect", port, "exec", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        sys.exit(
            "board run failed (is the firmware deployed? try python3 tools/deploy.py)\n"
            + result.stderr.strip()
        )

    board = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "R":
            board[parts[1]] = (float(parts[2]), float(parts[3]))
    return board


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", action="store_true", help="also run on the ESP32")
    parser.add_argument("--port", default=default_port())
    parser.add_argument("--verbose", action="store_true", help="print every case")
    args = parser.parse_args()

    cases = load_cases()
    board = run_on_board(cases, args.port) if args.board else {}

    worst_host = worst_board = worst_board_spa = 0.0
    worst_host_label = worst_board_label = None
    failures = 0

    header = f"{'case':<24}{'vs SPA':>10}"
    if args.board:
        header += f"{'vs board':>11}"
    print(header)
    print("-" * len(header))

    for case in cases:
        az, el, _ = sun_position(case["unix"], case["lat"], case["lon"])
        host_error = angle_between(
            unit_vector(az, el), unit_vector(case["azimuth"], case["elevation"])
        )
        worst_host, worst_host_label = max(
            (worst_host, worst_host_label), (host_error, case["label"])
        )

        board_error = None
        if case["label"] in board:
            b_az, b_el = board[case["label"]]
            board_error = angle_between(unit_vector(az, el), unit_vector(b_az, b_el))
            worst_board, worst_board_label = max(
                (worst_board, worst_board_label), (board_error, case["label"])
            )
            board_vs_spa = angle_between(
                unit_vector(b_az, b_el), unit_vector(case["azimuth"], case["elevation"])
            )
            worst_board_spa = max(worst_board_spa, board_vs_spa)
            if board_vs_spa >= VECTOR_TOLERANCE_DEG:
                failures += 1

        over = host_error >= VECTOR_TOLERANCE_DEG or (
            board_error is not None and board_error >= BOARD_TOLERANCE_DEG
        )
        failures += over
        if over or args.verbose:
            line = f"{case['label']:<24}{host_error:>10.5f}"
            if board_error is not None:
                line += f"{board_error:>11.6f}"
            print(line + ("  <-- OVER" if over else ""))

    print("-" * len(header))
    print(f"cases                      {len(cases)}")
    print(f"worst vs NREL SPA          {worst_host:.5f} deg  ({worst_host_label})")
    print(f"  doubled at the beam      {2 * worst_host:.5f} deg")
    print("  sun's angular diameter   0.53 deg")
    if args.board:
        print(f"worst BOARD vs NREL SPA    {worst_board_spa:.5f} deg  <- what the machine uses")
        print(f"worst host vs board        {worst_board:.6f} deg  ({worst_board_label})")
        if worst_board >= BOARD_TOLERANCE_DEG:
            print("  ^ the board's float precision is degrading the result")

    print("PASS" if failures == 0 else f"FAIL ({failures} case(s) over tolerance)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
