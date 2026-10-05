"""Tilt and shock from an accelerometer: shared by every IMU driver.

A driver supplies `accel() -> (x, y, z)` in g; this supplies the rest of the
interface the tracker's supervisor uses (`read`, `calibrate_level`,
`calibrated`). So the MPU6050 on the devkit and the BMI270 on the AtomS3R
behave identically, and the supervisor never knows which one it has.

  * TILT: the angle between the current gravity vector and the reference
    attitude -- the calibrated "level" (`imu.level` in config, set by POST
    /api/imu/level after installation), or the attitude at boot until then.
  * SHOCK: the magnitude of acceleration in g; 1.0 at rest.
"""

import math


def magnitude(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def tilt_between(reference, current):
    """Angle in degrees between two gravity vectors. 0 means unchanged attitude."""
    ref_len, cur_len = magnitude(reference), magnitude(current)
    if ref_len < 1e-6 or cur_len < 1e-6:
        return 0.0
    dot = sum(r * c for r, c in zip(reference, current)) / (ref_len * cur_len)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


class GravityLevel:
    """Mixin: the level/tilt/shock logic over a driver's `accel()`."""

    def _init_reference(self, level):
        self.calibrated = level is not None
        self.reference = tuple(level) if level else self.average(20)

    def average(self, samples=50):
        """Mean gravity vector over a burst of readings: for calibrating level."""
        sx = sy = sz = 0.0
        for _ in range(samples):
            x, y, z = self.accel()
            sx, sy, sz = sx + x, sy + y, sz + z
        return (sx / samples, sy / samples, sz / samples)

    def read(self):
        """(tilt_deg, accel_g): the pair the tracker's supervisor consumes."""
        a = self.accel()
        return tilt_between(self.reference, a), magnitude(a)

    def calibrate_level(self):
        """Adopt the current attitude as level. Returns the vector, for config."""
        self.reference = self.average(100)
        self.calibrated = True
        return self.reference
