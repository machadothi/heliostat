"""Time source for the tracker, with validity tracking and a simulation rate.

The ESP32 has no battery-backed clock, so on every boot it believes it is some
time in 2000 until the phone tells it otherwise. That matters more here than in
most projects: WITHOUT A VALID TIME THERE IS NO SUN VECTOR, and a heliostat that
guesses would aim a concentrated beam at an arbitrary part of the sky. The
supervisor refuses to track until `is_valid()` returns True.

`rate` exists for the simulator. At rate=600 a full solar day plays out in 2.4
minutes, which is what makes the whole tracking loop -- including the dawn and
dusk stow transitions -- testable in an afternoon with no mechanics attached.

The monotonic source is injected rather than imported so this module stays free
of hardware and runs unchanged under pytest.
"""


class Clock:
    def __init__(self, monotonic, epoch=None, rate=1.0):
        """`monotonic` is a callable returning seconds that only ever increase."""
        self._monotonic = monotonic
        self._rate = rate
        self._epoch = epoch
        self._set_at = monotonic() if epoch is not None else None

    def set_epoch(self, epoch, rate=None):
        """Adopt a wall-clock time, normally pushed from the phone."""
        self._epoch = epoch
        self._set_at = self._monotonic()
        if rate is not None:
            self._rate = rate

    def now(self):
        """Current Unix epoch seconds, or None if time has never been set.

        Whole seconds, deliberately. On the ESP32 floats are single precision,
        and a Unix time of ~1.8e9 as a float resolves only 128 s -- the sun moves
        half a degree in that. An int epoch plus an int elapsed stays exact
        (MicroPython ints are arbitrary precision), and solar.julian then splits
        it without loss. One second is 0.004 degrees of sun motion.
        """
        if self._epoch is None:
            return None
        return self._epoch + int((self._monotonic() - self._set_at) * self._rate)

    def is_valid(self, max_age_s=None):
        """Has time been set, and recently enough to still be trusted?

        The ESP32's oscillator drifts, so a sync from three weeks ago is worth
        less than one from this morning. `max_age_s` of None means never expire.
        """
        if self._epoch is None:
            return False
        if max_age_s is None:
            return True
        return self.age() <= max_age_s

    def age(self):
        """Seconds of REAL time since the last sync, ignoring the rate.

        Deliberately not scaled: staleness is about how long the oscillator has
        been drifting, which has nothing to do with how fast simulated time runs.
        """
        if self._set_at is None:
            return None
        return self._monotonic() - self._set_at

    @property
    def rate(self):
        return self._rate

    @rate.setter
    def rate(self, value):
        # Re-anchor first, or every past second would be retroactively rescaled.
        current = self.now()
        if current is not None:
            self._epoch = current
            self._set_at = self._monotonic()
        self._rate = value

    @property
    def simulated(self):
        return self._rate != 1.0
