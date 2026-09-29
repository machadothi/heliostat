"""The safety supervisor, rule by rule, with no hardware and no tracker.

Every rule is exercised twice -- once where it must fire, once where it must
stay quiet -- because a rule that fires too eagerly gets disabled by a
frustrated operator, and a disabled rule protects nobody.
"""

import copy

import pytest
from config import DEFAULTS
from control.safety import (
    DEFOCUS,
    DEFOCUS_STOW,
    ESTOP,
    FAULT,
    LOG,
    RULE_COMMS,
    RULE_ESTOP,
    RULE_KEEPOUT,
    RULE_LIMIT,
    RULE_NO_TIME,
    RULE_SERVO,
    RULE_SUN_LOW,
    RULE_TILT,
    RULE_UNREACHABLE,
    RULE_WEATHER,
    STOW,
    PressureTrend,
    Supervisor,
    Trip,
)

WINDOW = {"az_min": 250.0, "az_max": 290.0, "el_min": 0.0, "el_max": 30.0, "note": "kitchen"}


def _cfg(**overrides):
    cfg = copy.deepcopy(DEFAULTS["safety"])
    cfg["keepout"] = [WINDOW]
    cfg.update(overrides)
    return cfg


def _servo(**overrides):
    state = {
        "name": "az",
        "family": "STS",
        "ok": True,
        "temp_c": 30,
        "volts": 12.3,
        "load": 0,
    }
    state.update(overrides)
    return state


def _snap(**overrides):
    """A healthy machine, tracking at midday. Every test perturbs one thing."""
    snap = {
        "mode": "track",
        "intent": "track",
        "time_valid": True,
        "estop": False,
        "sun_el": 45.0,
        "unreachable": False,
        "unreachable_reason": None,
        "beam": (180.0, 10.0),
        "servos": [_servo(), _servo(name="el", family="SCS", volts=9.9)],
        "limit_tripped": False,
        "tilt_deg": 0.2,
        "accel_g": 1.0,
        "pressure_trend": 0.0,
        "wind_kph": None,
        "comms_age_s": 5.0,
    }
    snap.update(overrides)
    return snap


def _rules(trips):
    return [trip.rule for trip in trips]


@pytest.fixture
def supervisor():
    return Supervisor(_cfg())


def test_a_healthy_machine_trips_nothing(supervisor):
    assert supervisor.evaluate(_snap()) == []


# --- 1. E-stop ------------------------------------------------------------------


def test_estop_latches_the_most_severe_response(supervisor):
    trips = supervisor.evaluate(_snap(estop=True))
    trip = next(t for t in trips if t.rule == RULE_ESTOP)
    assert trip.action == ESTOP
    assert trip.latch


def test_estop_outranks_everything(supervisor):
    trips = supervisor.evaluate(
        _snap(estop=True, tilt_deg=20.0, sun_el=1.0, limit_tripped=True)
    )
    assert supervisor.governing(trips).rule == RULE_ESTOP


# --- 2. no valid time ------------------------------------------------------------


def test_no_time_refuses_to_track(supervisor):
    trips = supervisor.evaluate(_snap(time_valid=False, sun_el=None, beam=None))
    trip = next(t for t in trips if t.rule == RULE_NO_TIME)
    assert trip.action == STOW
    assert not trip.latch  # it clears by itself once the phone sends the time


def test_no_time_is_irrelevant_when_nobody_wants_tracking(supervisor):
    """At boot, idle, with no time yet: nothing is wrong."""
    trips = supervisor.evaluate(
        _snap(mode="idle", intent="idle", time_valid=False, sun_el=None, beam=None)
    )
    assert RULE_NO_TIME not in _rules(trips)


def test_no_time_still_fires_while_stowed_with_tracking_intent(supervisor):
    """Otherwise the machine would 'resume' into tracking without a clock."""
    trips = supervisor.evaluate(
        _snap(mode="stow", intent="track", time_valid=False, sun_el=None, beam=None)
    )
    assert RULE_NO_TIME in _rules(trips)


# --- 3. sun too low --------------------------------------------------------------


def test_low_sun_stows_without_latching(supervisor):
    trips = supervisor.evaluate(_snap(sun_el=3.0))
    trip = next(t for t in trips if t.rule == RULE_SUN_LOW)
    assert trip.action == STOW
    assert not trip.latch  # tomorrow morning it must start again unaided


def test_sun_just_above_the_limit_is_fine(supervisor):
    assert RULE_SUN_LOW not in _rules(supervisor.evaluate(_snap(sun_el=5.01)))


def test_low_sun_does_not_move_an_idle_machine(supervisor):
    trips = supervisor.evaluate(_snap(mode="idle", intent="idle", sun_el=-10.0, beam=None))
    assert RULE_SUN_LOW not in _rules(trips)


# --- 4. unreachable target -------------------------------------------------------


def test_unreachable_target_stows_and_carries_its_reason(supervisor):
    trips = supervisor.evaluate(
        _snap(unreachable=True, unreachable_reason="normal in the dead zone")
    )
    trip = next(t for t in trips if t.rule == RULE_UNREACHABLE)
    assert trip.action == STOW
    assert not trip.latch
    assert "dead zone" in trip.reason


# --- 5. keep-out -----------------------------------------------------------------


def test_beam_in_a_keepout_zone_defocuses_then_stows_and_latches(supervisor):
    trips = supervisor.evaluate(_snap(beam=(270.0, 15.0)))
    trip = next(t for t in trips if t.rule == RULE_KEEPOUT)
    assert trip.action == DEFOCUS_STOW
    assert trip.latch
    assert "kitchen" in trip.reason


def test_keepout_ignores_a_beam_outside_every_zone(supervisor):
    assert RULE_KEEPOUT not in _rules(supervisor.evaluate(_snap(beam=(180.0, 15.0))))


def test_keepout_is_skipped_when_there_is_no_beam(supervisor):
    """No beam -- sun behind the mirror, or mid-slew -- means nothing to check."""
    assert RULE_KEEPOUT not in _rules(supervisor.evaluate(_snap(beam=None)))


# --- 6. servo faults -------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "fragment"),
    [
        ({"ok": False}, "not responding"),
        ({"temp_c": 80}, "over temperature"),
        ({"load": 950}, "load"),
        ({"load": -950}, "load"),  # load is signed; either direction counts
    ],
)
def test_servo_faults_latch(supervisor, fault, fragment):
    trips = supervisor.evaluate(_snap(servos=[_servo(**fault), _servo(name="el")]))
    trip = next(t for t in trips if t.rule == RULE_SERVO)
    assert trip.action == FAULT
    assert trip.latch
    assert fragment in trip.reason


def test_a_sagging_rail_is_rejected(supervisor):
    """A supply collapsing under load -- the brownout seen on the bench."""
    sagging = _servo(name="el", family="SCS", volts=4.2)
    trips = supervisor.evaluate(_snap(servos=[_servo(), sagging]))
    trip = next(t for t in trips if t.rule == RULE_SERVO)
    assert "4.2 V" in trip.reason


def test_the_bench_voltages_are_accepted(supervisor):
    """What the driver board actually supplies must not trip anything."""
    servos = [_servo(volts=12.3), _servo(name="el", family="SCS", volts=9.9)]
    assert RULE_SERVO not in _rules(supervisor.evaluate(_snap(servos=servos)))


def test_st3020_at_its_normal_12v_is_fine(supervisor):
    trips = supervisor.evaluate(_snap(servos=[_servo(volts=12.3), _servo(family="SCS", volts=9.9)]))
    assert RULE_SERVO not in _rules(trips)


def test_servo_silence_under_estop_is_expected(supervisor):
    """The relay has cut servo power; a missing reply is not a new fault."""
    trips = supervisor.evaluate(_snap(mode="estop", estop=True, servos=[_servo(ok=False)]))
    assert RULE_SERVO not in _rules(trips)


# --- 7. limit switches -----------------------------------------------------------


def test_an_open_limit_switch_faults(supervisor):
    trip = next(t for t in supervisor.evaluate(_snap(limit_tripped=True)) if t.rule == RULE_LIMIT)
    assert trip.action == FAULT
    assert trip.latch


def test_an_open_limit_faults_even_while_idle(supervisor):
    """A cut wire reads as open. It must fault an idle machine, not wait for a move."""
    trips = supervisor.evaluate(_snap(mode="idle", intent="idle", beam=None, limit_tripped=True))
    assert RULE_LIMIT in _rules(trips)


def test_homing_is_allowed_to_touch_the_switches(supervisor):
    trips = supervisor.evaluate(_snap(mode="home", intent="home", beam=None, limit_tripped=True))
    assert RULE_LIMIT not in _rules(trips)


# --- 8. tilt and shock -----------------------------------------------------------


def test_a_tilted_base_defocuses_stows_and_latches(supervisor):
    trip = next(t for t in supervisor.evaluate(_snap(tilt_deg=7.5)) if t.rule == RULE_TILT)
    assert trip.action == DEFOCUS_STOW
    assert trip.latch


def test_a_shock_trips_the_same_rule(supervisor):
    trips = supervisor.evaluate(_snap(accel_g=2.4))
    assert RULE_TILT in _rules(trips)


def test_small_tilt_and_normal_gravity_are_fine(supervisor):
    assert RULE_TILT not in _rules(supervisor.evaluate(_snap(tilt_deg=4.9, accel_g=1.1)))


# --- 9. weather ------------------------------------------------------------------


def test_a_falling_barometer_stows(supervisor):
    trip = next(
        t for t in supervisor.evaluate(_snap(pressure_trend=-3.0)) if t.rule == RULE_WEATHER
    )
    assert trip.action == STOW
    assert not trip.latch


def test_a_rising_barometer_is_fine(supervisor):
    assert RULE_WEATHER not in _rules(supervisor.evaluate(_snap(pressure_trend=+4.0)))


def test_high_wind_stows(supervisor):
    assert RULE_WEATHER in _rules(supervisor.evaluate(_snap(wind_kph=55.0)))


def test_no_anemometer_means_no_wind_rule(supervisor):
    assert RULE_WEATHER not in _rules(supervisor.evaluate(_snap(wind_kph=None)))


# --- 10. comms loss --------------------------------------------------------------


def test_comms_loss_is_logged_but_not_acted_on(supervisor):
    """A correctly tracking heliostat is safe. Stopping it because the phone
    walked out of range would freeze the mirror while the sun kept moving."""
    trips = supervisor.evaluate(_snap(comms_age_s=5000.0))
    trip = next(t for t in trips if t.rule == RULE_COMMS)
    assert trip.action == LOG
    assert supervisor.governing(trips) is None


def test_comms_loss_escalates_other_trips_straight_to_stow(supervisor):
    """With nobody watching, skip defocus and park."""
    trips = supervisor.evaluate(_snap(comms_age_s=5000.0, tilt_deg=9.0))
    governing = supervisor.governing(trips, comms_lost=True)
    assert governing.action == STOW
    assert governing.latch  # escalation must not lose the latch
    assert "unattended" in governing.reason


def test_comms_escalation_does_not_soften_a_fault(supervisor):
    trips = supervisor.evaluate(_snap(comms_age_s=5000.0, limit_tripped=True))
    assert supervisor.governing(trips, comms_lost=True).action == FAULT


# --- choosing the governing trip --------------------------------------------------


def test_the_most_severe_action_governs(supervisor):
    trips = supervisor.evaluate(_snap(sun_el=2.0, tilt_deg=9.0, limit_tripped=True))
    assert supervisor.governing(trips).action == FAULT


def test_a_latching_trip_beats_an_equal_nonlatching_one():
    """A hazard must never be masked by an environmental response of equal rank."""
    environmental = Trip("weather", STOW, False, "storm")
    hazard = Trip("x", STOW, True, "hazard")
    supervisor = Supervisor(_cfg())
    assert supervisor.governing([environmental, hazard]) is hazard
    assert supervisor.governing([hazard, environmental]) is hazard


def test_nothing_governs_an_empty_list(supervisor):
    assert supervisor.governing([]) is None


def test_actions_are_ordered_by_severity():
    assert LOG < DEFOCUS < STOW < DEFOCUS_STOW < FAULT < ESTOP


# --- pressure trend -------------------------------------------------------------


def test_trend_needs_enough_history_before_it_speaks():
    trend = PressureTrend(window_s=1800, min_span_s=600, sample_every_s=60)
    trend.add(0.0, 1013.0)
    trend.add(60.0, 1012.9)
    assert trend.hpa_per_hour() is None  # a minute of data is noise, not weather


def test_trend_reports_hpa_per_hour():
    trend = PressureTrend(window_s=1800, min_span_s=600, sample_every_s=60)
    for minute in range(0, 31):
        trend.add(minute * 60.0, 1013.0 - minute * 0.05)  # -3 hPa/h
    assert trend.hpa_per_hour() == pytest.approx(-3.0, abs=0.01)


def test_trend_samples_are_rate_limited():
    """Reading every second must not flood the buffer and shorten the window."""
    trend = PressureTrend(window_s=1800, min_span_s=600, sample_every_s=60)
    for second in range(0, 1200):
        trend.add(float(second), 1013.0)
    assert len(trend._samples) <= 21
