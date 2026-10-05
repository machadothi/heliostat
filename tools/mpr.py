#!/usr/bin/env python3
"""mpremote, made reliable for this board: hard reset -> clean REPL -> no soft reset.

    python3 tools/mpr.py exec "print(1)"
    python3 tools/mpr.py fs cp firmware/app.py :
    python3 tools/mpr.py --run-app          # just hard-reset and let the app start
    python3 tools/mpr.py --stay exec "..."  # stay at the REPL afterwards

After every command the board is reset back into the app, so Bluetooth keeps
advertising unless --stay is given.

Why this exists. mpremote soft-resets the interpreter on every connection. On
the ESP32, a soft reset after Bluetooth has been active can hang the chip until
the watchdog fires ("PRO CPU has been reset by WDT"); after that mpremote cannot
get in until someone presses the reset button.

So instead:
  1. HARD-reset the board over the USB-serial control lines: RTS pulsed with
     DTR released, as esptool does. That pulls EN low through the devkit's
     auto-reset circuit, and resets the AtomS3R's chip through its built-in
     USB-Serial-JTAG;
  2. send Ctrl-C through the 1 s grace window in main.py, so the app, and
     Bluetooth, never start;
  3. hand over to mpremote with `resume`, which skips its soft reset.
"""

import subprocess
import sys
import time

import serial

sys.path.insert(0, __import__("os").path.dirname(__file__))
from port import default_port  # noqa: E402

PORT = default_port()
PROMPT = b">>> "


def _open_and_reset(port, timeout=0.05):
    """Open the port and hard-reset the board. Returns the open port."""
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, timeout
    # Never let a write block forever: a board that is not reading its console
    # used to hang this tool (and every mpremote after it).
    s.write_timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()
    s.reset_output_buffer()  # drop anything a killed earlier session left queued
    s.rts = True  # esptool's hard reset: EN low...
    time.sleep(0.1)
    s.rts = False  # ...and released
    return s


def hard_reset_to_repl(port=PORT, timeout=8.0):
    """Reset the board and interrupt main.py. Returns True once a REPL prompt is seen."""
    s = _open_and_reset(port)
    try:
        seen, reset_seen = b"", False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                s.write(b"\x03")  # harmless at the REPL; interrupts main.py's grace window
            except serial.SerialTimeoutException:
                pass  # the board is not reading yet (still booting)
            seen += s.read(512)
            if not reset_seen and b"rst:" in seen:
                # Only trust a prompt printed AFTER this reset's boot banner.
                reset_seen, seen = True, seen[seen.index(b"rst:"):]
            if reset_seen and PROMPT in seen:
                return True
        return False
    finally:
        s.close()


def reset_into_app(port=PORT):
    """Hard-reset and let main.py start the app (and Bluetooth) as it would at power-up."""
    _open_and_reset(port).close()


def main(argv):
    port = PORT
    if argv[:1] == ["--port"]:
        port, argv = argv[1], argv[2:]
    if argv == ["--run-app"]:
        reset_into_app(port)
        print("board hard-reset; the app starts in about 2 s")
        return 0
    # By default the board goes back to running the app afterwards. Leaving it at
    # the REPL means no Bluetooth, which looks exactly like "my phone can't find
    # the heliostat". --stay keeps it at the REPL for a follow-up command.
    stay = "--stay" in argv
    argv = [a for a in argv if a != "--stay"]
    if not hard_reset_to_repl(port):
        print("[mpr] no REPL prompt after hard reset -- is the board connected?", file=sys.stderr)
        return 1
    status = subprocess.call([sys.executable, "-m", "mpremote", "connect", port, "resume", *argv])
    if not stay:
        reset_into_app(port)
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
