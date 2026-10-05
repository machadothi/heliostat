"""BMI270 (the AtomS3R's IMU), as a level and shock detector -- accelerometer only.

Same job and interface as hal/imu_mpu6050.py (see hal/attitude.py): the
supervisor's tilt and shock rules cannot tell which one they have.

Unlike the MPU6050, the BMI270 does nothing until Bosch's 8 KB configuration
file has been uploaded into it, at every power-up (hal/bmi270_config.py). The
sequence follows Bosch's BMI270_SensorAPI (bmi2.c, bmi270.c):

    soft reset -> advanced power save off -> INIT_CTRL 0 -> burst-write the file
    into INIT_DATA, addressing each chunk through INIT_ADDR_0/1 (in 2-byte words)
    -> INIT_CTRL 1 -> INTERNAL_STATUS must report "init ok"

Its auxiliary I2C master, which carries the BMM150 magnetometer on the
AtomS3R, is driven from hal/mag_bmm150.py through this object.
"""

from hal.attitude import GravityLevel

ADDRESS = 0x68
CHIP_ID = 0x24

REG_CHIP_ID = 0x00
REG_STATUS = 0x03
REG_AUX_DATA = 0x04  # 8 bytes: what the aux master last read
REG_ACC_X_LSB = 0x0C
REG_INTERNAL_STATUS = 0x21
REG_ACC_CONF = 0x40
REG_ACC_RANGE = 0x41
REG_AUX_DEV_ID = 0x4B
REG_AUX_IF_CONF = 0x4C
REG_AUX_RD_ADDR = 0x4D
REG_AUX_WR_ADDR = 0x4E
REG_AUX_WR_DATA = 0x4F
REG_INIT_CTRL = 0x59
REG_INIT_ADDR_0 = 0x5B
REG_INIT_DATA = 0x5E
REG_IF_CONF = 0x6B
REG_PWR_CONF = 0x7C
REG_PWR_CTRL = 0x7D
REG_CMD = 0x7E

SOFT_RESET = 0xB6
PWR_CTRL_AUX_EN = 0x01
PWR_CTRL_ACC_EN = 0x04
STATUS_AUX_BUSY = 0x04
STATUS_DRDY_ACC = 0x80
INIT_OK = 0x01

# 25 Hz, normal averaging, performance mode: ~10 Hz bandwidth. Measured on the
# AtomS3R at rest: 0.45 mg noise, the quietest of 12.5-100 Hz.
ACC_CONF_25HZ = 0xA6
ACC_RANGE_2G = 0x00
LSB_PER_G = 16384.0

UPLOAD_CHUNK = 128


def _default_sleep_ms(ms):
    import time

    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000)


def accel_from_bytes(data):
    """Six bytes from ACC_X_LSB onward (little-endian int16) -> (x, y, z) in g."""
    out = []
    for i in (0, 2, 4):
        value = data[i] | (data[i + 1] << 8)
        out.append((value - 65536 if value & 0x8000 else value) / LSB_PER_G)
    return tuple(out)


class InitError(Exception):
    pass


class IMU(GravityLevel):
    def __init__(self, i2c, level=None, address=ADDRESS, sleep_ms=_default_sleep_ms):
        from hal.bmi270_config import CONFIG

        self.i2c = i2c
        self.address = address
        self.sleep_ms = sleep_ms
        self.chip_id = self.read_reg(REG_CHIP_ID)
        if self.chip_id != CHIP_ID:
            raise InitError("not a BMI270 (chip id 0x{:02X})".format(self.chip_id))

        self.write_reg(REG_CMD, SOFT_RESET)
        self.sleep_ms(2)
        self.write_reg(REG_PWR_CONF, 0x00)  # advanced power save off: needed to upload
        self.sleep_ms(1)
        self.write_reg(REG_INIT_CTRL, 0x00)
        for index in range(0, len(CONFIG), UPLOAD_CHUNK):
            word = index // 2
            self.write_regs(REG_INIT_ADDR_0, bytes((word & 0x0F, word >> 4)))
            self.write_regs(REG_INIT_DATA, CONFIG[index:index + UPLOAD_CHUNK])
        self.write_reg(REG_INIT_CTRL, 0x01)

        status = 0
        for _ in range(30):  # Bosch allows up to 20 ms; be generous
            self.sleep_ms(5)
            status = self.read_reg(REG_INTERNAL_STATUS) & 0x0F
            if status == INIT_OK:
                break
        else:
            raise InitError("config upload not accepted (INTERNAL_STATUS 0x{:02X})".format(status))

        # Enable FIRST, then configure. Configured before being enabled (found on
        # the AtomS3R), the accelerometer never starts and reads zeros forever.
        self.write_reg(REG_PWR_CTRL, self.read_reg(REG_PWR_CTRL) | PWR_CTRL_ACC_EN)
        self.sleep_ms(2)
        self.write_reg(REG_ACC_CONF, ACC_CONF_25HZ)
        self.write_reg(REG_ACC_RANGE, ACC_RANGE_2G)
        for _ in range(50):
            self.sleep_ms(10)
            if self.read_reg(REG_STATUS) & STATUS_DRDY_ACC:
                break
        else:
            raise InitError("accelerometer produced no data")
        self._init_reference(level)

    def accel(self):
        """(x, y, z) in g."""
        return accel_from_bytes(self.i2c.readfrom_mem(self.address, REG_ACC_X_LSB, 6))

    # -- register access, also used by the BMM150 driver on the aux bus --------

    def read_reg(self, register):
        return self.i2c.readfrom_mem(self.address, register, 1)[0]

    def read_regs(self, register, n):
        return self.i2c.readfrom_mem(self.address, register, n)

    def write_reg(self, register, value):
        self.i2c.writeto_mem(self.address, register, bytes((value,)))

    def write_regs(self, register, data):
        self.i2c.writeto_mem(self.address, register, data)
