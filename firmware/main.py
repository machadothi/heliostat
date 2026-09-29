"""Entry point. Runs on every boot, after boot.py.

SAFE-BOOT GUARD: hold the BOOT button (GPIO0) while resetting and the application
is skipped entirely, dropping you at the REPL. Without this, a single bad
asyncio.run() in a tight loop means an erase-and-reflash to recover the board.

Any exception escaping app.run() also falls through to the REPL rather than
rebooting, so a crash is debuggable instead of being a reset loop.
"""

import sys

from machine import Pin

_BOOT_BUTTON = 0


def _safe_mode_requested():
    # BOOT is pulled up; pressed reads low.
    return not Pin(_BOOT_BUTTON, Pin.IN, Pin.PULL_UP).value()


if _safe_mode_requested():
    print("=" * 46)
    print(" SAFE MODE - application not started")
    print(" Release BOOT and reset to run normally.")
    print("=" * 46)
else:
    try:
        import app

        app.run()
    except KeyboardInterrupt:
        print("interrupted -> REPL")
    except Exception as exc:  # noqa: BLE001 - last resort, must not reboot
        print("application crashed -> REPL")
        sys.print_exception(exc)
