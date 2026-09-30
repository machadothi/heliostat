"""BMP180 barometer and thermometer.

Two uses, one of them real:
  * Atmospheric REFRACTION. The sun's apparent elevation depends on the air's
    pressure and temperature; feeding real values to solar/refraction.py makes
    the correction site-specific instead of assuming a standard atmosphere.
  * A falling-barometer storm PROXY for the weather stow rule. A proxy only:
    it will not catch a dry gust front. An anemometer does that.

`compensate()` is Bosch's datasheet algorithm, integer arithmetic and all, kept
free of hardware so tests/test_sensors.py can check it against the datasheet's
own worked example. The BMP280/BME280 successors have a different register map
and need their own driver.
"""

import time

ADDRESS = 0x77

_CHIP_ID = 0xD0
_EXPECTED_CHIP_ID = 0x55
_CALIBRATION = 0xAA
_CONTROL = 0xF4
_OUT = 0xF6
_READ_TEMPERATURE = 0x2E
_READ_PRESSURE = 0x34

# Oversampling 0..3. 3 = "ultra high resolution", 25.5 ms per pressure read --
# fine at a 1 Hz loop, and the lowest noise.
OVERSAMPLING = 3
_PRESSURE_WAIT_MS = (5, 8, 14, 26)


def parse_calibration(data):
    """The 11 calibration words: AC1..AC3 and B1, B2, MB, MC, MD are signed."""
    words = [(data[i] << 8) | data[i + 1] for i in range(0, 22, 2)]
    signed = (0, 1, 2, 6, 7, 8, 9, 10)
    return [w - 65536 if (i in signed and w & 0x8000) else w for i, w in enumerate(words)]


def compensate(cal, ut, up, oss):
    """Datasheet algorithm. Returns (temperature in 0.1 degC, pressure in Pa)."""
    ac1, ac2, ac3, ac4, ac5, ac6, b1, b2, _mb, mc, md = cal

    x1 = ((ut - ac6) * ac5) >> 15
    x2 = (mc << 11) // (x1 + md)
    b5 = x1 + x2
    temperature = (b5 + 8) >> 4

    b6 = b5 - 4000
    x1 = (b2 * ((b6 * b6) >> 12)) >> 11
    x2 = (ac2 * b6) >> 11
    x3 = x1 + x2
    b3 = (((ac1 * 4 + x3) << oss) + 2) // 4
    x1 = (ac3 * b6) >> 13
    x2 = (b1 * ((b6 * b6) >> 12)) >> 16
    x3 = ((x1 + x2) + 2) >> 2
    b4 = (ac4 * (x3 + 32768)) >> 15
    b7 = (up - b3) * (50000 >> oss)
    p = (b7 * 2) // b4 if b7 < 0x80000000 else (b7 // b4) * 2
    x1 = (p >> 8) * (p >> 8)
    x1 = (x1 * 3038) >> 16
    x2 = (-7357 * p) >> 16
    pressure = p + ((x1 + x2 + 3791) >> 4)
    return temperature, pressure


class Barometer:
    def __init__(self, i2c, address=ADDRESS, oversampling=OVERSAMPLING):
        self.i2c = i2c
        self.address = address
        self.oss = oversampling
        chip = self.i2c.readfrom_mem(address, _CHIP_ID, 1)[0]
        if chip != _EXPECTED_CHIP_ID:
            raise OSError(f"0x{address:02X} is not a BMP180 (chip id 0x{chip:02X})")
        self.cal = parse_calibration(self.i2c.readfrom_mem(address, _CALIBRATION, 22))

    def read(self):
        """(pressure_hpa, temp_c). Blocks ~31 ms at the default oversampling."""
        self.i2c.writeto_mem(self.address, _CONTROL, bytes((_READ_TEMPERATURE,)))
        time.sleep_ms(5)
        raw = self.i2c.readfrom_mem(self.address, _OUT, 2)
        ut = (raw[0] << 8) | raw[1]

        self.i2c.writeto_mem(self.address, _CONTROL, bytes((_READ_PRESSURE + (self.oss << 6),)))
        time.sleep_ms(_PRESSURE_WAIT_MS[self.oss])
        raw = self.i2c.readfrom_mem(self.address, _OUT, 3)
        up = ((raw[0] << 16) | (raw[1] << 8) | raw[2]) >> (8 - self.oss)

        temperature, pressure = compensate(self.cal, ut, up, self.oss)
        return pressure / 100.0, temperature / 10.0
