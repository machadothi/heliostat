"""Simulated limit switches and E-stop input.

Mirrors the fail-safe wiring of the real ones: a switch reports `triggered()`
True when its contact is OPEN. On hardware that is also what a cut wire looks
like, which is the point of wiring them normally-closed.
"""


class SimSwitch:
    """A switch that trips when a simulated axis passes a mechanical angle.

    `forced` overrides the position logic -- set it True to simulate an
    unplugged switch, which must fault the machine even while it sits idle.
    """

    def __init__(self, name, axis=None, trip_below_deg=None, trip_above_deg=None):
        self.name = name
        self._axis = axis
        self._below = trip_below_deg
        self._above = trip_above_deg
        self.forced = None

    def triggered(self):
        if self.forced is not None:
            return self.forced
        if self._axis is None:
            return False
        position = self._axis.true_mech_deg()
        if self._below is not None and position < self._below:
            return True
        return self._above is not None and position > self._above


class SimEstop:
    """The E-stop sense input. On hardware the relay has already cut servo power."""

    def __init__(self):
        self.pressed = False

    def active(self):
        return self.pressed
