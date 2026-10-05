"""Diagnostics for the logs and /api/status: memory, and why the board last reset."""


def idf_heap():
    """(free, largest free block) of the ESP-IDF data heap, or None off-board.

    This is the heap WiFi and lwIP allocate from -- separate from the Python
    heap gc.mem_free() reports. When it runs dry the board stops answering even
    ping, while Python carries on unaware.
    """
    try:
        import esp32

        regions = esp32.idf_heap_info(esp32.HEAP_DATA)
        return sum(r[1] for r in regions), max(r[2] for r in regions)
    except Exception:  # noqa: BLE001
        return None


# machine.reset_cause() on the ESP32 port lumps causes together: a brownout
# reads as power-on -- and so does the EN/reset button, which on the classic
# ESP32 IS a power-on reset -- while a crash (panic) reads as a hard reset, same
# as machine.reset(). Coarse, but it still separates "power" from "crash" from
# "watchdog". The exact
# ESP-IDF reason is on the serial console at boot: rst:0xc is a panic, 0xf a
# brownout, 0x1 power-on.
_RESET_CAUSES = {
    1: "power-on, reset button or brownout",
    2: "machine.reset() or a crash",
    3: "watchdog",
    4: "deep sleep",
    5: "soft reset",
}


def reset_cause():
    """Why the board last reset, in words; None off-board."""
    try:
        import machine

        cause = machine.reset_cause()
    except Exception:  # noqa: BLE001
        return None
    return _RESET_CAUSES.get(cause, "unknown ({})".format(cause))
