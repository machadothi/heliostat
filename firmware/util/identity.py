"""Who this heliostat is: a stable id that survives renames and new IP addresses."""


def device_id():
    """12 hex digits from the ESP32's factory MAC; a fixed stand-in off-board.

    The phone remembers a heliostat by this id, not by its IP: DHCP can hand the
    board a new address, and two boards can share a name.
    """
    try:
        import machine

        return "".join("{:02x}".format(b) for b in machine.unique_id())
    except Exception:  # noqa: BLE001 - CPython (mock server, tests)
        return "000000000000"
