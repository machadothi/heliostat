#!/usr/bin/env python3
"""Find the servos on the bus, and give a newly added servo its own ID.

    python3 tools/servo_id.py scan                  # every ID: answering, garbled (conflict) or silent
    python3 tools/servo_id.py add                   # new servo at factory ID 1 -> lowest free ID from 2
    python3 tools/servo_id.py add --to 3            # ... or a chosen ID
    python3 tools/servo_id.py assign 1 2            # change any servo's ID (old -> new)
    python3 tools/servo_id.py scan --family scs     # SC09 / SCS-series servos

Feetech/Waveshare bus servos all leave the factory as ID 1. Two of them on the
same bus both answer to ID 1, at the same instant, and their replies collide on
the shared wire -- garbage instead of a reply, and neither can be commanded. So a
new servo is given its own ID BEFORE it joins the chain:

    1. Connect ONLY the new servo to the bus (power on).
    2. python3 tools/servo_id.py add
    3. Reconnect the chain. `scan` should now show every servo as answering.
    4. Make the firmware config point the right axis at the new ID.

Why a ping is not enough to spot a conflict: identical servos at the same ID
send byte-identical ping replies, which can decode cleanly. `scan` therefore also
reads each servo's position block repeatedly -- two servos at different angles
cannot agree on those bytes. Any ID that produces bytes but no valid reply is
reported as GARBLED.

Writing the ID is refused unless exactly one servo answers cleanly at the old
ID and nothing at all answers at the new one. The ID lives in EEPROM, so it is
unlocked, written and locked again, then checked by pinging both IDs.

This runs on the board through tools/mpr.py, which stops the heliostat app for
the duration (it must not drive the bus meanwhile) and restarts it afterwards.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Runs on the ESP32. Raw frames rather than hal/scservo.py: telling a garbled
# reply from silence needs the bytes the driver throws away.
BOARD_CODE = r'''
from machine import UART
import time

LOCK = {"sts": 55, "scs": 48}[FAMILY]
REG_ID = 5
try:  # the servo pins of whichever board this is (firmware installed)
    from hal import profiles
    _p = profiles.current()
    _uart, _tx, _rx = _p["servo_uart"], _p["servo_tx"], _p["servo_rx"]
except ImportError:  # bare MicroPython: assume the devkit wiring
    _uart, _tx, _rx = 2, 17, 16
u = UART(_uart, baudrate=1000000, tx=_tx, rx=_rx, timeout=0)


def frame(sid, inst, params=b""):
    body = bytes((sid, len(params) + 2, inst)) + params
    return b"\xff\xff" + body + bytes(((~sum(body)) & 0xFF,))


def transact(sid, inst, params=b"", wait_ms=4):
    while u.any():
        u.read()
    u.write(frame(sid, inst, params))
    time.sleep_ms(wait_ms)
    return u.read() or b""


def valid(reply, sid, n_params):
    """The parameter bytes of a clean status packet from `sid`, else None."""
    if len(reply) != n_params + 6 or reply[0] != 0xFF or reply[1] != 0xFF:
        return None
    if reply[2] != sid or reply[3] != n_params + 2:
        return None
    if (~sum(reply[2:5 + n_params])) & 0xFF != reply[5 + n_params]:
        return None
    return reply[5:5 + n_params]


def probe(sid, reads=20):
    """'ok', 'garbled' or 'silent' -- plus voltage when ok."""
    replies = [transact(sid, 1) for _ in range(3)]
    if not any(replies):
        return "silent", None
    if any(valid(r, sid, 0) is None for r in replies if r):
        return "garbled", None
    volts = None
    bad = 0
    for _ in range(reads):
        block = valid(transact(sid, 2, bytes((56, 11))), sid, 11)
        if block is None:
            bad += 1  # one glitch is noise; two servos colliding garble nearly every read
        else:
            volts = block[6] / 10
    if bad > 2 or (reads and volts is None):
        return "garbled", None
    return "ok", volts


def write_byte(sid, addr, value):
    """True if the servo acknowledged the write."""
    return valid(transact(sid, 3, bytes((addr, value)), wait_ms=10), sid, 0) is not None


def scan():
    seen = {}
    for sid in range(254):
        state, volts = probe(sid, reads=0)
        if state != "silent":
            seen[sid] = probe(sid) if state == "ok" else (state, volts)
    return seen


def report(seen):
    if not seen:
        print("  no servo answers on any ID -- is servo power on and the bus cable seated?")
    for sid, (state, volts) in sorted(seen.items()):
        if state == "ok":
            print("  ID %3d  answering  %.1f V" % (sid, volts))
        else:
            print("  ID %3d  GARBLED    two or more servos share this ID -- connect them one at a time" % sid)


if ACTION == "scan":
    print("scanning IDs 0-253 (%s)..." % FAMILY.upper())
    report(scan())
    print("RESULT ok")
else:
    old = OLD
    print("checking ID %d (%s)..." % (old, FAMILY.upper()))
    state, volts = probe(old)
    seen = scan()
    new = NEW
    if new is None:
        new = next(i for i in range(2, 254) if i != old and i not in seen)
    if state == "silent":
        print("nothing answers at ID %d. Seen on the bus:" % old)
        report(seen)
        print("RESULT fail")
    elif state == "garbled":
        print("ID %d is GARBLED: more than one servo answers to it. Connect ONLY the servo" % old)
        print("whose ID should change, then run this again.")
        print("RESULT fail")
    elif new in seen:
        print("ID %d is already taken on this bus:" % new)
        report(seen)
        print("RESULT fail")
    else:
        others = [i for i in seen if i != old]
        if others:
            print("note: also on the bus: IDs %s (not touched)" % others)
        print("changing ID %d -> %d (%.1f V)..." % (old, new, volts))
        ok = write_byte(old, LOCK, 0)
        # The acknowledgement of the ID write itself may come from either ID, so
        # it is not checked here: re-locking under the NEW ID, and the pings
        # below, are the proof.
        transact(old, 3, bytes((REG_ID, new)), wait_ms=10)
        time.sleep_ms(20)
        ok = ok and write_byte(new, LOCK, 1)
        time.sleep_ms(20)
        after_new, _ = probe(new)
        after_old, _ = probe(old, reads=0)
        if ok and after_new == "ok" and after_old == "silent":
            print("done: the servo now answers as ID %d, and nothing answers at ID %d" % (new, old))
            print("RESULT ok")
        else:
            print("FAILED: writes %s, ID %d is %s, ID %d is %s"
                  % ("acknowledged" if ok else "NOT acknowledged", new, after_new, old, after_old))
            print("RESULT fail")
'''


def run_on_board(action, family, old=None, new=None):
    code = "ACTION = {!r}\nFAMILY = {!r}\nOLD = {!r}\nNEW = {!r}\n".format(action, family, old, new)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as script:
        script.write(code + BOARD_CODE)
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "mpr.py"), "run", script.name],
        capture_output=True, text=True, timeout=180,
    )
    Path(script.name).unlink()
    output = result.stdout + result.stderr
    lines = output.strip().splitlines()
    for line in lines:
        if not line.startswith("RESULT"):
            print(line)
    if "RESULT ok" not in output:
        if "RESULT" not in output:
            print("(the board did not finish -- is it plugged in? see tools/port.py)")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--family", choices=("sts", "scs"), default="sts",
                        help="sts: ST3020/ST3215 (default); scs: SC09 and friends")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("scan", help="list every servo on the bus, flagging ID conflicts")
    add = sub.add_parser("add", help="give a NEW servo (alone on the bus) its own ID")
    add.add_argument("--from", dest="old", type=int, default=1, help="its current ID (factory: 1)")
    add.add_argument("--to", dest="new", type=int, help="the ID to give it (default: lowest free from 2)")
    assign = sub.add_parser("assign", help="change a servo's ID")
    assign.add_argument("old", type=int)
    assign.add_argument("new", type=int)
    args = parser.parse_args()

    for value in (getattr(args, "old", None), getattr(args, "new", None)):
        if value is not None and not 0 <= value <= 253:
            parser.error("servo IDs are 0-253 (254 is broadcast)")
    if args.action in ("add", "assign") and args.new is not None and args.new == args.old:
        parser.error("old and new ID are the same")

    if args.action == "scan":
        run_on_board("scan", args.family)
    else:
        run_on_board("assign", args.family, args.old, args.new)


if __name__ == "__main__":
    main()
