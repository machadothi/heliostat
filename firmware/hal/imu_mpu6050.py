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

from hal.attitude import GravityLevel, magnitude, tilt_between  # noqa: F401 (re-exported)

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


class IMU(GravityLevel):
    def __init__(self, i2c, level=None, address=ADDRESS):
        self.i2c = i2c
        self.address = address
        self.who_am_i = self._read(_WHO_AM_I, 1)[0]
        self._write(_PWR_MGMT_1, 0x00)  # wake from sleep, internal oscillator
        self._write(_CONFIG, _DLPF_5HZ)
        self._write(_ACCEL_CONFIG, 0x00)  # +/-2 g
        self._init_reference(level)

    def accel(self):
        """(x, y, z) in g."""
        return accel_from_bytes(self._read(_ACCEL_XOUT_H, 6))

    def _read(self, register, n):
        return self.i2c.readfrom_mem(self.address, register, n)

    def _write(self, register, value):
        self.i2c.writeto_mem(self.address, register, bytes((value,)))
