"""Simulated environmental sensors: IMU, barometer, anemometer.

Each has settable values so a test can drive the matching safety rule, and a
`read()` with the same signature as the real driver it stands in for.
"""


class SimIMU:
    """MPU6050 stand-in. Tilt is relative to the calibrated level, in degrees."""

    def __init__(self):
        self.tilt_deg = 0.0
        self.accel_g = 1.0  # at rest the accelerometer reads gravity alone

    def read(self):
        return self.tilt_deg, self.accel_g

    def accel(self):
        """The gravity vector: tilted about X by tilt_deg, scaled to accel_g."""
        import math

        t = math.radians(self.tilt_deg)
        return (0.0, self.accel_g * math.sin(t), self.accel_g * math.cos(t))


class SimBaro:
    """BMP180 stand-in, with an optional pressure ramp to simulate a front."""

    def __init__(self, monotonic, pressure_hpa=1013.25, temp_c=20.0):
        self._monotonic = monotonic
        self._base = pressure_hpa
        self._ramp_start = monotonic()
        self.hpa_per_hour = 0.0
        self.temp_c = temp_c

    def ramp(self, hpa_per_hour):
        """Start pressure changing at this rate from its current value."""
        self._base = self.read()[0]
        self._ramp_start = self._monotonic()
        self.hpa_per_hour = hpa_per_hour

    def read(self):
        hours = (self._monotonic() - self._ramp_start) / 3600.0
        return self._base + self.hpa_per_hour * hours, self.temp_c


class SimWind:
    """Anemometer stand-in. None means no anemometer is fitted."""

    def __init__(self, kph=0.0):
        self.kph = kph

    def read(self):
        return self.kph
