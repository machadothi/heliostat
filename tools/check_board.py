#!/usr/bin/env python3
"""Probe the board for the facts that constrain the firmware design.

This is the FIRST thing to run on a new board. Two of these answers change how
the solar math must be written:

  * Float precision. If MicroPython was built with single-precision floats, a
    Julian Day near 2_460_000 has only a 24-bit mantissa behind it, giving a
    resolution of roughly 0.25 days -- six hours. The solar position would be
    silently, catastrophically wrong. solar/julian.py avoids ever materialising
    a full JD as a float, but we need to know which regime we are in.

  * Epoch. The ESP32 port of MicroPython counts from 2000-01-01, not 1970. The
    phone sends a Unix epoch. If these disagree the offset is 946_684_800 s and
    every conversion must go through one constant in one place.

Usage:
    python3 tools/check_board.py [--port PORT]
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from port import default_port  # noqa: E402

# Seconds between 1970-01-01 and 2000-01-01.
UNIX_TO_2000 = 946_684_800

PROBE = r"""
import math, time, sys, os, gc

print("MPY_VERSION", sys.version)
print("PLATFORM", sys.platform)
print("IMPL", sys.implementation)

# --- float precision -------------------------------------------------------
pi = repr(math.pi)
print("PI", pi)
print("PRECISION", "double" if pi == "3.141592653589793" else "single")

# The number that actually matters: can a Julian Day resolve one second?
jd = 2460000.5
one_second = 1.0 / 86400.0
print("JD_PLUS_1S_DISTINCT", (jd + one_second) != jd)
print("JD_PLUS_1H_DISTINCT", (jd + 1.0 / 24.0) != jd)

# --- epoch -----------------------------------------------------------------
print("EPOCH_TUPLE", time.gmtime(0))

# --- arbitrary precision integers (julian.py depends on these) -------------
print("BIG_INT", 2 ** 70)
print("MAXSIZE", sys.maxsize)

# --- resources -------------------------------------------------------------
gc.collect()
print("MEM_FREE", gc.mem_free())
s = os.statvfs("/")
print("FLASH_FREE_KB", s[0] * s[3] // 1024)
print("FLASH_TOTAL_KB", s[0] * s[2] // 1024)

# --- modules the design assumes --------------------------------------------
for name in ("bluetooth", "network", "ntptime", "asyncio", "machine"):
    try:
        __import__(name)
        print("MODULE", name, "ok")
    except Exception as exc:
        print("MODULE", name, "MISSING", exc)
"""


def run_probe(port: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "mpremote", "connect", port, "exec", PROBE],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        sys.exit(f"mpremote failed:\n{result.stderr.strip()}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=default_port())
    args = parser.parse_args()

    output = run_probe(args.port)
    facts = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            facts[parts[0]] = parts[1]

    print(output)
    print("=" * 62)

    precision = facts.get("PRECISION")
    if precision == "double":
        print("FLOATS   double precision - solar math has full headroom.")
    elif precision == "single":
        print("FLOATS   SINGLE precision.")
        print("         A full Julian Day as a float cannot resolve an hour.")
        print("         solar/julian.py MUST carry time as (jdn:int, frac:float).")
    else:
        print("FLOATS   could not determine precision")

    if facts.get("JD_PLUS_1S_DISTINCT") == "False":
        print("         Confirmed: JD + 1 second is indistinguishable from JD.")
    if facts.get("JD_PLUS_1H_DISTINCT") == "False":
        print("         Confirmed: JD + 1 HOUR is indistinguishable from JD.")

    epoch = facts.get("EPOCH_TUPLE", "")
    if epoch.startswith("(2000"):
        print(f"EPOCH    2000-01-01. Unix offset = {UNIX_TO_2000} s.")
        print("         net/timesync.py must subtract this from phone timestamps.")
    elif epoch.startswith("(1970"):
        print("EPOCH    1970-01-01, same as Unix. No conversion needed.")
    else:
        print(f"EPOCH    unexpected: {epoch}")

    print("=" * 62)
    print("Record these in docs/wiring.md before writing solar/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
