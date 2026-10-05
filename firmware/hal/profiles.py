"""Which board this is, and its pins. Imports nothing, so main.py can use it early.

Two boards run the same firmware:

    esp32_devkit   plain ESP32 devkit (no PSRAM), CP2102 USB-serial, LED on GPIO2,
                   MPU6050 on external I2C
    atoms3r        M5Stack AtomS3R: ESP32-S3 with 8 MB PSRAM, native USB, a
                   128x128 screen, BMI270 IMU (+ BMM150) on its internal I2C

The build settings for each live in boards/<name>.json (tools/deploy.py), and
deploy freezes a one-line `board_id` module naming the profile into the build.
Without it (CPython tests, an .mpy deploy) the chip decides.

Pin notes: on the classic ESP32 avoid the strapping pins 0/2/12/15 for anything
that could hold them at the wrong level during boot, and GPIO34-39, which are
input-only with no pull-ups. On the ESP32-S3 the strapping pins are 0/3/45/46;
GPIO0 and 45 are the AtomS3R's internal I2C, which has its own pull-ups.
"""

PROFILES = {
    "esp32_devkit": {
        "chip": "esp32",
        "servo_uart": 2,
        "servo_tx": 17,
        "servo_rx": 16,
        # (sda, scl) per I2C bus, probed in order.
        "i2c": ((21, 22),),
        "limit_az": 32,
        "limit_el": 33,
        "estop": 25,
        "anemometer": 27,
        "button": 0,  # BOOT: provisioning (hold 3 s) and safe mode (hold at reset)
        "status": "led",
        "status_led": 2,
        "display": None,
    },
    "atoms3r": {
        "chip": "esp32s3",
        "servo_uart": 1,
        # The Grove socket (HY2.0-4P: GND, 5V, G2 yellow, G1 white). Any GPIO
        # can carry a UART on the S3; Grove gives a keyed, locking connector.
        # Measured on the bench: TX G1, RX G2 with the Waveshare adapter.
        "servo_tx": 1,
        "servo_rx": 2,
        # Internal bus first (BMI270 0x68, LP5562 0x30), then the bottom header
        # G5 (SDA) / G6 (SCL) for anything external (a BMP180, an MPU6050).
        "i2c": ((45, 0), (5, 6)),
        "limit_az": 8,
        "limit_el": 38,
        "estop": 7,
        "anemometer": 39,
        "button": 41,  # the screen button
        "status": "display",
        "status_led": None,
        # 3-wire SPI to the GC9107/ST7735S, as in M5GFX's AtomS3R autodetect.
        "display": {"sck": 15, "mosi": 21, "cs": 14, "dc": 42, "rst": 48},
    },
}


def chip():
    """'esp32s3', 'esp32', or None off-board."""
    try:
        import os

        machine = os.uname().machine
    except Exception:  # noqa: BLE001
        return None
    if "ESP32S3" in machine:
        return "esp32s3"
    if "ESP32" in machine:
        return "esp32"
    return None


def name():
    """The profile this firmware runs as."""
    try:
        from board_id import BOARD  # frozen into the build by tools/deploy.py

        return BOARD
    except ImportError:
        pass
    return "atoms3r" if chip() == "esp32s3" else "esp32_devkit"


def current():
    """The pin profile, with its name under "name"."""
    profile = dict(PROFILES[name()])
    profile["name"] = name()
    return profile


def mismatch():
    """A reason string if the profile was built for a different chip, else None.

    Flashing the devkit build onto the Atom (or the reverse) must not start the
    app: the servo UART pins would drive whatever happens to sit there.
    """
    actual = chip()
    wanted = PROFILES[name()]["chip"]
    if actual is not None and actual != wanted:
        return "firmware built for {} ({}), running on {}".format(name(), wanted, actual)
    return None
