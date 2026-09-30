"""Memory readings for the logs and /api/status."""


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
