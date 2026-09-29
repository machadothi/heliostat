"""A monotonic seconds counter that survives MicroPython's tick wraparound.

`time.ticks_ms()` wraps -- after about 12.4 days on the ESP32 -- and a naive
subtraction across the wrap goes hugely negative. A heliostat is meant to run
unattended for months, so every elapsed-time calculation in the firmware would
eventually break on day 12 unless it goes through `ticks_diff`.

This accumulates `ticks_diff` into an ever-increasing float, so the rest of the
code can treat time as a plain number. On CPython it falls back to
`time.monotonic`, which lets the same modules run under pytest.

`monotonic()` must be called at least once per wrap period to keep counting
correctly. The control loop calls it every second, so that is never in doubt.
"""

import time

try:
    _ticks_ms = time.ticks_ms
    _ticks_diff = time.ticks_diff
except AttributeError:  # CPython
    _ticks_ms = None


if _ticks_ms is None:
    monotonic = time.monotonic
else:
    _last_ticks = _ticks_ms()
    _elapsed_ms = 0

    def monotonic():
        global _last_ticks, _elapsed_ms
        now = _ticks_ms()
        _elapsed_ms += _ticks_diff(now, _last_ticks)
        _last_ticks = now
        return _elapsed_ms / 1000.0
