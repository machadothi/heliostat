"""The control loop. Once a second: where is the sun, where should the mirror
point, is anything unsafe, and move.

Everything physical arrives by constructor injection -- the two axis drivers,
the sensors, the clock -- so this module imports nothing from hal/ or net/. The
same code drives the real servos on the board and the simulated ones under
pytest, and tests/test_architecture.py keeps it that way.

ONE CYCLE
---------
    1. refresh both axes and read the sensors
    2. compute the sun's position (only if time is valid)
    3. plan the normal the current mode wants, and whether it is reachable
    4. hand a snapshot to the safety supervisor
    5. act on the governing trip: change mode, latch, or resume
    6. command the axes, subject to a one-step deadband
    7. record telemetry

Safety is evaluated BEFORE anything moves, every cycle. A mode requested by the
operator therefore never produces motion until the supervisor has seen it.

WHY 1 Hz WITH A DEADBAND
------------------------
The sun moves 15 deg/hour and the mirror normal half that. One ST3020 step takes
42 s to accumulate and one SC09 step 141 s. Commanding more often than that only
makes the servos hunt and whine. The deadband compares against the last
COMMANDED angle, not the measured one, so encoder noise cannot trigger moves.

INTENT VERSUS MODE
------------------
`intent` is what the operator asked for; `mode` is what is happening. They
differ when the supervisor overrides: asked to track at night, the machine sits
in STOW with intent TRACK, and starts tracking by itself when the sun clears the
minimum elevation. A latched trip resets intent to IDLE, so nothing resumes
until a person has cleared the fault.
"""

from solar.sunpos import sun_position
from util import log
from util.ringbuf import RingBuffer

from control import geometry as geo
from control import modes
from control.kinematics import AzElMount, OutOfReach, keepout_violation, solve
from control.safety import (
    ACTION_NAMES,
    DEFOCUS,
    DEFOCUS_STOW,
    ESTOP,
    FAULT,
    RULE_COMMS,
    STOW,
    PressureTrend,
    Supervisor,
    Trip,
)

TELEMETRY_SAMPLES = 120

# How long to hold a defocus before stowing anyway, if the axes never report
# settled. Defocus is a few degrees of travel; this is generous.
DEFOCUS_SETTLE_TIMEOUT_S = 5.0


class Sensors:
    """Whatever sensors are fitted. Every one is optional."""

    def __init__(self, imu=None, baro=None, wind=None, estop=None, limits=()):
        self.imu = imu
        self.baro = baro
        self.wind = wind
        self.estop = estop
        self.limits = tuple(limits)


class CommandRejected(Exception):
    """An operator request that cannot be honoured right now."""


class Tracker:
    def __init__(self, cfg, az, el, clock, monotonic, sensors=None, mount=AzElMount):
        self.cfg = cfg
        self.az = az
        self.el = el
        self.clock = clock
        self.monotonic = monotonic
        self.sensors = sensors or Sensors()
        self.mount = mount

        self.supervisor = Supervisor(cfg["safety"])
        self.pressure_trend = PressureTrend()
        self.telemetry = RingBuffer(TELEMETRY_SAMPLES)

        self.mode = modes.IDLE
        self.intent = modes.IDLE
        self.latch = None  # the Trip that latched us into safety, if any
        self.trips = []
        self.governing = None

        self._pending_stow_since = None
        self._last_contact = monotonic()
        self._last_cmd = [None, None]
        self._plan = None  # (az_mech, el_mech) the current automatic mode wants
        self._sun = None  # (azimuth, apparent elevation)
        self._beam = None
        self._efficiency = None

    # ======================================================================
    # operator interface -- called from the HTTP and BLE handlers
    # ======================================================================

    def touch(self):
        """Record contact from the phone or API, for the comms-loss rule."""
        self._last_contact = self.monotonic()

    def request_mode(self, mode):
        """Ask for a mode. Raises CommandRejected with a reason if refused.

        Takes effect on the next cycle, after the supervisor has looked at it.
        """
        if not modes.is_valid(mode):
            raise CommandRejected(f"unknown mode: {mode}")
        if self.mode == modes.ESTOP:
            raise CommandRejected("E-stop is latched; release it and clear the fault")
        if self.latch is not None and mode not in (modes.IDLE, modes.STOW):
            raise CommandRejected(f"latched by '{self.latch.reason}'; clear it first")
        if mode in modes.AUTOMATIC_MODES and not self._time_valid():
            # Refused outright rather than armed: without time there is no sun
            # vector, and the operator should hear that now, not discover it.
            raise CommandRejected("no valid time; send the time before tracking")
        if not modes.can_transition(self.mode, mode):
            raise CommandRejected(
                "cannot go from {} to {} (allowed: {})".format(
                    self.mode, mode, ", ".join(modes.allowed_from(self.mode))
                )
            )
        self.intent = mode
        self._enter(mode, "operator request")

    def clear_fault(self):
        """Acknowledge a latched trip and return to IDLE.

        Refused while the E-stop is still physically pressed: clearing needs the
        twist-release AND this call, never one alone.
        """
        estop = self.sensors.estop
        if estop is not None and estop.active():
            raise CommandRejected("release the E-stop before clearing")
        if self.latch is not None:
            log.warning(f"fault cleared by operator: {self.latch.reason}")
        self.latch = None
        self.intent = modes.IDLE
        self._pending_stow_since = None
        self._enter(modes.IDLE, "fault cleared")

    def software_fault(self, reason):
        """Latch a FAULT from outside the supervisor -- a control loop that keeps
        throwing is not controlling anything, and must not be left to drift."""
        self.latch = Trip("software", FAULT, True, reason)
        self.intent = modes.IDLE
        self._pending_stow_since = None
        log.error(f"LATCHED fault: {reason}")
        self._enter(modes.FAULT, reason)

    async def jog(self, axis_index, delta_deg=None, abs_deg=None):
        """Move one axis by hand. MANUAL mode only.

        Refused if, with the sun where it is, the resulting beam would land in a
        keep-out zone -- checked before moving, not after.
        """
        if self.mode != modes.MANUAL:
            raise CommandRejected("jog requires manual mode")
        driver = (self.az, self.el)[axis_index]
        current = driver.state["target_deg"]
        if current is None:
            current = driver.position_deg()
        target = abs_deg if abs_deg is not None else current + (delta_deg or 0.0)
        target, _ = driver.axis.clamp(target)

        joints = [self.az.state["target_deg"], self.el.state["target_deg"]]
        for i, d in enumerate((self.az, self.el)):
            if joints[i] is None:
                joints[i] = d.position_deg()
        joints[axis_index] = target
        self._refuse_if_beam_forbidden(joints[0], joints[1])

        await driver.move_deg(target)
        self._last_cmd[axis_index] = target
        return target

    def capture_target(self):
        """Derive the target from where the beam is landing RIGHT NOW.

        Jog until the spot sits where you want it, then call this. The inverse
        of the bisector gives the target direction directly -- no survey.
        """
        if self._sun is None:
            raise CommandRejected("no sun position; is the time valid?")
        if self._sun[1] <= 0.0:
            raise CommandRejected("the sun is below the horizon")
        normal = self._actual_normal()
        if normal is None:
            raise CommandRejected("axis positions unknown")
        s = geo.sun_vector(*self._sun)
        if geo.dot(s, normal) <= 0.0:
            raise CommandRejected("the sun is behind the mirror; there is no beam")
        azimuth, elevation = geo.azel_from_vector(geo.target_from_normal(s, normal))
        self.cfg["target"] = dict(self.cfg["target"], mode="azel", az=azimuth, el=elevation)
        log.info(f"target captured at az {azimuth:.2f} el {elevation:.2f}")
        return azimuth, elevation

    # ======================================================================
    # the cycle
    # ======================================================================

    async def step(self):
        await self.az.update()
        await self.el.update()
        mono = self.monotonic()
        now = self.clock.now()
        time_ok = self._time_valid()

        tilt, accel = self._read(self.sensors.imu, (None, None))
        pressure, temp = self._read(self.sensors.baro, (None, None))
        if pressure is not None:
            self.pressure_trend.add(mono, pressure)
        wind = self._read(self.sensors.wind, None)
        estop = self.sensors.estop.active() if self.sensors.estop else False
        limit = any(switch.triggered() for switch in self.sensors.limits)

        s = None
        self._sun = None
        if time_ok:
            site = self.cfg["site"]
            sun_az, _, sun_el = sun_position(now, site["lat"], site["lon"], pressure, temp)
            self._sun = (sun_az, sun_el)
            s = geo.sun_vector(sun_az, sun_el)

        t = self._target_vector()
        unreachable_reason = self._plan_normal(s, t)
        self._efficiency = geo.cosine_efficiency(s, t) if s is not None else None
        self._beam = self._actual_beam(s)

        snapshot = {
            "mode": self.mode,
            "intent": self.intent,
            "time_valid": time_ok,
            "estop": estop,
            "sun_el": self._sun[1] if self._sun else None,
            "unreachable": unreachable_reason is not None,
            "unreachable_reason": unreachable_reason,
            "beam": self._beam,
            "servos": [self.az.state, self.el.state],
            "limit_tripped": limit,
            "tilt_deg": tilt,
            "accel_g": accel,
            "pressure_trend": self.pressure_trend.hpa_per_hour(),
            "wind_kph": wind,
            "comms_age_s": mono - self._last_contact,
        }
        self.trips = self.supervisor.evaluate(snapshot)
        comms_lost = any(trip.rule == RULE_COMMS for trip in self.trips)
        self.governing = self.supervisor.governing(self.trips, comms_lost)

        self._act(self.governing, mono)
        await self._drive()
        self._record(now)

    # ======================================================================
    # internals
    # ======================================================================

    def _act(self, trip, mono):
        # A defocus-then-stow completes whether or not the trip persists. Once
        # defocused, a keep-out trip may well have cleared -- the beam has moved
        # -- but the machine latched, so it must still go and park.
        if self._pending_stow_since is not None and self.mode == modes.DEFOCUS:
            settled = self.az.settled and self.el.settled
            if settled or mono - self._pending_stow_since > DEFOCUS_SETTLE_TIMEOUT_S:
                self._pending_stow_since = None
                self._enter(modes.STOW, "defocus complete, stowing")

        if trip is None:
            self._maybe_resume()
            return

        # While latched, anything milder than what latched us is irrelevant: the
        # machine already sits in a response at least that strong.
        if self.latch is not None and trip.action < self.latch.action:
            return

        if trip.latch and (self.latch is None or trip.action > self.latch.action):
            self.latch = trip
            self.intent = modes.IDLE
            log.error(f"LATCHED {ACTION_NAMES[trip.action]}: {trip.reason}")

        action = trip.action
        if action == ESTOP:
            self._enter(modes.ESTOP, trip.reason)
        elif action == FAULT:
            self._enter(modes.FAULT, trip.reason)
        elif action == DEFOCUS_STOW:
            if self.mode == modes.TRACK:
                if self._enter(modes.DEFOCUS, trip.reason):
                    self._pending_stow_since = mono
            elif self.mode == modes.DEFOCUS:
                if self._pending_stow_since is None:
                    self._pending_stow_since = mono
            elif self.mode not in (modes.STOW, modes.FAULT, modes.ESTOP):
                # MANUAL, HOME, IDLE: no target to defocus from; park directly.
                self._enter(modes.STOW, trip.reason)
        elif action == STOW:
            if self.mode not in (modes.STOW, modes.FAULT, modes.ESTOP):
                self._enter(modes.STOW, trip.reason)
        elif action == DEFOCUS and self.mode == modes.TRACK:
            self._enter(modes.DEFOCUS, trip.reason)

    def _maybe_resume(self):
        """Return to the operator's automatic intent once nothing objects."""
        if self.latch is not None or self._pending_stow_since is not None:
            return
        if (
            self.intent in modes.AUTOMATIC_MODES
            and self.mode != self.intent
            and self.mode in (modes.STOW, modes.DEFOCUS, modes.TRACK)
        ):
            self._enter(self.intent, "conditions cleared, resuming")

    def _enter(self, mode, reason=""):
        if mode == self.mode:
            return True
        if not modes.can_transition(self.mode, mode):
            log.warning(f"blocked {self.mode} -> {mode} ({reason})")
            return False
        log.info(f"{self.mode} -> {mode}: {reason}")
        self.mode = mode
        torque = modes.wants_torque(mode)
        self.az.torque(torque)
        self.el.torque(torque)
        # A new mode starts from a clean slate, so its first command is sent
        # even if it happens to sit inside the old deadband.
        self._last_cmd = [None, None]
        if mode != modes.DEFOCUS:
            self._pending_stow_since = None
        return True

    def _plan_normal(self, s, t):
        """Work out the joint angles the automatic mode wants.

        Planned whenever tracking is wanted -- even while stowed -- so the
        machine does not resume into a target it cannot reach and bounce back.
        Returns None if reachable, or a reason string if not.
        """
        self._plan = None
        wanted = self.intent in modes.AUTOMATIC_MODES or self.mode in modes.AUTOMATIC_MODES
        if s is None or not wanted:
            return None
        try:
            if self.mode == modes.DEFOCUS or self.intent == modes.DEFOCUS:
                offset = self.cfg["safety"]["defocus_offset_deg"]
                normal = geo.defocus_normal(s, t, offset)
            else:
                normal = geo.mirror_normal(s, t)
        except geo.UnreachableTarget as exc:
            return str(exc)
        try:
            az, el, clamped = solve(
                self.mount, self.az.axis, self.el.axis, normal, self.az.position_deg()
            )
        except OutOfReach as exc:
            return f"mount cannot reach the normal: {exc}"
        if clamped:
            # The mirror would NOT point where the geometry needs it, so the beam
            # would land somewhere nobody chose. Treat as unreachable.
            return "normal needs travel beyond a soft limit"
        self._plan = (az, el)
        return None

    async def _drive(self):
        if self.mode in modes.AUTOMATIC_MODES:
            if self._plan is not None:
                await self._command(self._plan)
        elif self.mode == modes.STOW:
            stow = self.cfg["safety"]["stow"]
            await self._command((stow["az_mech_deg"], stow["el_mech_deg"]))
        # IDLE holds; MANUAL moves only on jog(); HOME is driven by the homing
        # task; FAULT and ESTOP have torque off.

    async def _command(self, joints):
        steps = self.cfg["tracker"]["deadband_steps"]
        for index, (driver, target) in enumerate(zip((self.az, self.el), joints)):
            last = self._last_cmd[index]
            if last is None or abs(target - last) >= driver.axis.deadband_deg * steps:
                await driver.move_deg(target)
                self._last_cmd[index] = target

    def _time_valid(self):
        return self.clock.is_valid(self.cfg["safety"]["time_stale_s"])

    def _target_vector(self):
        target = self.cfg["target"]
        if target["mode"] == "point":
            return geo.target_vector_from_point(target["x"], target["y"], target["z"])
        return geo.target_vector_from_azel(target["az"], target["el"])

    def _actual_normal(self):
        az, el = self.az.position_deg(), self.el.position_deg()
        if az is None or el is None:
            return None
        return self.mount.normal_from_joints(az, el)

    def _actual_beam(self, s):
        """Direction of the REAL reflected beam, when it is meaningful.

        Only while settled: mid-slew the beam sweeps transiently, and latching a
        keep-out fault on every stow would make the machine unusable. The sweep
        itself is a known gap -- see "acquisition path" in docs/calibration.md.
        Only when the sun is on the mirror's face: otherwise there is no beam.
        """
        if s is None or self.mode not in (modes.TRACK, modes.DEFOCUS, modes.MANUAL):
            return None
        if not (self.az.settled and self.el.settled):
            return None
        normal = self._actual_normal()
        if normal is None or geo.dot(s, normal) <= 0.0:
            return None
        return geo.azel_from_vector(geo.beam_direction(s, normal))

    def _refuse_if_beam_forbidden(self, az_deg, el_deg):
        if self._sun is None or self._sun[1] <= 0.0:
            return
        s = geo.sun_vector(*self._sun)
        normal = self.mount.normal_from_joints(az_deg, el_deg)
        if geo.dot(s, normal) <= 0.0:
            return
        beam = geo.azel_from_vector(geo.beam_direction(s, normal))
        zone = keepout_violation(beam[0], beam[1], self.cfg["safety"]["keepout"])
        if zone is not None:
            raise CommandRejected(
                "that position puts the beam in keep-out zone '{}'".format(
                    zone.get("note", "unnamed")
                )
            )

    @staticmethod
    def _read(device, default):
        return device.read() if device is not None else default

    def _record(self, now):
        self.telemetry.append(
            {
                "t": now,
                "mode": self.mode,
                "sun": self._sun,
                "cmd": tuple(self._last_cmd),
                "pos": (self.az.position_deg(), self.el.position_deg()),
                "beam": self._beam,
                "eff": self._efficiency,
                "trips": [trip.rule for trip in self.trips],
            }
        )

    def status(self):
        """Everything the phone's dashboard needs, in one dict."""
        return {
            "mode": self.mode,
            "intent": self.intent,
            "latch": self.latch.as_dict() if self.latch else None,
            "time_valid": self._time_valid(),
            "time": self.clock.now(),
            "time_age_s": self.clock.age(),
            "sun": self._sun,
            "target": self.cfg["target"],
            "plan": self._plan,
            "beam": self._beam,
            "efficiency": self._efficiency,
            "axes": [self.az.status(), self.el.status()],
            "trips": [trip.as_dict() for trip in self.trips],
            "governing": self.governing.as_dict() if self.governing else None,
            "comms_age_s": self.monotonic() - self._last_contact,
            "allowed_modes": modes.allowed_from(self.mode),
        }
