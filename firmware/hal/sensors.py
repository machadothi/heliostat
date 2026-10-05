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


def make_buses():
    """[(name, I2C, addresses present)] for every bus in the board profile."""
    from machine import I2C, Pin

    buses = []
    for index, (sda, scl) in enumerate(board.I2C_BUSES):
        name = "i2c{} (sda {}, scl {})".format(index, sda, scl)
        try:
            bus = I2C(index, sda=Pin(sda), scl=Pin(scl), freq=100_000)
            buses.append((name, bus, bus.scan()))
        except Exception as exc:  # noqa: BLE001
            log.error(f"{name} unavailable: {exc}")
    return buses


IMU_ADDRESS = 0x68  # both the MPU6050 and the BMI270 answer here


def identify_imu(bus):
    """'bmi270', 'mpu6050' or None -- by chip-ID register, since both sit at 0x68."""
    try:
        if bus.readfrom_mem(IMU_ADDRESS, 0x00, 1)[0] == 0x24:  # BMI270 CHIP_ID
            return "bmi270"
        if bus.readfrom_mem(IMU_ADDRESS, 0x75, 1)[0] in (0x68, 0x70, 0x72):  # MPU WHO_AM_I
            return "mpu6050"
    except OSError:
        pass
    return None


def make_imu(bus, kind, level):
    """(imu, magnetometer or None) for an identified chip."""
    if kind == "bmi270":
        from hal.imu_bmi270 import IMU

        imu = IMU(bus, level=level)
        mag = None
        try:
            from hal.mag_bmm150 import Magnetometer

            mag = Magnetometer(imu)
        except Exception as exc:  # noqa: BLE001 - the IMU is still useful without it
            log.warning(f"no BMM150 behind the BMI270: {exc}")
        return imu, mag
    from hal.imu_mpu6050 import IMU

    return IMU(bus, level=level), None


def make_real_sensors(cfg):
    hardware = cfg.get("hardware", {})
    imu = baro = mag = None
    buses = make_buses()
    level = cfg.get("imu", {}).get("level")

    for name, bus, present in buses:
        if imu is not None or IMU_ADDRESS not in present:
            continue
        kind = identify_imu(bus)
        if kind is None:
            continue
        try:
            imu, mag = make_imu(bus, kind, level)
            note = "" if imu.calibrated else "; level not calibrated, using boot attitude"
            extra = " + BMM150" if mag is not None else ""
            log.info(f"{kind.upper()}{extra} found on {name}{note}")
        except Exception as exc:  # noqa: BLE001
            log.error(f"{kind.upper()} on {name} failed: {exc}")
    if imu is None:
        log.warning("no IMU on I2C: tilt and shock rules inactive")

    from hal.baro_bmp180 import ADDRESS as BARO_ADDRESS

    for name, bus, present in buses:
        if baro is None and BARO_ADDRESS in present:
            try:
                from hal.baro_bmp180 import Barometer

                baro = Barometer(bus)
                log.info(f"BMP180 found on {name}")
            except Exception as exc:  # noqa: BLE001
                log.error(f"barometer at 0x77 on {name} failed: {exc}")
    if baro is None:
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

    return Sensors(imu=imu, baro=baro, estop=estop, limits=limits, mag=mag)
