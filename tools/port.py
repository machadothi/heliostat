"""Which serial port and board: shared by every tool that talks to the board.

    HELIOSTAT_PORT   use this port, no detection
    HELIOSTAT_BOARD  use this board (a boards/<name>.json); its port is detected

Otherwise the USB devices in /dev/serial/by-id are matched against each board's
`usb_match`. Exactly one heliostat board plugged in: that one. Several: name
one with --board / HELIOSTAT_BOARD.
"""

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOARDS = REPO / "boards"
BY_ID = Path("/dev/serial/by-id")


class PortError(Exception):
    pass


def boards():
    """{name: config} for every boards/<name>.json."""
    return {p.stem: json.loads(p.read_text()) for p in sorted(BOARDS.glob("*.json"))}


def connected(by_id=BY_ID):
    """[(board name, port)] for every plugged-in device that matches a board."""
    found = []
    entries = sorted(by_id.iterdir()) if by_id.exists() else []
    for name, cfg in boards().items():
        for entry in entries:
            if any(m in entry.name for m in cfg["usb_match"]):
                found.append((name, str(entry.resolve())))
    return found


def resolve(board=None, by_id=BY_ID):
    """(board name, port). Raises PortError with a helpful message."""
    board = board or os.environ.get("HELIOSTAT_BOARD")
    if board and board not in boards():
        raise PortError(f"unknown board {board!r}; known: {', '.join(boards())}")
    port = os.environ.get("HELIOSTAT_PORT")
    found = connected(by_id)
    if port:
        if board is None:
            matches = [name for name, p in found if p == port]
            board = matches[0] if len(matches) == 1 else None
        return board, port
    if board:
        ports = [p for name, p in found if name == board]
        if not ports:
            raise PortError(f"no {board} plugged in")
        return board, ports[0]
    if not found:
        raise PortError("no heliostat board plugged in (looked in /dev/serial/by-id)")
    if len({name for name, _ in found}) > 1:
        listed = ", ".join(f"{name} on {p}" for name, p in found)
        raise PortError(f"several boards plugged in ({listed}); pick one with --board")
    return found[0]


def default_port():
    """The port alone, for tools with a --port option; falls back to /dev/ttyUSB0."""
    try:
        return resolve()[1]
    except PortError:
        return "/dev/ttyUSB0"
