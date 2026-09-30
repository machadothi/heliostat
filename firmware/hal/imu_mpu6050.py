"""MPU6050, used as a level and a shock detector -- accelerometer only.

What it answers:
  * TILT: how far the base has rotated from its calibrated, installed attitude.
    A base that has moved invalidates every calibration, so the supervisor
    defocuses and stows past a few degrees.
  * SHOCK: the magnitude of acceleration in g. At rest it reads gravity alone,
    1.0 g; a knock or a gust shaking the frame pushes it higher.

What it deliberately does NOT answer: heading. Without a magnetometer yaw
drifts, and pointing comes from the servo encoders anyway.

The reference attitude is the calibrated "level" vector stored in config
(`imu.level`, set by POST /api/imu/level after installation). Until that
exists, the attitude at boot is used, so the rule still catches the base being
knocked over -- just not a base installed crooked, which is a calibration job.

The maths lives in plain functions so it runs under pytest; only IMU talks to
the I2C bus, and even that is handed a bus rather than creating one.
"""

import math

ADDRESS = 0x68

_PWR_MGMT_1 = 0x6B
_CONFIG = 0x1A
_ACCEL_CONFIG = 0x1C
_ACCEL_XOUT_H = 0x3B
_WHO_AM_I = 0x75

_LSB_PER_G = 16384.0  # at the +/-2 g range

# Digital low-pass filter setting 6 (~5 Hz). Tilt is slow; this keeps servo
# vibration and the odd bump from reading as a tilt.
_DLPF_5HZ = 0x06


def to_signed16(high, low):
    value = (high << 8) | low
    return value - 65536 if value & 0x8000 else value


def accel_from_bytes(data):
    """Six raw bytes from ACCEL_XOUT_H onward -> (x, y, z) in g."""
    return tuple(to_signed16(data[i], data[i + 1]) / _LSB_PER_G for i in (0, 2, 4))


def magnitude(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def tilt_between(reference, current):
    """Angle in degrees between two gravity vectors. 0 means unchanged attitude."""
    ref_len, cur_len = magnitude(reference), magnitude(current)
    if ref_len < 1e-6 or cur_len < 1e-6:
        return 0.0
    dot = sum(r * c for r, c in zip(reference, current)) / (ref_len * cur_len)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


class IMU:
    def __init__(self, i2c, level=None, address=ADDRESS):
        self.i2c = i2c
        self.address = address
        self.who_am_i = self._read(_WHO_AM_I, 1)[0]
        self._write(_PWR_MGMT_1, 0x00)  # wake from sleep, internal oscillator
        self._write(_CONFIG, _DLPF_5HZ)
        self._write(_ACCEL_CONFIG, 0x00)  # +/-2 g
        self.calibrated = level is not None
        self.reference = tuple(level) if level else self.average(20)

    def accel(self):
        """(x, y, z) in g."""
        return accel_from_bytes(self._read(_ACCEL_XOUT_H, 6))

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

    def _read(self, register, n):
        return self.i2c.readfrom_mem(self.address, register, n)

    def _write(self, register, value):
        self.i2c.writeto_mem(self.address, register, bytes((value,)))
