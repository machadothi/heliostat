"""BMM150 magnetometer, behind the BMI270's auxiliary I2C master (AtomS3R).

Used for ONE job: calibrating which way true north is for the yaw encoder
(tools/calibrate_azimuth.py), with the Atom riding the mirror. It is never used
for pointing in operation -- a few degrees of magnetic error next to servo
motors is fine for a calibration that is checked against the sun, not for
aiming a beam.

The BMM150 is not on the main bus: the BMI270 talks to it. In "manual" aux
mode (Bosch BMI270_SensorAPI, set_aux_interface):

    write:  AUX_WR_DATA = value, then AUX_WR_ADDR = register  -> the BMI270 sends it
    read:   AUX_IF_CONF manual burst length (bits 3:2 -- NOT the automatic
            mode's bits 1:0), then AUX_RD_ADDR = register
            -> the BMI270 fetches 1/2/6/8 bytes into DATA_0..7 (0x04..0x0B)

and STATUS.aux_busy is polled between operations.

Raw values are turned into microtesla with Bosch's floating-point compensation
(BMM150_SensorAPI bmm150.c: compensate_x/y/z), using the per-chip trim
registers read once at start-up. Bosch's code is BSD-3-Clause, (c) Bosch
Sensortec GmbH; the formulas below follow it term for term.
"""

from hal import imu_bmi270 as bmi

AUX_ADDRESS = 0x10  # the BMM150's address on the BMI270's aux bus
CHIP_ID = 0x32

REG_CHIP_ID = 0x40
REG_DATA_X_LSB = 0x42  # 8 bytes: X, Y, Z, RHALL
REG_POWER_CONTROL = 0x4B
REG_OP_MODE = 0x4C
REG_REP_XY = 0x51
REG_REP_Z = 0x52

# Bosch's "enhanced" preset: 15 XY / 14 Z repetitions.
REP_XY_ENHANCED = 0x07
REP_Z_ENHANCED = 0x0D
# Normal mode, 30 Hz output data rate (bits 5:3 = 0b111).
OP_NORMAL_30HZ = 0x38

OVERFLOW_XY = -4096
OVERFLOW_Z = -16384

_BURST_CODE = {1: 0, 2: 1, 6: 2, 8: 3}
_AUX_MANUAL = 0x80
_IF_CONF_AUX_EN = 0x20


def _s8(b):
    return b - 256 if b & 0x80 else b


def _s16(lsb, msb):
    v = lsb | (msb << 8)
    return v - 65536 if v & 0x8000 else v


def parse_trim(x1y1, z4x2y2, rest):
    """The trim registers, from the three reads Bosch's read_trim_registers makes:
    0x5D-0x5E (2 bytes), 0x62-0x65 (4), 0x68-0x71 (10)."""
    return {
        "x1": _s8(x1y1[0]),
        "y1": _s8(x1y1[1]),
        "z4": _s16(z4x2y2[0], z4x2y2[1]),
        "x2": _s8(z4x2y2[2]),
        "y2": _s8(z4x2y2[3]),
        "z2": _s16(rest[0], rest[1]),
        "z1": rest[2] | (rest[3] << 8),
        "xyz1": rest[4] | ((rest[5] & 0x7F) << 8),
        "z3": _s16(rest[6], rest[7]),
        "xy2": _s8(rest[8]),
        "xy1": rest[9],
    }


def parse_raw(data):
    """8 bytes from DATA_X_LSB -> (raw_x, raw_y, raw_z, rhall), as bmm150_read_mag_data."""
    x = _s8(data[1]) * 32 | (data[0] >> 3)
    y = _s8(data[3]) * 32 | (data[2] >> 3)
    z = _s8(data[5]) * 128 | (data[4] >> 1)
    rhall = (data[7] << 6) | (data[6] >> 2)
    return x, y, z, rhall


def _compensate_xy(raw, rhall, t, d1, d2):
    if raw == OVERFLOW_XY or rhall == 0 or t["xyz1"] == 0:
        return None
    r = t["xyz1"] * 16384.0 / rhall - 16384.0
    c1 = t["xy2"] * (r * r / 268435456.0)
    c2 = c1 + r * t["xy1"] / 16384.0
    c3 = d2 + 160.0
    c4 = raw * ((c2 + 256.0) * c3)
    return ((c4 / 8192.0) + (d1 * 8.0)) / 16.0


def compensate(raw_x, raw_y, raw_z, rhall, t):
    """Raw counts -> (x, y, z) in microtesla, or None on overflow."""
    x = _compensate_xy(raw_x, rhall, t, t["x1"], t["x2"])
    y = _compensate_xy(raw_y, rhall, t, t["y1"], t["y2"])
    if raw_z == OVERFLOW_Z or t["z2"] == 0 or t["z1"] == 0 or t["xyz1"] == 0 or rhall == 0:
        z = None
    else:
        z0 = raw_z - t["z4"]
        z1 = rhall - t["xyz1"]
        z2 = t["z3"] * z1
        z3 = t["z1"] * rhall / 32768.0
        z4 = t["z2"] + z3
        z5 = (z0 * 131072.0) - z2
        z = (z5 / (z4 * 4.0)) / 16.0
    if x is None or y is None or z is None:
        return None
    return x, y, z


class InitError(Exception):
    pass


class Magnetometer:
    def __init__(self, imu):
        """`imu` is an initialised hal.imu_bmi270.IMU."""
        self.imu = imu
        pwr = imu.read_reg(bmi.REG_PWR_CTRL)
        imu.write_reg(bmi.REG_PWR_CTRL, pwr & ~bmi.PWR_CTRL_AUX_EN)  # configure with aux off
        imu.write_reg(bmi.REG_IF_CONF, imu.read_reg(bmi.REG_IF_CONF) | _IF_CONF_AUX_EN)
        imu.write_reg(bmi.REG_AUX_DEV_ID, AUX_ADDRESS << 1)
        imu.write_reg(bmi.REG_AUX_IF_CONF, _AUX_MANUAL | (_BURST_CODE[8] << 2))
        imu.write_reg(bmi.REG_PWR_CTRL, pwr | bmi.PWR_CTRL_AUX_EN)
        imu.sleep_ms(10)

        self._write(REG_POWER_CONTROL, 0x01)  # leave suspend
        imu.sleep_ms(4)  # 3 ms start-up
        self.chip_id = self._read(REG_CHIP_ID, 1)[0]
        if self.chip_id != CHIP_ID:
            raise InitError("no BMM150 on the aux bus (chip id 0x{:02X})".format(self.chip_id))
        self.trim = parse_trim(self._read(0x5D, 2), self._read(0x62, 6)[:4],
                               self._read(0x68, 8) + self._read(0x70, 2))
        self._write(REG_REP_XY, REP_XY_ENHANCED)
        self._write(REG_REP_Z, REP_Z_ENHANCED)
        self._write(REG_OP_MODE, OP_NORMAL_30HZ)
        imu.sleep_ms(40)

    def read_ut(self):
        """(x, y, z) in microtesla, or None if the reading overflowed."""
        return compensate(*parse_raw(self._read(REG_DATA_X_LSB, 8)), self.trim)

    # -- the aux bus, in manual mode ------------------------------------------

    def _wait(self):
        for _ in range(50):
            if not self.imu.read_reg(bmi.REG_STATUS) & bmi.STATUS_AUX_BUSY:
                return
            self.imu.sleep_ms(1)
        raise InitError("BMI270 aux bus stuck busy")

    def _write(self, register, value):
        self.imu.write_reg(bmi.REG_AUX_WR_DATA, value)
        self.imu.write_reg(bmi.REG_AUX_WR_ADDR, register)
        self._wait()
        self.imu.sleep_ms(2)

    def _read(self, register, n):
        self.imu.write_reg(bmi.REG_AUX_IF_CONF, _AUX_MANUAL | (_BURST_CODE[n] << 2))
        self.imu.write_reg(bmi.REG_AUX_RD_ADDR, register)
        self._wait()
        return self.imu.read_regs(bmi.REG_AUX_DATA, n)
