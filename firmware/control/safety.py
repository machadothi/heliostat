"""The safety supervisor. Every trip rule, in one place.

A heliostat concentrates sunlight. A mirror aimed wrongly, or left unattended in
a fault, can blind someone or scorch whatever the beam lands on. So the rules
that stop that from happening live together here, rather than being scattered as
`if` statements through the tracker, the API and the drivers -- where they would
quietly disagree with each other.

The supervisor is a PURE EVALUATOR. It takes a snapshot of the machine's state
and returns the trips that apply. It never moves anything. The tracker decides
how to act on the result, which keeps every rule testable with a dict and no
hardware at all.

RESPONSES, from mildest to most severe
--------------------------------------
    LOG           note it and carry on
    DEFOCUS       rotate the beam a few degrees off target; instantly reversible
    STOW          park the mirror face-down, where it can reflect nothing
    DEFOCUS_STOW  defocus first -- a sub-second move -- then stow
    FAULT         torque off; needs an operator to clear it
    ESTOP         latched; servo power is cut in HARDWARE, not here

Defocus comes before stow because of timing. A defocus is a few degrees of servo
travel and gets the concentrated spot off its target within a second; a stow can
be well over a hundred degrees and take tens of seconds. When something is wrong
with where the beam is going, the first second matters most.

LATCHING
--------
Environmental trips (sun too low, no valid time, bad weather) do NOT latch. The
mirror stows while the condition lasts and resumes tracking by itself when it
clears -- otherwise someone would have to press "track" every morning.

Hazard trips (keep-out, tilt, a servo or limit fault, the E-stop) DO latch. Once
one fires, the machine stays safe until a person has looked at it and cleared it.
"""

from util.ringbuf import RingBuffer

from control.kinematics import keepout_violation

LOG = 0
DEFOCUS = 1
STOW = 2
DEFOCUS_STOW = 3
FAULT = 4
ESTOP = 5

ACTION_NAMES = {
    LOG: "log",
    DEFOCUS: "defocus",
    STOW: "stow",
    DEFOCUS_STOW: "defocus_stow",
    FAULT: "fault",
    ESTOP: "estop",
}

# Rule identifiers. Strings, so they read properly in logs and over the API.
RULE_NO_TIME = "no_time"
RULE_ESTOP = "estop"
RULE_KEEPOUT = "keepout"
RULE_SUN_LOW = "sun_low"
RULE_UNREACHABLE = "unreachable"
RULE_SERVO = "servo"
RULE_LIMIT = "limit"
RULE_TILT = "tilt"
RULE_WEATHER = "weather"
RULE_COMMS = "comms"


class Trip:
    """One rule that fired, what it demands, and why."""

    __slots__ = ("rule", "action", "latch", "reason")

    def __init__(self, rule, action, latch, reason):
        self.rule = rule
        self.action = action
        self.latch = latch
        self.reason = reason

    def as_dict(self):
        return {
            "rule": self.rule,
            "action": ACTION_NAMES[self.action],
            "latch": self.latch,
            "reason": self.reason,
        }

    def __repr__(self):
        return f"<Trip {self.rule} {ACTION_NAMES[self.action]} {self.reason}>"


class Supervisor:
    """Evaluates the rules against a snapshot of the machine.

    The snapshot is a plain dict, so a test can build one by hand. Keys:

        mode, intent        current mode and the operator's standing request
        time_valid          bool
        estop               bool, physical E-stop input
        sun_el              apparent solar elevation in degrees, or None
        unreachable         bool, target opposite the sun
        beam                (az, el) of the ACTUAL reflected beam, or None
        servos              list of per-axis state dicts from the drivers
        limit_tripped       bool, any limit switch open
        tilt_deg, accel_g   from the IMU, or None
        pressure_trend      hPa per hour (negative is falling), or None
        wind_kph            or None
        comms_age_s         seconds since the phone or API last spoke
    """

    def __init__(self, safety_cfg):
        self.cfg = safety_cfg

    # -- the rules -----------------------------------------------------------

    def evaluate(self, snap):
        trips = []
        mode = snap["mode"]
        wants_tracking = snap["intent"] == "track" or mode in ("track", "defocus")

        # 1. E-STOP. Checked first because it overrides everything. The relay has
        #    already cut servo power by the time we see this; the job here is to
        #    latch, refuse commands, and tell the phone.
        if snap.get("estop"):
            trips.append(Trip(RULE_ESTOP, ESTOP, True, "emergency stop pressed"))

        # 2. NO VALID TIME. The single most important rule: without time there is
        #    no sun vector, and tracking would aim the beam somewhere
        #    unpredictable. Stowing needs no time, so that is the response.
        if wants_tracking and not snap.get("time_valid"):
            trips.append(Trip(RULE_NO_TIME, STOW, False, "no valid time; refusing to track"))

        # 3. SUN TOO LOW. Also the normal end-of-day path. Not latched: the
        #    mirror resumes by itself at dawn.
        sun_el = snap.get("sun_el")
        if wants_tracking and sun_el is not None and sun_el < self.cfg["min_sun_elev_deg"]:
            trips.append(
                Trip(RULE_SUN_LOW, STOW, False, f"sun at {sun_el:.1f} deg, below limit")
            )

        # 4. UNREACHABLE TARGET. Either the target sits (almost) opposite the sun
        #    and would need grazing incidence, or the normal it needs falls in the
        #    azimuth dead zone or past a soft limit. Either way no defocused
        #    normal is available -- it derives from the same bisector -- so stow
        #    until the sun has moved on. Not latched.
        if wants_tracking and snap.get("unreachable"):
            reason = snap.get("unreachable_reason") or "target unreachable"
            trips.append(Trip(RULE_UNREACHABLE, STOW, False, reason))

        # 5. KEEP-OUT. Checked on the ACTUAL beam, never the normal.
        beam = snap.get("beam")
        if beam is not None:
            zone = keepout_violation(beam[0], beam[1], self.cfg.get("keepout"))
            if zone is not None:
                trips.append(
                    Trip(
                        RULE_KEEPOUT,
                        DEFOCUS_STOW,
                        True,
                        "beam at az {:.1f} el {:.1f} enters zone '{}'".format(
                            beam[0], beam[1], zone.get("note", "unnamed")
                        ),
                    )
                )

        # 6. SERVO FAULT. Skipped under E-stop, where the relay has cut servo
        #    power and silence is exactly what we expect.
        if mode != "estop":
            for servo in snap.get("servos") or ():
                problem = self._servo_problem(servo)
                if problem:
                    trips.append(Trip(RULE_SERVO, FAULT, True, problem))

        # 7. LIMIT SWITCH. Wired normally-closed, so a broken wire reads as a
        #    trip: the failure looks like a limit hit, never like "all clear".
        #    Ignored while homing, which drives onto the switches on purpose.
        if snap.get("limit_tripped") and mode != "home":
            trips.append(Trip(RULE_LIMIT, FAULT, True, "limit switch open"))

        # 8. TILT OR SHOCK. The base has moved; every calibration is now suspect.
        tilt = snap.get("tilt_deg")
        if tilt is not None and tilt > self.cfg["tilt_limit_deg"]:
            trips.append(
                Trip(RULE_TILT, DEFOCUS_STOW, True, f"base tilted {tilt:.1f} deg")
            )
        accel = snap.get("accel_g")
        if accel is not None and accel > self.cfg["shock_g"]:
            trips.append(Trip(RULE_TILT, DEFOCUS_STOW, True, f"shock of {accel:.2f} g"))

        # 9. WEATHER. A falling barometer is only a storm PROXY -- it will not
        #    catch a dry gust front, which is what flips a mirror. An anemometer
        #    makes this real; see docs/bom.md.
        trend = snap.get("pressure_trend")
        if trend is not None and trend < -self.cfg["pressure_drop_hpa_per_hr"]:
            trips.append(
                Trip(RULE_WEATHER, STOW, False, f"pressure falling {-trend:.1f} hPa/h")
            )
        wind = snap.get("wind_kph")
        if wind is not None and wind > self.cfg["wind_stow_kph"]:
            trips.append(Trip(RULE_WEATHER, STOW, False, f"wind {wind:.0f} km/h"))

        # 10. COMMS LOSS. Logged, NOT acted on. A correctly tracking heliostat is
        #     safe; freezing it because the phone walked out of range would leave
        #     the mirror still while the sun moves, drifting the beam across
        #     whatever lies beside the target. See governing() for the escalation.
        comms_age = snap.get("comms_age_s")
        if comms_age is not None and comms_age > self.cfg["comms_timeout_s"]:
            trips.append(
                Trip(RULE_COMMS, LOG, False, f"no contact for {comms_age:.0f} s")
            )

        return trips

    def _servo_problem(self, servo):
        limits = self.cfg["servo"]
        name = servo.get("name", "?")
        if not servo.get("ok", True):
            return f"{name}: not responding"
        temp = servo.get("temp_c")
        if temp is not None and temp > limits["max_temp_c"]:
            return f"{name}: {temp} C over temperature"
        volts = servo.get("volts")
        window = limits["volt_limits"].get(servo.get("family"))
        if volts is not None and window is not None and not window[0] <= volts <= window[1]:
            return f"{name}: {volts:.1f} V outside {window[0]}-{window[1]} V"
        load = servo.get("load")
        if load is not None and abs(load) > limits["max_load"]:
            return f"{name}: load {load} over limit"
        return None

    # -- choosing a response ---------------------------------------------------

    def governing(self, trips, comms_lost=False):
        """The single trip that decides what happens this cycle, or None.

        The most severe action wins. Among equals, a latching trip beats a
        non-latching one, so a hazard is never masked by an environmental stow.

        If comms are lost AND something else has tripped, a DEFOCUS or
        DEFOCUS_STOW is escalated straight to STOW: with nobody watching, there
        is no point holding the mirror anywhere near its target.
        """
        best = None
        for trip in trips:
            if trip.action == LOG:
                continue
            if best is None or (trip.action, trip.latch) > (best.action, best.latch):
                best = trip

        if best is not None and comms_lost and best.action in (DEFOCUS, DEFOCUS_STOW):
            best = Trip(best.rule, STOW, best.latch, best.reason + " (unattended: stowing)")
        return best


class PressureTrend:
    """Rate of change of barometric pressure, from a rolling window.

    Samples no more often than `sample_every_s` into a fixed buffer, and reports
    nothing until the samples span at least `min_span_s` -- a slope over a few
    seconds is noise, not weather.
    """

    def __init__(self, window_s=1800.0, min_span_s=600.0, sample_every_s=60.0):
        self.min_span_s = min_span_s
        self.sample_every_s = sample_every_s
        self._samples = RingBuffer(int(window_s / sample_every_s) + 1)

    def add(self, t_s, pressure_hpa):
        latest = self._samples.latest()
        if latest is None or t_s - latest[0] >= self.sample_every_s:
            self._samples.append((t_s, pressure_hpa))

    def hpa_per_hour(self):
        if len(self._samples) < 2:
            return None
        samples = self._samples.to_list()
        (t0, p0), (t1, p1) = samples[0], samples[-1]
        span = t1 - t0
        if span < self.min_span_s:
            return None
        return (p1 - p0) / span * 3600.0

