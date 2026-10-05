"""Level-filtered logging that never touches flash.

MicroPython ships `logging` only as a pip-installable extra, and the standard
one writes wherever its handlers point. This is deliberately smaller: print to
the console, and keep the last few lines in RAM so `GET /api/status` can return
them when something has gone wrong and nobody is holding a serial cable.

NOTHING HERE WRITES TO FLASH. See util/ringbuf for why.
"""

from util.ringbuf import RingBuffer

DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40

_NAMES = {DEBUG: "DEBUG", INFO: "INFO", WARNING: "WARN", ERROR: "ERROR"}
_LEVELS = {"debug": DEBUG, "info": INFO, "warning": WARNING, "error": ERROR}

HISTORY_LINES = 40

_level = INFO
_history = RingBuffer(HISTORY_LINES)
_clock = None


def configure(level="info", clock=None):
    """Set the threshold and, optionally, a callable returning epoch seconds.

    The clock is injected rather than imported so the logger works before time
    has been synchronised and inside the simulator, where time runs fast.
    """
    global _level, _clock
    _level = _LEVELS.get(level, INFO)
    _clock = clock


def stamp(epoch):
    """'13:05:42Z' from Unix seconds, in integer arithmetic.

    Never through a float: on the ESP32 a float holds 1.8e9 only to 128 s, and
    the log's timestamps used to advance in exactly those steps.
    """
    seconds = int(epoch) % 86400
    return "{:02d}:{:02d}:{:02d}Z".format(seconds // 3600, seconds // 60 % 60, seconds % 60)


def log(level, message, keep=True):
    """Print a line; `keep=False` leaves it out of the history the API returns,
    for routine lines (the memory report) that would push real events out."""
    if level < _level:
        return
    prefix = ""
    if _clock is not None:
        try:
            now = _clock()
            if now is not None:
                prefix = stamp(now) + " "
        except Exception:
            prefix = ""
    line = "{}{}: {}".format(prefix, _NAMES.get(level, "?"), message)
    if keep:
        _history.append(line)
    print(line)


def debug(message):
    log(DEBUG, message)


def info(message, keep=True):
    log(INFO, message, keep)


def warning(message):
    log(WARNING, message)


def error(message):
    log(ERROR, message)


def history():
    """The most recent lines, oldest first. Exposed over the API."""
    return _history.to_list()


def clear():
    _history.clear()
