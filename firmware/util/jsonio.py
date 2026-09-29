"""Atomic JSON persistence.

A half-written config is worse than no config: the board comes up with a file
that parses partly, or not at all, and the failure happens at the next power cut
rather than while anyone is watching. So every write goes to a temporary file
first and is then renamed into place, which is the closest thing to atomic that
a small filesystem offers.

Writes must be RARE. See util/ringbuf for the flash-endurance arithmetic.
"""

import json
import os

TEMP_SUFFIX = ".tmp"


def read_json(path, default=None):
    """Parse a JSON file, returning `default` if it is missing or corrupt.

    A corrupt config should not brick the machine -- falling back to defaults
    and saying so in the log leaves it recoverable over the network.
    """
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def atomic_write_json(path, data):
    """Write JSON via a temporary file and a rename."""
    temp = path + TEMP_SUFFIX
    with open(temp, "w") as handle:
        json.dump(data, handle)

    # MicroPython's os.rename will not silently replace an existing file on
    # every filesystem it supports, so clear the way first. The window between
    # the remove and the rename is the one moment a power cut loses the file --
    # but the temporary copy is already complete on disk, so it is recoverable.
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(temp, path)


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def deep_merge(base, overlay):
    """Recursively merge `overlay` onto a copy of `base`.

    This is what lets a firmware update add a config field without wiping the
    operator's stored values: defaults supply anything the saved file predates,
    and the saved file wins wherever it has an opinion.

    Lists are replaced wholesale, not merged. Merging a keep-out zone list
    element-wise would produce zones nobody wrote.
    """
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
