"""A simulated servo axis with the same surface as the real one.

Faithful where it matters to the code above it:

  * it slews toward the target at the axis's configured speed, rather than
    teleporting, so settling and backlash overshoot behave as on hardware;
  * readings are quantised to the family's encoder step (0.088 deg for the
    ST3020, 0.293 for the SC09);
  * readings carry +/- `noise_steps` of jitter -- the bench measured +/-2 --
    so deadband logic is exercised against realistic noise;
  * it works in SERVO degrees internally and converts through the axis
    calibration, so an offset or direction error shows up here too.

Faults can be injected through `faults` to drive every safety rule from a test.
"""

from control.kinematics import FAMILY_DEGREES, FAMILY_STEPS

from hal.axis_base import AxisDriver

# What each servo reported on the bench, through the Waveshare driver board.
NOMINAL_VOLTS = {"STS": 12.3, "SCS": 9.9}


class SimAxis(AxisDriver):
    def __init__(
        self, axis, monotonic, start_mech_deg=None, noise_steps=0, seed=1, max_missed=3
    ):
        super().__init__(axis)
        self._monotonic = monotonic
        self._step = FAMILY_DEGREES[axis.family] / FAMILY_STEPS[axis.family]
        self._noise_steps = noise_steps
        self._seed = seed
        self._max_missed = max_missed

        if start_mech_deg is None:
            start_mech_deg = 0.5 * (axis.min_deg + axis.max_deg)
        self._servo_true = axis.to_servo_deg(start_mech_deg)
        self._servo_cmd = self._servo_true
        self._speed_limit = None
        self._last_t = monotonic()

        # Set any of these to force a fault on the next refresh.
        # sag_deg: a load the servo holds short of its goal, in servo degrees --
        # the tilt axis under the mirror's weight sat ~0.9 deg low on the bench.
        self.faults = {"no_reply": False, "temp_c": None, "volts": None, "load": None, "sag_deg": 0.0}

    # -- test hooks -------------------------------------------------------------

    def true_mech_deg(self):
        """Where the joint really is, without quantisation or noise."""
        return self.axis.from_servo_deg(self._servo_true)

    def teleport(self, mech_deg):
        """Put the joint somewhere instantly. For test setup only."""
        self._servo_true = self.axis.to_servo_deg(mech_deg)
        self._servo_cmd = self._servo_true

    def push_by_hand(self, mech_deg):
        """Move a released joint by hand: the servo's stored goal is NOT changed."""
        self._servo_true = self.axis.to_servo_deg(mech_deg)

    # -- AxisDriver hooks -------------------------------------------------------

    async def _command(self, servo_deg, speed_dps):
        if self.faults["no_reply"]:
            return  # the command is lost on the wire, just as on hardware
        # A real servo takes an INTEGER step position. Without this the
        # simulated joint could rest between steps -- somewhere the hardware
        # never can -- and disagree with its own quantised reading.
        self._servo_cmd = round(servo_deg / self._step) * self._step
        self._speed_limit = speed_dps

    async def _refresh(self):
        now = self._monotonic()
        dt = max(0.0, now - self._last_t)
        self._last_t = now

        if self.faults["no_reply"]:
            self.state["missed"] += 1
            self.state["ok"] = self.state["missed"] < self._max_missed
            return  # state goes stale, exactly as it would

        self.state["missed"] = 0
        self.state["ok"] = True

        if self.torque_on:
            speed = self.axis.max_speed_dps * self.axis.gear_ratio
            if self._speed_limit:
                speed = min(speed, self._speed_limit * self.axis.gear_ratio)
            goal = self._servo_cmd - self.faults["sag_deg"]
            error = goal - self._servo_true
            travel = speed * dt
            if abs(error) <= travel:
                self._servo_true = goal
            else:
                self._servo_true += travel if error > 0 else -travel

        reading = round(self._servo_true / self._step) * self._step
        if self._noise_steps:
            reading += self._noise() * self._step

        self.state["position_deg"] = self.axis.from_servo_deg(reading)
        goal = self._servo_cmd - self.faults["sag_deg"]
        self.state["moving"] = self.torque_on and abs(goal - self._servo_true) > 0.5 * self._step
        self.state["volts"] = _override(self.faults["volts"], NOMINAL_VOLTS[self.axis.family])
        self.state["temp_c"] = _override(self.faults["temp_c"], 25)
        self.state["load"] = _override(self.faults["load"], 0)

    def _set_torque(self, on):
        # Like the hardware: a release keeps the stored goal (the joint just stops
        # being driven), and the real driver sets goal := present on enable.
        if on:
            self._servo_cmd = self._servo_true

    # -- internals --------------------------------------------------------------

    def _noise(self):
        """Deterministic integer jitter in [-noise_steps, +noise_steps].

        A small LCG rather than `random`, so a seeded test reproduces exactly on
        both CPython and MicroPython.
        """
        self._seed = (1103515245 * self._seed + 12345) & 0x7FFFFFFF
        span = 2 * self._noise_steps + 1
        return (self._seed % span) - self._noise_steps


def _override(forced, nominal):
    return nominal if forced is None else forced
