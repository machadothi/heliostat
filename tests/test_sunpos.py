"""Solar position, checked against NREL SPA via a frozen pvlib reference.

WHICH ERROR METRIC MATTERS
--------------------------
Azimuth on its own is a trap. Near the zenith it is ill-conditioned: when the sun
is almost overhead, a hair's movement swings the azimuth wildly, and comparing
azimuths there exaggerates a difference that barely exists physically.

What a heliostat actually consumes is the sun *vector*. So the primary assertion
is the angular separation between our unit sun vector and the reference's, which
stays meaningful everywhere including straight overhead. Azimuth is checked
separately, and only where it is well conditioned.
"""

import json
import math
from pathlib import Path

import pytest
from solar.julian import jdn_and_frac, julian_centuries, unix_from_civil
from solar.refraction import apparent_elevation, refraction_arcmin
from solar.sunpos import (
    equation_of_time,
    hour_angle,
    solar_noon_minutes,
    sun_declination,
    sun_position,
    true_solar_time,
)

REFERENCE = Path(__file__).resolve().parent.parent / "tools" / "reference" / "noaa_cases.json"

# The NOAA/Meeus algorithm is specified to about 0.01 deg. A heliostat doubles
# any pointing error at the beam, so 0.02 deg here is 0.04 deg of beam error --
# still an order of magnitude inside the sun's own 0.53 deg disc.
VECTOR_TOLERANCE_DEG = 0.02

# Azimuth is only well conditioned away from the zenith.
AZIMUTH_TOLERANCE_DEG = 0.02
MIN_ZENITH_FOR_AZIMUTH_DEG = 5.0

# Refraction models differ more than the position algorithms do, so the bar near
# the horizon is honestly looser. It is also the regime the supervisor stows in.
APPARENT_TOLERANCE_DEG = 0.10


def _cases():
    data = json.loads(REFERENCE.read_text())
    return data["cases"]


def _unit_vector(azimuth_deg, elevation_deg):
    """ENU unit vector: x East, y North, z Up. Matches control/geometry."""
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return (math.cos(el) * math.sin(az), math.cos(el) * math.cos(az), math.sin(el))


def _angle_between(a, b):
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))
    return math.degrees(math.acos(dot))


CASES = _cases()
IDS = [c["label"] for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_sun_vector_matches_nrel_spa(case):
    """The assertion that actually governs where the beam lands."""
    azimuth, elevation, _ = sun_position(case["unix"], case["lat"], case["lon"])
    ours = _unit_vector(azimuth, elevation)
    theirs = _unit_vector(case["azimuth"], case["elevation"])
    separation = _angle_between(ours, theirs)
    assert separation < VECTOR_TOLERANCE_DEG, (
        f"{case['label']}: sun vector off by {separation:.4f} deg "
        f"({2 * separation:.4f} deg at the beam)"
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_true_elevation_matches_nrel_spa(case):
    _, elevation, _ = sun_position(case["unix"], case["lat"], case["lon"])
    assert abs(elevation - case["elevation"]) < VECTOR_TOLERANCE_DEG


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_azimuth_matches_where_well_conditioned(case):
    zenith = 90.0 - case["elevation"]
    if zenith < MIN_ZENITH_FOR_AZIMUTH_DEG:
        pytest.skip(f"zenith {zenith:.2f} deg - azimuth is ill-conditioned overhead")
    azimuth, _, _ = sun_position(case["unix"], case["lat"], case["lon"])
    # Compare across the 0/360 seam, not numerically.
    delta = abs((azimuth - case["azimuth"] + 180.0) % 360.0 - 180.0)
    assert delta < AZIMUTH_TOLERANCE_DEG


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_apparent_elevation_matches(case):
    _, _, apparent = sun_position(case["unix"], case["lat"], case["lon"])
    assert abs(apparent - case["apparent_elevation"]) < APPARENT_TOLERANCE_DEG


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_azimuth_is_always_in_range(case):
    azimuth, _, _ = sun_position(case["unix"], case["lat"], case["lon"])
    assert 0.0 <= azimuth < 360.0


# --- independent checks that share no failure mode with pvlib ---------------


@pytest.mark.parametrize(
    ("label", "date", "expected_decl"),
    [
        ("june solstice", (2024, 6, 20), 23.44),
        ("december solstice", (2024, 12, 21), -23.44),
    ],
)
def test_declination_reaches_the_obliquity_at_the_solstices(label, date, expected_decl):
    """The sun's declination extremes ARE the obliquity of the ecliptic."""
    jdn, frac = jdn_and_frac(unix_from_civil(*date, 12, 0, 0))
    assert sun_declination(julian_centuries(jdn, frac)) == pytest.approx(expected_decl, abs=0.02)


@pytest.mark.parametrize("date", [(2024, 3, 20), (2024, 9, 22)])
def test_declination_crosses_zero_at_the_equinoxes(date):
    jdn, frac = jdn_and_frac(unix_from_civil(*date, 12, 0, 0))
    assert abs(sun_declination(julian_centuries(jdn, frac))) < 0.6


def test_equation_of_time_hits_its_textbook_extremes():
    """Roughly -14.2 min in mid-February and +16.4 min in early November.

    These are published values anyone can look up, and they exercise a different
    part of the algorithm than declination does.
    """
    jdn, frac = jdn_and_frac(unix_from_civil(2024, 2, 11, 12, 0, 0))
    assert equation_of_time(julian_centuries(jdn, frac)) == pytest.approx(-14.2, abs=0.3)

    jdn, frac = jdn_and_frac(unix_from_civil(2024, 11, 3, 12, 0, 0))
    assert equation_of_time(julian_centuries(jdn, frac)) == pytest.approx(16.4, abs=0.3)


def test_solar_noon_is_near_1200_utc_at_greenwich():
    """At longitude 0 the sun transits within the equation of time of 12:00 UT."""
    for month in range(1, 13):
        minutes = solar_noon_minutes(unix_from_civil(2026, month, 15, 12, 0, 0), 0.0)
        assert abs(minutes - 720.0) < 17.0  # equation of time never exceeds ~16.5 min


def test_solar_noon_shifts_four_minutes_per_degree_of_longitude():
    epoch = unix_from_civil(2026, 6, 21, 12, 0, 0)
    at_greenwich = solar_noon_minutes(epoch, 0.0)
    at_15_east = solar_noon_minutes(epoch, 15.0)
    assert (at_greenwich - at_15_east) == pytest.approx(60.0, abs=0.01)


def test_hour_angle_is_zero_at_local_solar_noon():
    assert hour_angle(720.0) == pytest.approx(0.0)
    assert hour_angle(360.0) == pytest.approx(-90.0)  # six hours before noon
    # Midnight sits exactly on the seam; -180 and +180 are the same direction and
    # cos() cannot tell them apart, so either is correct.
    assert abs(hour_angle(0.0)) == pytest.approx(180.0)


def test_true_solar_time_wraps_within_a_day():
    for frac in (0.0, 0.25, 0.5, 0.75, 0.999):
        for lon in (-179.0, -90.0, 0.0, 90.0, 179.0):
            assert 0.0 <= true_solar_time(frac, 15.0, lon) < 1440.0


def test_sun_is_up_at_local_noon_and_down_at_local_midnight():
    lat, lon = 51.4779, 0.0
    _, noon_elevation, _ = sun_position(unix_from_civil(2026, 6, 21, 12, 0, 0), lat, lon)
    _, midnight_elevation, _ = sun_position(unix_from_civil(2026, 6, 21, 0, 0, 0), lat, lon)
    assert noon_elevation > 55.0
    assert midnight_elevation < 0.0


def test_elevation_varies_smoothly_through_a_day():
    """No discontinuities: successive samples must not jump."""
    lat, lon = 51.4779, -0.0015
    base = unix_from_civil(2026, 6, 21)
    previous = None
    for minute in range(0, 1440, 5):
        _, elevation, _ = sun_position(base + minute * 60, lat, lon)
        if previous is not None:
            assert abs(elevation - previous) < 1.5, f"jump at minute {minute}"
        previous = elevation


# --- refraction --------------------------------------------------------------


def test_refraction_is_largest_at_the_horizon_and_falls_away():
    at_horizon = refraction_arcmin(0.0)
    at_ten = refraction_arcmin(10.0)
    at_thirty = refraction_arcmin(30.0)

    # 29 arcmin (0.483 deg), not the 34 arcmin usually quoted. The two numbers
    # answer different questions: Saemundsson maps TRUE -> apparent, so this is
    # the lift applied to a sun geometrically ON the horizon. The familiar 34
    # arcmin is the reverse map, the correction at an APPARENT altitude of zero,
    # where the true altitude is already about -0.57 deg. Mixing them up is a
    # classic way to end up half a degree out at sunrise.
    assert at_horizon / 60.0 == pytest.approx(0.483, abs=0.02)
    assert at_ten / 60.0 == pytest.approx(0.09, abs=0.03)
    assert at_thirty / 60.0 == pytest.approx(0.03, abs=0.02)
    assert at_horizon > at_ten > at_thirty > 0.0


def test_refraction_always_lifts_the_sun():
    for elevation in (0.0, 1.0, 5.0, 20.0, 60.0, 89.0):
        assert apparent_elevation(elevation) >= elevation


def test_colder_denser_air_refracts_more():
    cold = refraction_arcmin(5.0, pressure_hpa=1030.0, temp_c=-10.0)
    hot = refraction_arcmin(5.0, pressure_hpa=990.0, temp_c=35.0)
    assert cold > hot


def test_refraction_vanishes_well_below_the_horizon():
    assert refraction_arcmin(-5.0) == 0.0
