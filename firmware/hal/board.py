"""The pin map, and the factories that decide between real and simulated parts.

This is the ONE place that knows whether the machine is simulated. Everything
above it -- the tracker, the supervisor, the API -- receives objects with the
same interface either way. Flip `sim` in config.json and the identical control
code runs against pure-Python stand-ins, which is how the whole system is
exercised with no mechanics attached.

The pins come from the board profile (hal/profiles.py): the same firmware runs
on the ESP32 devkit and on the M5Stack AtomS3R.
"""

from control.kinematics import Axis
from control.tracker import Sensors

from hal import profiles

# --- pin map, from the board profile ------------------------------------------
PROFILE = profiles.current()

SERVO_UART = PROFILE["servo_uart"]
SERVO_TX = PROFILE["servo_tx"]
SERVO_RX = PROFILE["servo_rx"]
SERVO_BAUD = 1_000_000

I2C_BUSES = PROFILE["i2c"]  # ((sda, scl), ...), probed in order

LIMIT_AZ = PROFILE["limit_az"]
LIMIT_EL = PROFILE["limit_el"]
ESTOP_SENSE = PROFILE["estop"]
ANEMOMETER = PROFILE["anemometer"]
STATUS = PROFILE["status"]  # "led" or "display"
STATUS_LED = PROFILE["status_led"]
PROVISION_BUTTON = PROFILE["button"]  # also the safe-mode button in main.py

# How far past a soft limit the simulated limit switch sits. On the real machine
# the switch is mounted just beyond the end of normal travel, so routine moves
# never touch it.
SIM_SWITCH_MARGIN_DEG = 1.0


def make_axis_models(cfg):
    """The hardware-free Axis descriptions, built from config."""
    return [Axis(**axis_cfg) for axis_cfg in cfg["axes"]]


def make_axes(cfg, monotonic):
    """Return (azimuth_driver, elevation_driver), real or simulated."""
    models = make_axis_models(cfg)
    max_missed = cfg["safety"]["servo"]["max_missed_replies"]

    if cfg["sim"]:
        from hal.servo_axis_sim import SimAxis

        # +/-2 steps of reading noise: what the bench measured on both servos.
        return tuple(
            SimAxis(model, monotonic, noise_steps=2, seed=index + 1, max_missed=max_missed)
            for index, model in enumerate(models)
        )

    # Real servos arrive in milestone M4 (hal/servo_axis.py).
    from hal.servo_axis import make_real_axes

    return make_real_axes(cfg, models, max_missed)


def make_sensors(cfg, monotonic, axes):
    """Return a Sensors bundle, real or simulated."""
    if cfg["sim"]:
        from hal.sensors_sim import SimBaro, SimIMU
        from hal.switches_sim import SimEstop, SimSwitch

        limits = [
            SimSwitch(
                "limit_" + driver.axis.name,
                axis=driver,
                trip_below_deg=driver.axis.min_deg - SIM_SWITCH_MARGIN_DEG,
                trip_above_deg=driver.axis.max_deg + SIM_SWITCH_MARGIN_DEG,
            )
            for driver in axes
        ]
        # No SimWind: an anemometer is optional hardware, and the default sim
        # should model the machine as it will actually be built first.
        return Sensors(
            imu=SimIMU(), baro=SimBaro(monotonic), estop=SimEstop(), limits=limits
        )

    # Real sensors arrive in milestone M5.
    from hal.sensors import make_real_sensors

    return make_real_sensors(cfg)
