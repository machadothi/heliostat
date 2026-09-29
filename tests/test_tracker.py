"""The tracker end to end: real control code, simulated servos, fake time.

Where tests/test_safety.py checks each rule in isolation, this checks that the
machine actually DOES the right thing when a rule fires -- that a keep-out trip
really defocuses before it stows, that a latched fault really refuses to resume,
that the mirror really starts by itself at dawn.
"""

import asyncio

import pytest
from config import DEFAULTS, ConfigStore
from control import geometry as geo
from control import modes
from control.clock import Clock
from control.tracker import CommandRejected, Tracker
from hal import board
from hal.servo_axis_sim import SimAxis
from solar.julian import unix_from_civil

GREENWICH = {"lat": 51.4779, "lon": -0.0015}
NOON = unix_from_civil(2026, 6, 21, 12, 0, 0)
BEFORE_DAWN = unix_from_civil(2026, 6, 21, 4, 0, 0)
EVENING = unix_from_civil(2026, 6, 21, 19, 0, 0)
TARGET = {"mode": "azel", "az": 180.0, "el": 10.0}


class FakeMonotonic:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Rig:
    """A tracker on simulated hardware, with the handles a test needs."""

    def __init__(self, epoch=NOON, rate=1.0, noise=0, overlay=None):
        data = {"sim": True, "site": dict(GREENWICH), "target": dict(TARGET)}
        if overlay:
            data.update(overlay)
        self.cfg = ConfigStore(data=data).data
        self.time = FakeMonotonic()
        self.clock = Clock(self.time, epoch=epoch, rate=rate)
        models = board.make_axis_models(self.cfg)
        self.az, self.el = (
            SimAxis(model, self.time, noise_steps=noise, seed=i + 1)
            for i, model in enumerate(models)
        )
        self.sensors = board.make_sensors(self.cfg, self.time, (self.az, self.el))
        self.tracker = Tracker(self.cfg, self.az, self.el, self.clock, self.time, self.sensors)
        self.modes_seen = []

    def run(self, cycles=1, dt=1.0, touch=True):
        async def go():
            for _ in range(cycles):
                self.time.advance(dt)
                if touch:
                    self.tracker.touch()
                await self.tracker.step()
                if not self.modes_seen or self.modes_seen[-1] != self.tracker.mode:
                    self.modes_seen.append(self.tracker.mode)

        asyncio.run(go())
        return self

    def true_beam_error(self):
        normal = self.tracker.mount.normal_from_joints(
            self.az.true_mech_deg(), self.el.true_mech_deg()
        )
        s = geo.sun_vector(*self.tracker._sun)
        target = geo.target_vector_from_azel(self.cfg["target"]["az"], self.cfg["target"]["el"])
        return geo.angle_between_deg(geo.beam_direction(s, normal), target)

    @property
    def mode(self):
        return self.tracker.mode


def _count_moves(driver):
    counter = {"n": 0}
    original = driver.move_deg

    async def counted(*args, **kwargs):
        counter["n"] += 1
        return await original(*args, **kwargs)

    driver.move_deg = counted
    return counter


# --- boot and time ------------------------------------------------------------


def test_boots_idle_and_does_not_move():
    rig = Rig(epoch=None)
    moves = _count_moves(rig.az)
    rig.run(10)
    assert rig.mode == modes.IDLE
    assert moves["n"] == 0


def test_refuses_to_track_without_valid_time():
    rig = Rig(epoch=None)
    with pytest.raises(CommandRejected, match="no valid time"):
        rig.tracker.request_mode(modes.TRACK)
    assert rig.mode == modes.IDLE


def test_stale_time_is_not_trusted():
    rig = Rig()
    rig.time.advance(rig.cfg["safety"]["time_stale_s"] + 1)
    with pytest.raises(CommandRejected, match="no valid time"):
        rig.tracker.request_mode(modes.TRACK)


# --- tracking -----------------------------------------------------------------


def test_tracks_the_beam_onto_the_target():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(10)
    assert rig.mode == modes.TRACK
    # 2 x the SC09's step is the resolution floor at the beam.
    assert rig.true_beam_error() < 0.6


def test_over_a_day_the_mirror_sweeps_half_the_suns_azimuth():
    """The heliostat rule of thumb, checked where it actually holds.

    It is exact only for motion within the plane of incidence. Over any short
    stretch the azimuth ratio wanders -- near noon the sun's azimuth races while
    the normal's does not -- but across a whole tracking day it comes out at
    half, and that is what sizes the azimuth travel.
    """
    rig = Rig(epoch=BEFORE_DAWN, rate=60.0)
    rig.tracker.request_mode(modes.TRACK)
    sun_az, mirror_az = [], []
    for _ in range(18 * 60):
        rig.run(1)
        if rig.mode == modes.TRACK:
            sun_az.append(rig.tracker._sun[0])
            mirror_az.append(rig.az.true_mech_deg())
    sun_span = max(sun_az) - min(sun_az)
    mirror_span = max(mirror_az) - min(mirror_az)
    assert mirror_span / sun_span == pytest.approx(0.5, abs=0.03)


def test_encoder_noise_does_not_cause_commands():
    """The deadband compares against the last COMMAND, never the noisy reading."""
    rig = Rig(rate=0.0, noise=2)  # the sun stands still
    rig.tracker.request_mode(modes.TRACK)
    rig.run(10)
    az_moves, el_moves = _count_moves(rig.az), _count_moves(rig.el)
    rig.run(200)
    assert az_moves["n"] == 0
    assert el_moves["n"] == 0


def test_arming_at_night_stows_then_starts_by_itself_at_dawn():
    rig = Rig(epoch=BEFORE_DAWN, rate=60.0)
    rig.tracker.request_mode(modes.TRACK)
    rig.run(3)
    assert rig.mode == modes.STOW
    assert rig.tracker.intent == modes.TRACK
    rig.run(60)  # an hour of solar time: the sun clears 5 degrees about 04:29
    assert rig.mode == modes.TRACK


def test_stows_at_dusk_and_keeps_the_intent():
    rig = Rig(epoch=EVENING, rate=60.0)
    rig.tracker.request_mode(modes.TRACK)
    rig.run(90)
    assert rig.mode == modes.STOW
    assert rig.tracker.intent == modes.TRACK
    assert rig.tracker.latch is None


def test_a_full_day():
    """M3's definition of done: stowed overnight, tracking all day, on target."""
    rig = Rig(epoch=unix_from_civil(2026, 6, 21), rate=60.0, noise=2)
    rig.tracker.request_mode(modes.TRACK)
    worst = 0.0
    for _ in range(1440):
        rig.run(1)
        if rig.mode == modes.TRACK and rig.tracker._beam is not None:
            worst = max(worst, rig.true_beam_error())
    assert rig.modes_seen == [modes.STOW, modes.TRACK, modes.STOW]
    # 0.59 deg is the SC09 floor; the rest is 60x time acceleration lag.
    assert worst < 0.8


# --- unreachable targets ------------------------------------------------------


def test_a_target_opposite_the_sun_stows_without_latching():
    # At noon the sun is due south at 62 deg; aim at its antipode.
    rig = Rig(overlay={"target": {"mode": "azel", "az": 0.0, "el": -62.0}})
    rig.tracker.request_mode(modes.TRACK)
    rig.run(3)
    assert rig.mode == modes.STOW
    assert rig.tracker.latch is None
    assert rig.tracker.intent == modes.TRACK


def test_a_normal_in_the_dead_zone_stows():
    """An azimuth travel of 200..340 cannot reach the ~180 deg normal needed at noon."""
    axes = [dict(axis) for axis in DEFAULTS["axes"]]
    axes[0].update(min_deg=200.0, max_deg=340.0, home_mech_deg=200.0)
    rig = Rig(overlay={"axes": axes})
    rig.tracker.request_mode(modes.TRACK)
    rig.run(3)
    assert rig.mode == modes.STOW
    assert any("reach" in t["reason"] for t in rig.tracker.status()["trips"])


# --- keep-out -----------------------------------------------------------------


def test_keepout_defocuses_first_then_stows_and_latches():
    zone = {"az_min": 170, "az_max": 190, "el_min": 0, "el_max": 20, "note": "window"}
    rig = Rig()
    rig.cfg["safety"]["keepout"] = [zone]
    rig.tracker.supervisor.cfg = rig.cfg["safety"]
    rig.tracker.request_mode(modes.TRACK)
    rig.run(20)
    assert rig.modes_seen == [modes.TRACK, modes.DEFOCUS, modes.STOW]
    assert rig.tracker.latch.rule == "keepout"
    assert rig.tracker.intent == modes.IDLE


def test_a_latched_machine_refuses_to_track_until_cleared():
    zone = {"az_min": 170, "az_max": 190, "el_min": 0, "el_max": 20}
    rig = Rig()
    rig.cfg["safety"]["keepout"] = [zone]
    rig.tracker.request_mode(modes.TRACK)
    rig.run(20)
    with pytest.raises(CommandRejected, match="latched"):
        rig.tracker.request_mode(modes.TRACK)

    rig.cfg["safety"]["keepout"] = []  # the operator fixes the configuration
    rig.tracker.clear_fault()
    assert rig.mode == modes.IDLE
    rig.tracker.request_mode(modes.TRACK)
    rig.run(10)
    assert rig.mode == modes.TRACK


# --- tilt and weather ---------------------------------------------------------


def test_tilt_defocuses_then_stows_and_latches():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(10)
    rig.sensors.imu.tilt_deg = 8.0
    rig.run(20)
    assert rig.modes_seen[-2:] == [modes.DEFOCUS, modes.STOW]
    assert rig.tracker.latch.rule == "tilt"


def test_falling_pressure_stows_without_latching():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(5)
    rig.sensors.baro.ramp(-4.0)
    rig.run(15, dt=60.0)  # fifteen minutes, enough history for a trend
    assert rig.mode == modes.STOW
    assert rig.tracker.latch is None


# --- faults -------------------------------------------------------------------


def test_a_silent_servo_faults_after_the_allowed_misses():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(5)
    rig.el.faults["no_reply"] = True
    rig.run(2)
    assert rig.mode == modes.TRACK  # two misses are tolerated
    rig.run(2)
    assert rig.mode == modes.FAULT
    assert not rig.az.torque_on


def test_a_sagging_supply_faults():
    rig = Rig()
    rig.el.faults["volts"] = 4.2
    rig.run(2)
    assert rig.mode == modes.FAULT
    assert "4.2" in rig.tracker.latch.reason


def test_an_unplugged_limit_switch_faults_an_idle_machine():
    rig = Rig()
    rig.sensors.limits[0].forced = True
    rig.run(2)
    assert rig.mode == modes.FAULT


def test_a_failing_control_loop_latches_a_fault():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(3)
    rig.tracker.software_fault("simulated crash")
    assert rig.mode == modes.FAULT
    with pytest.raises(CommandRejected):
        rig.tracker.request_mode(modes.TRACK)


# --- E-stop -------------------------------------------------------------------


def test_estop_latches_and_needs_both_release_and_clear():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(3)
    rig.sensors.estop.pressed = True
    rig.run(1)
    assert rig.mode == modes.ESTOP
    assert not rig.az.torque_on

    with pytest.raises(CommandRejected, match="release"):
        rig.tracker.clear_fault()

    rig.sensors.estop.pressed = False
    rig.run(3)
    assert rig.mode == modes.ESTOP  # releasing the button alone is not enough
    rig.tracker.clear_fault()
    assert rig.mode == modes.IDLE


def test_estop_refuses_every_mode_request():
    rig = Rig()
    rig.sensors.estop.pressed = True
    rig.run(1)
    for mode in (modes.TRACK, modes.MANUAL, modes.STOW, modes.IDLE):
        with pytest.raises(CommandRejected):
            rig.tracker.request_mode(mode)


# --- comms loss ---------------------------------------------------------------


def test_losing_the_phone_does_not_stop_tracking():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(5)
    rig.run(20, dt=60.0, touch=False)  # twenty minutes of silence
    assert rig.mode == modes.TRACK
    assert "comms" in [t["rule"] for t in rig.tracker.status()["trips"]]


def test_unattended_hazard_skips_defocus_and_stows():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(5)
    rig.run(20, dt=60.0, touch=False)
    rig.modes_seen.clear()
    rig.sensors.imu.tilt_deg = 8.0
    rig.run(5, touch=False)
    assert modes.DEFOCUS not in rig.modes_seen
    assert rig.mode == modes.STOW


# --- manual jog and capture ----------------------------------------------------


def test_jog_requires_manual_mode():
    rig = Rig()
    with pytest.raises(CommandRejected, match="manual"):
        asyncio.run(rig.tracker.jog(0, delta_deg=5.0))


def test_jog_moves_one_axis():
    rig = Rig()
    rig.run(1)
    rig.tracker.request_mode(modes.MANUAL)
    start = rig.az.true_mech_deg()
    asyncio.run(rig.tracker.jog(0, delta_deg=5.0))
    rig.run(3)
    assert rig.az.true_mech_deg() == pytest.approx(start + 5.0, abs=0.01)


def test_jog_into_a_keepout_zone_is_refused_before_moving():
    rig = Rig()
    rig.run(1)  # establishes the sun position
    rig.tracker.request_mode(modes.MANUAL)
    az0, el0 = rig.az.true_mech_deg(), rig.el.true_mech_deg()
    # Work out where the beam WOULD land after the jog, and forbid exactly that.
    s = geo.sun_vector(*rig.tracker._sun)
    beam = geo.azel_from_vector(
        geo.beam_direction(s, rig.tracker.mount.normal_from_joints(az0, el0 + 30.0))
    )
    rig.cfg["safety"]["keepout"] = [
        {"az_min": beam[0] - 3, "az_max": beam[0] + 3,
         "el_min": beam[1] - 3, "el_max": beam[1] + 3, "note": "neighbour"}
    ]
    with pytest.raises(CommandRejected, match="neighbour"):
        asyncio.run(rig.tracker.jog(1, delta_deg=30.0))
    rig.run(3)
    assert rig.el.true_mech_deg() == pytest.approx(el0, abs=0.01)


def test_capture_current_aim_recovers_where_the_beam_lands():
    """Jog until the spot is where you want it, capture, and tracking holds it."""
    rig = Rig(rate=0.0)
    rig.run(1)
    rig.tracker.request_mode(modes.MANUAL)
    asyncio.run(rig.tracker.jog(0, abs_deg=200.0))
    asyncio.run(rig.tracker.jog(1, abs_deg=40.0))
    rig.run(5)

    captured_az, captured_el = rig.tracker.capture_target()
    s = geo.sun_vector(*rig.tracker._sun)
    wanted = geo.mirror_normal(s, geo.target_vector_from_azel(captured_az, captured_el))
    held_az, held_el = rig.az.true_mech_deg(), rig.el.true_mech_deg()
    actual = rig.tracker.mount.normal_from_joints(held_az, held_el)
    assert geo.angle_between_deg(wanted, actual) < 1e-6

    # Tracking the captured target must leave the mirror where it is.
    rig.tracker.request_mode(modes.IDLE)
    rig.tracker.request_mode(modes.TRACK)
    rig.run(5)
    assert rig.az.true_mech_deg() == pytest.approx(held_az, abs=1e-6)
    assert rig.el.true_mech_deg() == pytest.approx(held_el, abs=1e-6)


def test_capture_needs_the_sun_on_the_mirror():
    rig = Rig(epoch=BEFORE_DAWN - 3600)  # sun below the horizon
    rig.run(1)
    with pytest.raises(CommandRejected):
        rig.tracker.capture_target()


# --- reporting ----------------------------------------------------------------


def test_status_carries_what_the_dashboard_needs():
    rig = Rig()
    rig.tracker.request_mode(modes.TRACK)
    rig.run(5)
    status = rig.tracker.status()
    for key in ("mode", "intent", "latch", "time_valid", "sun", "beam", "efficiency",
                "axes", "trips", "allowed_modes"):
        assert key in status
    assert status["time_valid"]
    assert 0.0 < status["efficiency"] <= 1.0


def test_telemetry_is_bounded():
    rig = Rig()
    rig.run(500)
    assert len(rig.tracker.telemetry) == rig.tracker.telemetry.capacity
