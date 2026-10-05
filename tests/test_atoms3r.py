"""AtomS3R support: the board profile, the BMI270 and the BMM150 behind it.

The BMM150 vectors were read from the real AtomS3R (trim registers and a raw
sample), with the board's own float32 result alongside; the unpacking and the
X compensation were also checked by hand against Bosch's bmm150.c.
"""

import pytest
from hal import profiles
from hal import imu_bmi270 as bmi
from hal import mag_bmm150 as mag
from hal.sensors import identify_imu

# --- board profile ------------------------------------------------------------


def test_profiles_share_one_set_of_keys():
    keys = [set(p) for p in profiles.PROFILES.values()]
    assert all(k == keys[0] for k in keys)


def test_the_atom_avoids_its_strapping_and_internal_pins_for_io():
    atom = profiles.PROFILES["atoms3r"]
    io = [atom[k] for k in ("servo_tx", "servo_rx", "limit_az", "limit_el", "estop", "anemometer")]
    assert not set(io) & {0, 3, 45, 46}  # S3 strapping pins (0 and 45 are the internal I2C)
    assert len(set(io)) == len(io)


def test_off_board_the_devkit_profile_is_used_and_nothing_mismatches(monkeypatch):
    monkeypatch.setattr(profiles, "chip", lambda: None)
    assert profiles.name() == "esp32_devkit"
    assert profiles.mismatch() is None


def test_a_build_on_the_wrong_chip_is_caught(monkeypatch):
    monkeypatch.setattr(profiles, "chip", lambda: "esp32s3")
    monkeypatch.setattr(profiles, "name", lambda: "esp32_devkit")
    assert "esp32s3" in profiles.mismatch()


# --- telling the two IMUs at 0x68 apart -----------------------------------------


class RegBus:
    def __init__(self, regs):
        self.regs = regs

    def readfrom_mem(self, addr, reg, n):
        return bytes(self.regs.get(reg + i, 0) for i in range(n))


def test_imus_are_identified_by_chip_id_not_address():
    assert identify_imu(RegBus({0x00: 0x24})) == "bmi270"
    assert identify_imu(RegBus({0x75: 0x68})) == "mpu6050"
    assert identify_imu(RegBus({})) is None


# --- BMI270 ---------------------------------------------------------------------------


class FakeBMI270:
    """Records register writes; reports init ok and data ready."""

    def __init__(self, accel=b"\x00\x00\x00\x00\x00\x40"):  # z = +1 g
        self.writes = []
        self.accel = accel

    def readfrom_mem(self, addr, reg, n):
        if reg == bmi.REG_CHIP_ID:
            return bytes((bmi.CHIP_ID,))
        if reg == bmi.REG_INTERNAL_STATUS:
            return bytes((bmi.INIT_OK,))
        if reg == bmi.REG_STATUS:
            return bytes((bmi.STATUS_DRDY_ACC,))
        if reg == bmi.REG_ACC_X_LSB:
            return self.accel[:n]
        if reg == bmi.REG_PWR_CTRL:
            return b"\x00"
        return bytes(n)

    def writeto_mem(self, addr, reg, data):
        self.writes.append((reg, bytes(data)))


def test_bmi270_init_uploads_the_whole_config_then_enables_before_configuring():
    from hal.bmi270_config import CONFIG

    bus = FakeBMI270()
    imu = bmi.IMU(bus, sleep_ms=lambda ms: None)
    uploaded = b"".join(d for r, d in bus.writes if r == bmi.REG_INIT_DATA)
    assert uploaded == CONFIG and len(CONFIG) == 8192
    # Each chunk is addressed in 2-byte words, split low nibble / high bits.
    addrs = [d for r, d in bus.writes if r == bmi.REG_INIT_ADDR_0]
    assert addrs[1] == bytes(((bmi.UPLOAD_CHUNK // 2) & 0x0F, (bmi.UPLOAD_CHUNK // 2) >> 4))
    order = [r for r, _ in bus.writes]
    # Found on the hardware: configured before being enabled, it never starts.
    assert order.index(bmi.REG_PWR_CTRL) < order.index(bmi.REG_ACC_CONF)
    assert imu.accel() == pytest.approx((0.0, 0.0, 1.0))
    assert imu.read() == pytest.approx((0.0, 1.0))


def test_bmi270_refuses_a_rejected_upload():
    class Rejecting(FakeBMI270):
        def readfrom_mem(self, addr, reg, n):
            if reg == bmi.REG_INTERNAL_STATUS:
                return b"\x02"  # init_err
            return super().readfrom_mem(addr, reg, n)

    with pytest.raises(bmi.InitError, match="not accepted"):
        bmi.IMU(Rejecting(), sleep_ms=lambda ms: None)


def test_bmi270_accel_is_little_endian_signed():
    assert bmi.accel_from_bytes(bytes.fromhex("d8f0bd3d73fd")) == pytest.approx(
        (-3880 / 16384, 15805 / 16384, -653 / 16384))


# --- BMM150 ---------------------------------------------------------------------------

TRIM = mag.parse_trim(bytes.fromhex("0000"), bytes.fromhex("00001b1b"),
                      bytes.fromhex("fd02b554451b0000fd1d"))


def test_trim_registers_parse_like_bosch():
    assert TRIM == {"x1": 0, "y1": 0, "z4": 0, "x2": 27, "y2": 27, "z2": 765, "z1": 21685,
                    "xyz1": 6981, "z3": 0, "xy2": -3, "xy1": 29}


def test_raw_unpacking_keeps_the_sign():
    assert mag.parse_raw(bytes.fromhex("c106e905f5ff4164")) == (216, 189, -6, 6416)


def test_compensation_matches_the_board():
    x, y, z = mag.compensate(216, 189, -6, 6416, TRIM)
    assert (x, y, z) == pytest.approx((79.67045, 69.71164, -2.452235), abs=1e-3)


def test_overflow_reads_as_none():
    assert mag.compensate(mag.OVERFLOW_XY, 189, -6, 6416, TRIM) is None
    assert mag.compensate(216, 189, mag.OVERFLOW_Z, 6416, TRIM) is None
