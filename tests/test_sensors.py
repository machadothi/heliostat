"""Sensor maths, checked against the manufacturers' own worked examples."""

import math

import pytest
from hal.baro_bmp180 import compensate, parse_calibration
from hal.imu_mpu6050 import IMU, accel_from_bytes, magnitude, tilt_between, to_signed16

# --- BMP180: Bosch datasheet, section 3.5, "calculating pressure and temperature" ---

DATASHEET_CAL = [408, -72, -14383, 32741, 32757, 23153, 6190, 4, -32768, -8711, 2868]


def test_bmp180_matches_the_datasheet_worked_example():
    temperature, pressure = compensate(DATASHEET_CAL, ut=27898, up=23843, oss=0)
    assert temperature == 150  # 15.0 degC
    assert pressure == 69964  # Pa


def test_bmp180_calibration_words_are_signed_where_the_datasheet_says():
    raw = bytearray()
    for word in DATASHEET_CAL:
        raw += (word & 0xFFFF).to_bytes(2, "big")
    assert parse_calibration(bytes(raw)) == DATASHEET_CAL


# --- MPU6050 -------------------------------------------------------------------------


def test_signed_16_bit():
    assert to_signed16(0x7F, 0xFF) == 32767
    assert to_signed16(0x80, 0x00) == -32768
    assert to_signed16(0xFF, 0xFF) == -1


def test_one_g_on_z_at_the_2g_range():
    x, y, z = accel_from_bytes(bytes([0, 0, 0, 0, 0x40, 0x00]))
    assert (x, y, z) == (0.0, 0.0, 1.0)


def test_tilt_is_the_angle_between_gravity_vectors():
    level = (0.0, 0.0, 1.0)
    ten = (math.sin(math.radians(10)), 0.0, math.cos(math.radians(10)))
    assert tilt_between(level, level) == pytest.approx(0.0, abs=1e-9)
    assert tilt_between(level, ten) == pytest.approx(10.0, abs=1e-9)
    # Scale must not matter: a shock changes the magnitude, not the attitude.
    assert tilt_between(level, tuple(2 * v for v in ten)) == pytest.approx(10.0, abs=1e-9)


def test_magnitude_at_rest_is_one_g():
    assert magnitude((0.6, 0.0, 0.8)) == pytest.approx(1.0)


class FakeI2C:
    """An MPU6050 lying flat, then tipped by `tilt_deg` about the X axis."""

    def __init__(self):
        self.tilt_deg = 0.0
        self.writes = []

    def readfrom_mem(self, address, register, n):
        if register == 0x75:
            return bytes([0x68])
        t = math.radians(self.tilt_deg)
        raw = [0, int(16384 * math.sin(t)), int(16384 * math.cos(t))]
        out = bytearray()
        for value in raw:
            out += (value & 0xFFFF).to_bytes(2, "big")
        return bytes(out)

    def writeto_mem(self, address, register, data):
        self.writes.append((register, data[0]))


def test_imu_wakes_the_chip_and_reads_tilt_from_boot_attitude():
    bus = FakeI2C()
    imu = IMU(bus)
    assert (0x6B, 0x00) in bus.writes  # woken from sleep
    assert not imu.calibrated
    tilt, accel_g = imu.read()
    assert tilt == pytest.approx(0.0, abs=0.01)
    assert accel_g == pytest.approx(1.0, abs=0.001)

    bus.tilt_deg = 7.0
    tilt, _ = imu.read()
    assert tilt == pytest.approx(7.0, abs=0.01)


def test_a_calibrated_level_is_used_instead_of_boot_attitude():
    bus = FakeI2C()
    bus.tilt_deg = 3.0  # installed slightly crooked
    imu = IMU(bus, level=(0.0, 0.0, 1.0))
    assert imu.calibrated
    tilt, _ = imu.read()
    assert tilt == pytest.approx(3.0, abs=0.01)
