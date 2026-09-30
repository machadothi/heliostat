"""Real sensors: whatever is actually wired, found by probing the I2C bus.

Each sensor is optional. A missing one leaves its safety rule silent, and the
boot log says so -- so a bench with only the IMU connected runs, and runs
honestly about what it is not checking.

Limit switches and the E-stop are different: they are wired normally-closed, so
an input with NOTHING connected reads exactly like a switch that has opened.
Enabling them before they are wired would fault (or E-stop) the machine on the
spot. They are therefore opt-in, via `hardware.limit_switches` and
`hardware.estop` in config.json.
"""

from control.tracker import Sensors
from util import log

from hal import board


class _NormallyClosedInput:
    """A switch wired COM->GND, NC->GPIO with the internal pull-up.

    Closed (normal) reads LOW. Open -- triggered, OR a broken wire, OR nothing
    connected at all -- reads HIGH. The failure mode looks like a trip, never
    like "all clear". That is the point of wiring it this way.
    """

    def __init__(self, name, pin_number):
        from machine import Pin

        self.name = name
        self._pin = Pin(pin_number, Pin.IN, Pin.PULL_UP)
        self._high_count = 0

    def triggered(self):
        # Two consecutive HIGH reads, one control cycle apart, to ride out
        # contact bounce and noise on a long outdoor run.
        if self._pin.value():
            self._high_count += 1
        else:
            self._high_count = 0
        return self._high_count >= 2

    def active(self):
        """Same signal, under the name the E-stop interface uses."""
        return self.triggered()


def make_i2c():
    from machine import I2C, Pin

    return I2C(0, sda=Pin(board.I2C_SDA), scl=Pin(board.I2C_SCL), freq=100_000)


def make_real_sensors(cfg):
    hardware = cfg.get("hardware", {})
    imu = baro = None

    try:
        i2c = make_i2c()
        present = i2c.scan()
    except Exception as exc:  # noqa: BLE001
        log.error(f"I2C bus unavailable: {exc}")
        i2c, present = None, []

    from hal.imu_mpu6050 import ADDRESS as IMU_ADDRESS

    if i2c is not None and IMU_ADDRESS in present:
        try:
            from hal.imu_mpu6050 import IMU

            imu = IMU(i2c, level=cfg.get("imu", {}).get("level"))
            note = "" if imu.calibrated else "; level not calibrated, using boot attitude"
            log.info(f"MPU6050 found (WHO_AM_I 0x{imu.who_am_i:02X}){note}")
        except Exception as exc:  # noqa: BLE001
            log.error(f"MPU6050 present but failed: {exc}")
    else:
        log.warning("no MPU6050 on I2C: tilt and shock rules inactive")

    from hal.baro_bmp180 import ADDRESS as BARO_ADDRESS

    if i2c is not None and BARO_ADDRESS in present:
        try:
            from hal.baro_bmp180 import Barometer

            baro = Barometer(i2c)
            log.info("BMP180 found")
        except Exception as exc:  # noqa: BLE001
            log.error(f"barometer at 0x77 failed: {exc}")
    else:
        log.warning("no BMP180 on I2C: standard atmosphere for refraction, no storm proxy")

    limits = ()
    if hardware.get("limit_switches"):
        limits = (_NormallyClosedInput("limit_az", board.LIMIT_AZ),
                  _NormallyClosedInput("limit_el", board.LIMIT_EL))
    else:
        log.warning("limit switches disabled in config (hardware.limit_switches)")

    estop = None
    if hardware.get("estop"):
        estop = _NormallyClosedInput("estop", board.ESTOP_SENSE)
    else:
        log.warning("E-stop input disabled in config (hardware.estop)")

    return Sensors(imu=imu, baro=baro, estop=estop, limits=limits)
