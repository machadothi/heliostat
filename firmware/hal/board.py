"""The pin map, and the factories that decide between real and simulated parts.

This is the ONE place that knows whether the machine is simulated. Everything
above it -- the tracker, the supervisor, the API -- receives objects with the
same interface either way. Flip `sim` in config.json and the identical control
code runs against pure-Python stand-ins, which is how the whole system is
exercised with no mechanics attached.

Pin choices avoid the ESP32 strapping pins (0, 2, 12, 15) for anything that
could hold them at the wrong level during boot, and avoid GPIO34-39, which are
input-only with NO internal pull-ups. See docs/wiring.md.
"""

from control.kinematics import Axis
from control.tracker import Sensors

# --- pin map ------------------------------------------------------------------
SERVO_UART = 2
SERVO_TX = 17
SERVO_RX = 16
SERVO_BAUD = 1_000_000

I2C_SDA = 21
I2C_SCL = 22

LIMIT_AZ = 32
LIMIT_EL = 33
ESTOP_SENSE = 25
ANEMOMETER = 27
STATUS_LED = 2
PROVISION_BUTTON = 0  # the BOOT button; also the safe-mode button in main.py

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
