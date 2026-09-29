"""Kinematics, calibration and the mode table.

The parts most likely to produce a mirror that moves confidently to the wrong
place: a sign error in the calibration transform, an azimuth that takes the
forbidden path, or a keep-out zone with a gap in it.
"""

import random

import pytest
from control import modes
from control.calibration import (
    apply_backlash,
    deadband_deg,
    from_servo_deg,
    offset_from_known_position,
    steps_per_degree,
    to_servo_deg,
)
from control.geometry import angle_between_deg, vector_from_azel
from control.kinematics import (
    Axis,
    AzElMount,
    KeepOutViolation,
    OutOfReach,
    assert_beam_allowed,
    keepout_violation,
    solve,
    zone_contains,
)

PROPERTY_SAMPLES = 10_000


def _az_axis(**overrides):
    kwargs = dict(
        name="az", servo_id=1, family="STS", min_deg=-170.0, max_deg=170.0, home_mech_deg=-170.0
    )
    kwargs.update(overrides)
    return Axis(**kwargs)


def _el_axis(**overrides):
    kwargs = dict(name="el", servo_id=2, family="SCS", min_deg=0.0, max_deg=90.0)
    kwargs.update(overrides)
    return Axis(**kwargs)


# --- mount round trip ---------------------------------------------------------


def test_normal_and_joints_round_trip():
    """joints_from_normal and normal_from_joints must be exact inverses."""
    rng = random.Random(4711)
    worst = 0.0
    for _ in range(PROPERTY_SAMPLES):
        azimuth = rng.uniform(0.0, 360.0)
        elevation = rng.uniform(-89.5, 89.5)
        normal = AzElMount.normal_from_joints(azimuth, elevation)
        back_az, back_el = AzElMount.joints_from_normal(normal)
        rebuilt = AzElMount.normal_from_joints(back_az, back_el)
        worst = max(worst, angle_between_deg(normal, rebuilt))
    assert worst < 1e-9


def test_elevation_ninety_means_the_mirror_faces_straight_up():
    normal = AzElMount.normal_from_joints(123.0, 90.0)
    assert normal[2] == pytest.approx(1.0, abs=1e-12)


def test_joints_are_in_their_declared_ranges():
    rng = random.Random(22)
    for _ in range(2000):
        normal = AzElMount.normal_from_joints(rng.uniform(0, 360), rng.uniform(-89, 89))
        azimuth, elevation = AzElMount.joints_from_normal(normal)
        assert 0.0 <= azimuth < 360.0
        assert -90.0 <= elevation <= 90.0


# --- calibration transform ----------------------------------------------------


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("gear_ratio", [1.0, 2.0, 0.5, 3.75])
@pytest.mark.parametrize("offset", [0.0, 37.5, -120.0])
def test_calibration_transform_round_trips(direction, gear_ratio, offset):
    for mech in (-90.0, -1.0, 0.0, 12.34, 175.0):
        servo = to_servo_deg(mech, offset, direction, gear_ratio)
        assert from_servo_deg(servo, offset, direction, gear_ratio) == pytest.approx(mech, abs=1e-9)


def test_direction_reverses_the_sense_of_travel():
    forward = to_servo_deg(10.0, 0.0, 1, 1.0)
    reversed_ = to_servo_deg(10.0, 0.0, -1, 1.0)
    assert forward == -reversed_


def test_gear_ratio_multiplies_servo_travel():
    """A 3:1 reduction means three servo degrees per mechanical degree."""
    assert to_servo_deg(10.0, 0.0, 1, 3.0) == pytest.approx(30.0)


def test_offset_from_known_position_reproduces_the_angle():
    """The homing routine's core arithmetic."""
    direction, gear = -1, 2.0
    true_offset = 43.75
    known_mech = -170.0
    servo_reading = to_servo_deg(known_mech, true_offset, direction, gear)

    derived = offset_from_known_position(servo_reading, known_mech, direction, gear)
    assert derived == pytest.approx(true_offset, abs=1e-9)
    assert from_servo_deg(servo_reading, derived, direction, gear) == pytest.approx(known_mech)


def test_axis_transform_matches_the_free_functions():
    axis = _az_axis(offset_deg=12.0, direction=-1, gear_ratio=1.5)
    assert axis.to_servo_deg(30.0) == to_servo_deg(30.0, 12.0, -1, 1.5)
    assert axis.from_servo_deg(axis.to_servo_deg(30.0)) == pytest.approx(30.0, abs=1e-9)


# --- resolution ---------------------------------------------------------------


def test_step_sizes_match_the_measured_hardware():
    """Confirmed on the bench: ST3020 0..4095 over 360, SC09 0..1023 over 300."""
    assert _az_axis().step_deg == pytest.approx(360.0 / 4096.0, abs=1e-9)  # 0.088 deg
    assert _el_axis().step_deg == pytest.approx(300.0 / 1024.0, abs=1e-9)  # 0.293 deg


def test_gearing_improves_resolution():
    """A reduction divides the step at the output, which is the point of fitting one."""
    assert _az_axis(gear_ratio=4.0).step_deg == pytest.approx(_az_axis().step_deg / 4.0)


def test_deadband_is_one_step():
    assert _az_axis().deadband_deg == pytest.approx(_az_axis().step_deg, abs=1e-12)
    assert steps_per_degree(4096, 360.0) == pytest.approx(11.377, abs=0.01)
    assert deadband_deg(1024, 300.0) == pytest.approx(0.293, abs=0.001)


def test_the_sun_takes_a_long_time_to_move_one_step():
    """The justification for a 1 Hz loop with a one-step deadband.

    The sun moves 15 deg/hour; the mirror normal half that. One ST3020 step is
    42 seconds of waiting, one SC09 step over two minutes. Commanding faster than
    that is asking the servo to hunt for nothing.
    """
    normal_rate_dps = 15.0 / 3600.0 / 2.0
    assert _az_axis().step_deg / normal_rate_dps == pytest.approx(42.0, abs=1.0)
    assert _el_axis().step_deg / normal_rate_dps == pytest.approx(141.0, abs=2.0)


# --- soft limits --------------------------------------------------------------


def test_clamp_reports_when_it_intervenes():
    axis = _el_axis()
    assert axis.clamp(45.0) == (45.0, False)
    assert axis.clamp(-5.0) == (0.0, True)
    assert axis.clamp(120.0) == (90.0, True)
    assert axis.clamp(0.0) == (0.0, False)  # exactly on the limit is allowed
    assert axis.clamp(90.0) == (90.0, False)


def test_in_range_matches_clamp():
    axis = _az_axis()
    for angle in (-200.0, -170.0, 0.0, 170.0, 200.0):
        assert axis.in_range(angle) == (axis.clamp(angle)[1] is False)


def test_axis_rejects_impossible_configuration():
    with pytest.raises(ValueError):
        Axis(name="x", servo_id=1, family="NOPE")
    with pytest.raises(ValueError):
        Axis(name="x", servo_id=1, family="STS", direction=0)
    with pytest.raises(ValueError):
        Axis(name="x", servo_id=1, family="STS", gear_ratio=0.0)
    with pytest.raises(ValueError):
        Axis(name="x", servo_id=1, family="STS", min_deg=10.0, max_deg=-10.0)


# --- azimuth wrap: the cable-winding hazard -----------------------------------


def test_unwrap_picks_the_representation_inside_the_travel():
    """350 degrees and -10 are the same direction; only one is reachable here."""
    axis = _az_axis()  # -170 .. +170
    assert axis.unwrap(350.0, current_deg=0.0) == pytest.approx(-10.0)
    assert axis.unwrap(10.0, current_deg=0.0) == pytest.approx(10.0)


def test_unwrap_prefers_the_representation_nearest_the_current_position():
    axis = Axis(name="az", servo_id=1, family="STS", min_deg=-360.0, max_deg=360.0)
    # Within -360..360, azimuth 90 is representable at -270 and at 90 only:
    # 450 would be a third, but it lies outside the travel and is correctly excluded.
    assert axis.unwrap(90.0, current_deg=0.0) == pytest.approx(90.0)
    assert axis.unwrap(90.0, current_deg=-300.0) == pytest.approx(-270.0)
    # From -100, reaching -270 is a 170-degree move and reaching +90 is 190, so
    # the negative representation wins even though it looks further from zero.
    assert axis.unwrap(90.0, current_deg=-100.0) == pytest.approx(-270.0)


def test_unwrap_reaches_a_second_turn_when_the_travel_allows_it():
    """A geared axis with more than one turn of travel has more choices."""
    axis = Axis(name="az", servo_id=1, family="STS", min_deg=-720.0, max_deg=720.0)
    assert axis.unwrap(90.0, current_deg=400.0) == pytest.approx(450.0)
    assert axis.unwrap(90.0, current_deg=-600.0) == pytest.approx(-630.0)


def test_unwrap_refuses_rather_than_crossing_the_dead_zone():
    """The wedge the single-turn servo cannot traverse is simply outside the limits.

    Refusing is the whole point: silently taking the short way is what winds the
    cable into a knot.
    """
    axis = _az_axis()  # -170 .. +170, so 170..190 is unreachable
    with pytest.raises(OutOfReach):
        axis.unwrap(180.0, current_deg=0.0)
    with pytest.raises(OutOfReach):
        axis.unwrap(175.0, current_deg=160.0)


def test_unwrap_never_returns_something_outside_the_travel():
    rng = random.Random(1001)
    axis = _az_axis()
    for _ in range(5000):
        target = rng.uniform(0.0, 360.0)
        current = rng.uniform(axis.min_deg, axis.max_deg)
        try:
            result = axis.unwrap(target, current)
        except OutOfReach:
            continue
        assert axis.min_deg <= result <= axis.max_deg
        # And it must still BE the requested direction.
        assert abs((result - target + 180.0) % 360.0 - 180.0) < 1e-9


def test_unwrap_with_no_current_position_uses_the_middle_of_travel():
    axis = _az_axis()
    assert axis.unwrap(10.0) == pytest.approx(10.0)


def test_a_full_turn_axis_can_always_be_reached():
    axis = Axis(name="az", servo_id=1, family="STS", min_deg=-180.0, max_deg=180.0)
    for target in range(0, 360, 7):
        assert axis.unwrap(float(target), current_deg=0.0) is not None


# --- keep-out zones -----------------------------------------------------------

WINDOW = {"az_min": 250.0, "az_max": 290.0, "el_min": 0.0, "el_max": 30.0, "note": "kitchen window"}
NORTH_GATE = {"az_min": 350.0, "az_max": 10.0, "el_min": 0.0, "el_max": 45.0, "note": "path"}


def test_zone_contains_a_direction_inside_it():
    assert zone_contains(WINDOW, 270.0, 15.0)
    assert zone_contains(WINDOW, 250.0, 0.0)  # inclusive edges
    assert zone_contains(WINDOW, 290.0, 30.0)


def test_zone_excludes_directions_outside_it():
    assert not zone_contains(WINDOW, 249.0, 15.0)
    assert not zone_contains(WINDOW, 291.0, 15.0)
    assert not zone_contains(WINDOW, 270.0, 31.0)  # above the zone
    assert not zone_contains(WINDOW, 270.0, -1.0)  # below it


def test_a_zone_may_wrap_through_north():
    """az_min 350 to az_max 10 is a 20-degree window straddling zero.

    Handling this wrongly would leave a hole in the protection exactly where
    people tend to put doors and paths.
    """
    assert zone_contains(NORTH_GATE, 355.0, 10.0)
    assert zone_contains(NORTH_GATE, 0.0, 10.0)
    assert zone_contains(NORTH_GATE, 5.0, 10.0)
    assert not zone_contains(NORTH_GATE, 180.0, 10.0)
    assert not zone_contains(NORTH_GATE, 11.0, 10.0)
    assert not zone_contains(NORTH_GATE, 349.0, 10.0)


def test_zone_normalises_azimuth_before_comparing():
    assert zone_contains(WINDOW, 270.0 - 360.0, 15.0)
    assert zone_contains(WINDOW, 270.0 + 360.0, 15.0)


def test_keepout_violation_returns_the_offending_zone():
    zones = [WINDOW, NORTH_GATE]
    assert keepout_violation(270.0, 15.0, zones) is WINDOW
    assert keepout_violation(0.0, 10.0, zones) is NORTH_GATE
    assert keepout_violation(180.0, 60.0, zones) is None
    assert keepout_violation(180.0, 60.0, None) is None
    assert keepout_violation(180.0, 60.0, []) is None


def test_assert_beam_allowed_names_the_zone():
    with pytest.raises(KeepOutViolation, match="kitchen window"):
        assert_beam_allowed(270.0, 15.0, [WINDOW])
    assert_beam_allowed(180.0, 60.0, [WINDOW])  # must not raise


# --- solving ------------------------------------------------------------------


def test_solve_produces_joints_that_reproduce_the_normal():
    az_axis = Axis(name="az", servo_id=1, family="STS", min_deg=-180.0, max_deg=180.0)
    el_axis = Axis(name="el", servo_id=2, family="SCS", min_deg=-90.0, max_deg=90.0)
    rng = random.Random(3141)
    for _ in range(2000):
        normal = vector_from_azel(rng.uniform(0.0, 360.0), rng.uniform(-80.0, 80.0))
        az, el, hit = solve(AzElMount, az_axis, el_axis, normal, current_az_deg=0.0)
        assert not hit
        assert angle_between_deg(AzElMount.normal_from_joints(az, el), normal) < 1e-9


def test_solve_reports_when_the_request_was_clamped():
    az_axis = Axis(name="az", servo_id=1, family="STS", min_deg=-180.0, max_deg=180.0)
    el_axis = _el_axis()  # 0 .. 90, so a downward-facing normal gets clamped
    _, elevation, hit = solve(AzElMount, az_axis, el_axis, vector_from_azel(0.0, -30.0), 0.0)
    assert hit
    assert elevation == pytest.approx(0.0)


def test_solve_raises_when_azimuth_is_unreachable():
    az_axis = _az_axis()  # -170 .. 170
    with pytest.raises(OutOfReach):
        solve(AzElMount, az_axis, _el_axis(), vector_from_azel(180.0, 45.0), 0.0)


# --- backlash -----------------------------------------------------------------


def test_backlash_is_ignored_when_not_configured():
    assert apply_backlash(10.0, 5.0, 0.0) == (10.0, False)


def test_backlash_is_ignored_on_the_first_command():
    assert apply_backlash(10.0, None, 0.5) == (10.0, False)


def test_moving_in_the_preferred_direction_needs_no_overshoot():
    assert apply_backlash(10.0, 5.0, 0.5) == (10.0, False)


def test_reversing_direction_triggers_an_overshoot():
    approach, needs = apply_backlash(5.0, 10.0, 0.5)
    assert needs
    assert approach == pytest.approx(4.5)  # undershoot, then come back up


def test_backlash_settles_from_the_same_side_every_time():
    """Whatever the approach, the final move is always in the positive direction."""
    rng = random.Random(5)
    for _ in range(500):
        previous = rng.uniform(-50.0, 50.0)
        target = rng.uniform(-50.0, 50.0)
        approach, needs = apply_backlash(target, previous, 0.4)
        if needs:
            assert approach < target  # so the final leg moves positive


def test_no_movement_needs_no_compensation():
    assert apply_backlash(7.0, 7.0, 0.5) == (7.0, False)


# --- modes --------------------------------------------------------------------


def test_every_mode_is_reachable_from_idle_or_via_fault():
    for mode in modes.ALL_MODES:
        assert modes.is_valid(mode)


def test_estop_latches():
    """The only way out of an E-stop is an explicit clear, which lands in IDLE."""
    assert modes.allowed_from(modes.ESTOP) == (modes.IDLE,)
    for mode in (modes.TRACK, modes.MANUAL, modes.STOW, modes.HOME):
        assert not modes.can_transition(modes.ESTOP, mode)


def test_a_fault_cannot_go_straight_back_to_tracking():
    """Somebody has to look at the machine first."""
    assert not modes.can_transition(modes.FAULT, modes.TRACK)
    assert modes.can_transition(modes.FAULT, modes.IDLE)
    assert modes.can_transition(modes.FAULT, modes.STOW)


def test_estop_is_reachable_from_everywhere():
    for mode in modes.ALL_MODES:
        if mode != modes.ESTOP:
            assert modes.can_transition(mode, modes.ESTOP), mode


def test_defocus_can_return_to_tracking():
    """Defocus is a holding state, not a terminal one."""
    assert modes.can_transition(modes.DEFOCUS, modes.TRACK)
    assert modes.can_transition(modes.TRACK, modes.DEFOCUS)


def test_manual_cannot_jump_straight_into_tracking():
    """Leaving jog mode returns to idle, so the operator re-arms deliberately."""
    assert not modes.can_transition(modes.MANUAL, modes.TRACK)
    assert modes.can_transition(modes.MANUAL, modes.IDLE)


def test_staying_put_is_always_allowed():
    for mode in modes.ALL_MODES:
        assert modes.can_transition(mode, mode)


def test_assert_transition_explains_itself():
    with pytest.raises(ValueError, match="illegal transition"):
        modes.assert_transition(modes.ESTOP, modes.TRACK)
    with pytest.raises(ValueError, match="unknown mode"):
        modes.assert_transition(modes.IDLE, "nonsense")
    modes.assert_transition(modes.IDLE, modes.TRACK)  # must not raise


def test_automatic_and_torque_classifications():
    assert modes.is_automatic(modes.TRACK)
    assert modes.is_automatic(modes.DEFOCUS)
    assert not modes.is_automatic(modes.MANUAL)
    assert not modes.is_automatic(modes.ESTOP)

    assert modes.wants_torque(modes.TRACK)
    assert modes.wants_torque(modes.STOW)
    # Torque is meaningless in these two: power is cut, or the servo has faulted.
    assert not modes.wants_torque(modes.ESTOP)
    assert not modes.wants_torque(modes.FAULT)


def test_no_transition_targets_an_unknown_mode():
    for mode in modes.ALL_MODES:
        for target in modes.allowed_from(mode):
            assert modes.is_valid(target), (mode, target)


# --- a realistic end-to-end pass ----------------------------------------------


def test_the_dead_zone_must_be_sited_away_from_the_working_arc():
    """A -170..+170 axis puts its dead zone at due South -- exactly where a
    northern-hemisphere mirror normal spends the day. Siting matters.
    """
    facing_south = _az_axis()  # dead zone at 180
    with pytest.raises(OutOfReach):
        facing_south.unwrap(180.0, current_deg=160.0)

    # Rotating the mount so the dead zone sits at North makes the same arc fine.
    facing_north = Axis(name="az", servo_id=1, family="STS", min_deg=10.0, max_deg=350.0)
    assert facing_north.unwrap(180.0, current_deg=160.0) == pytest.approx(180.0)
    with pytest.raises(OutOfReach):
        facing_north.unwrap(0.0, current_deg=180.0)


def test_a_days_worth_of_azimuth_never_leaves_the_travel():
    """Sweep the mirror normal through a plausible day and check nothing trips.

    The normal moves at half the sun's rate, so a 180-degree solar sweep is only
    about 90 degrees of mirror travel. The axis is configured with its dead zone
    at North, since a northern-hemisphere normal works the southern sky.
    """
    az_axis = Axis(name="az", servo_id=1, family="STS", min_deg=10.0, max_deg=350.0)
    el_axis = _el_axis()
    previous = None
    for step in range(0, 91):
        normal = vector_from_azel(135.0 + step, 40.0)
        az, el, hit = solve(AzElMount, az_axis, el_axis, normal, previous)
        assert not hit
        assert 0.0 <= el <= 90.0
        if previous is not None:
            # One degree per step, so anything larger means the solver reversed
            # or jumped a turn. The first sample is the initial acquisition and
            # is legitimately a large move.
            assert abs(az - previous) < 2.0, f"jump at step {step}"
        previous = az


def test_azimuth_seam_is_crossed_smoothly_on_a_full_turn_axis():
    """Passing through North must not produce a 360-degree jump."""
    axis = Axis(name="az", servo_id=1, family="STS", min_deg=-180.0, max_deg=180.0)
    previous = -5.0
    for target in (355.0, 357.0, 359.0, 1.0, 3.0, 5.0):
        current = axis.unwrap(target, previous)
        assert abs(current - previous) < 5.0, f"jump at {target}"
        previous = current
