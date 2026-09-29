"""Time handling, including the single-precision hazard it exists to defeat.

If these fail, nothing downstream can be trusted: an error in the time is an
error in the sun's position, and the solar math has no way to notice.
"""

import datetime

import numpy as np
import pytest
from solar.julian import (
    DAYS_PER_JULIAN_CENTURY,
    JDN_J2000,
    JDN_UNIX_EPOCH,
    jdn_and_frac,
    jdn_from_civil,
    julian_centuries,
    julian_day,
    unix_from_civil,
)

SECONDS_PER_DAY = 86400


@pytest.mark.parametrize(
    ("year", "month", "day", "expected"),
    [
        (1970, 1, 1, 2440588),  # Unix epoch
        (2000, 1, 1, 2451545),  # J2000.0
        (1901, 1, 1, 2415386),  # lower bound of the algorithm's stated range
        (2099, 12, 31, 2488069),  # upper bound
    ],
)
def test_known_julian_day_numbers(year, month, day, expected):
    assert jdn_from_civil(year, month, day) == expected


def test_jdn_matches_datetime_for_every_month():
    """January and February are where truncation-versus-floor division bites.

    The published Fliegel-Van Flandern formula assumes integer division that
    truncates toward zero; Python's // floors. Getting this wrong shifts those
    two months by exactly two days, which is subtle enough to survive a casual
    smoke test -- declination near a solstice barely moves over two days.
    """
    epoch = datetime.date(1970, 1, 1)
    for year in (1901, 1970, 1999, 2000, 2024, 2026, 2099):
        for month in range(1, 13):
            for day in (1, 15, 28):
                expected = JDN_UNIX_EPOCH + (datetime.date(year, month, day) - epoch).days
                assert jdn_from_civil(year, month, day) == expected, (year, month, day)


def test_j2000_is_exactly_2451545():
    """JD 2451545.0 is 2000-01-01 12:00 UT, by definition."""
    jdn, frac = jdn_and_frac(unix_from_civil(2000, 1, 1, 12, 0, 0))
    assert julian_day(jdn, frac) == pytest.approx(2451545.0, abs=1e-9)
    assert julian_centuries(jdn, frac) == pytest.approx(0.0, abs=1e-12)


def test_jdn_and_frac_splits_at_utc_midnight():
    jdn_midnight, frac_midnight = jdn_and_frac(unix_from_civil(2026, 6, 21, 0, 0, 0))
    jdn_noon, frac_noon = jdn_and_frac(unix_from_civil(2026, 6, 21, 12, 0, 0))

    assert jdn_midnight == jdn_noon == jdn_from_civil(2026, 6, 21)
    assert frac_midnight == pytest.approx(0.0)
    assert frac_noon == pytest.approx(0.5)


def test_frac_stays_in_unit_interval_across_a_year():
    start = unix_from_civil(2026, 1, 1)
    for offset in range(0, 366 * SECONDS_PER_DAY, 3607):  # a prime-ish stride
        _, frac = jdn_and_frac(start + offset)
        assert 0.0 <= frac < 1.0


def test_one_second_is_resolvable():
    """The whole point of the split: a second must remain visible."""
    base = unix_from_civil(2026, 6, 21, 12, 0, 0)
    _, frac_a = jdn_and_frac(base)
    _, frac_b = jdn_and_frac(base + 1)
    assert frac_b != frac_a
    assert (frac_b - frac_a) == pytest.approx(1.0 / SECONDS_PER_DAY, rel=1e-9)


def test_split_beats_the_naive_julian_day_under_single_precision():
    """Demonstrate the failure this module was designed around.

    Materialising a full Julian Day as a 32-bit float and subtracting J2000
    afterwards loses the time of day entirely -- the two large numbers are
    rounded to the same value long before the subtraction can help.

    Subtracting the integers FIRST, while they are still exact, keeps it.
    """
    jdn, frac = jdn_and_frac(unix_from_civil(2026, 6, 21, 12, 0, 0))
    one_hour_later = jdn_and_frac(unix_from_civil(2026, 6, 21, 13, 0, 0))

    # Naive: build the full JD, then round it to single precision.
    naive_a = np.float32(julian_day(jdn, frac))
    naive_b = np.float32(julian_day(*one_hour_later))
    assert naive_a == naive_b, "expected single precision to erase an hour here"

    # The split: the integer subtraction happens before any float is involved.
    split_a = np.float32(jdn - JDN_J2000) / np.float32(DAYS_PER_JULIAN_CENTURY) + np.float32(
        frac - 0.5
    ) / np.float32(DAYS_PER_JULIAN_CENTURY)
    split_b = np.float32(one_hour_later[0] - JDN_J2000) / np.float32(
        DAYS_PER_JULIAN_CENTURY
    ) + np.float32(one_hour_later[1] - 0.5) / np.float32(DAYS_PER_JULIAN_CENTURY)
    assert split_a != split_b, "the split must preserve an hour under float32"


def test_julian_centuries_is_monotonic_and_scaled_correctly():
    start = unix_from_civil(2000, 1, 1, 12, 0, 0)
    one_century = julian_centuries(*jdn_and_frac(start + int(DAYS_PER_JULIAN_CENTURY * 86400)))
    assert one_century == pytest.approx(1.0, abs=1e-9)

    previous = -1e9
    for year in range(1990, 2100, 7):
        current = julian_centuries(*jdn_and_frac(unix_from_civil(year, 3, 1)))
        assert current > previous
        previous = current


def test_unix_from_civil_round_trips_through_datetime():
    for year, month, day, hour, minute in [
        (1970, 1, 1, 0, 0),
        (2024, 2, 29, 23, 59),
        (2026, 9, 29, 13, 45),
        (2099, 12, 31, 12, 0),
    ]:
        expected = int(
            datetime.datetime(
                year, month, day, hour, minute, tzinfo=datetime.timezone.utc
            ).timestamp()
        )
        assert unix_from_civil(year, month, day, hour, minute) == expected
