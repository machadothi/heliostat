"""Runs at every boot (after boot.py). A stub that should never need to change:
the real start-up -- safe mode, the app, rollback of a bad update -- is
startup.py, frozen into the firmware, so updates over WiFi bring it along.
Deployed once over USB by tools/deploy.py.

If even importing startup fails, and the firmware came over WiFi and has not
proved itself (the "ota_pending" file), reset: the bootloader then rolls back
to the previous firmware. Otherwise stay at the REPL to debug.
"""

try:
    import startup  # noqa: F401
except Exception as exc:  # noqa: BLE001 - last resort
    import sys

    sys.print_exception(exc)
    try:
        import os

        os.stat("ota_pending")
        os.remove("ota_pending")
        print("unverified OTA firmware cannot start: resetting to roll back")
        import machine

        machine.reset()
    except OSError:
        pass
