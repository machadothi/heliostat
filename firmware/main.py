"""Entry point. Runs on every boot, after boot.py.

SAFE-BOOT GUARD: hold the board's button (BOOT/GPIO0 on the devkit, the screen
button/G41 on the AtomS3R) while resetting and the application is skipped
entirely, dropping you at the REPL. Without this, a single bad
asyncio.run() in a tight loop means an erase-and-reflash to recover the board.

Any exception escaping app.run() also falls through to the REPL rather than
rebooting, so a crash is debuggable instead of being a reset loop.
"""

import sys

from machine import Pin

from hal import profiles

_BOOT_BUTTON = profiles.current()["button"]


def _safe_mode_requested():
    # The button is pulled up; pressed reads low.
    return not Pin(_BOOT_BUTTON, Pin.IN, Pin.PULL_UP).value()


_wrong_board = profiles.mismatch()
if _wrong_board or _safe_mode_requested():
    print("=" * 46)
    print(" SAFE MODE - application not started")
    print(" " + (_wrong_board or "Release the button and reset to run normally."))
    print("=" * 46)
else:
    try:
        # One-second grace window: a Ctrl-C now lands at the REPL before the app
        # (and Bluetooth) ever starts. tools/mpr.py relies on it to reach a clean
        # REPL without a soft reset -- which can hang the ESP32 after BLE has run.
        import select
        import time

        print("starting app in 1 s (Ctrl-C for the REPL)")
        # Wait while READING the console, not in time.sleep(): on the AtomS3R's
        # USB-Serial-JTAG, Ctrl-C only arrives while someone reads stdin (see
        # app.drain_console).
        _poller = select.poll()
        _poller.register(sys.stdin, select.POLLIN)
        _until = time.ticks_add(time.ticks_ms(), 1000)
        while time.ticks_diff(_until, time.ticks_ms()) > 0:
            if _poller.poll(20):
                sys.stdin.read(1)

        # Initialise the WiFi driver FIRST, before any large module is loaded.
        # Its receive buffers come from the same RAM the Python heap grows into;
        # created after the app's modules they could not be allocated
        # ("Expected to init 10 rx buffer, actual is 3" -> Wifi Unknown Error
        # 0x0101, ESP_ERR_NO_MEM), and the app died at startup.
        import network

        network.WLAN(network.STA_IF)
        import app

        app.run()
    except KeyboardInterrupt:
        print("interrupted -> REPL")
    except Exception as exc:  # noqa: BLE001 - last resort, must not reboot
        print("application crashed -> REPL")
        sys.print_exception(exc)
