"""Time handling for the solar math, built to survive single-precision floats.

THE PROBLEM
-----------
A Julian Day in 2026 is about 2_460_700. On a MicroPython build with
single-precision floats, a 24-bit mantissa behind a number that large leaves
roughly 0.25 days -- six hours -- of resolution. Computing `jd = 2460700.5` and
then `T = (jd - 2451545) / 36525` destroys the time of day completely, and does
it silently: no exception, no warning, just a sun in the wrong part of the sky.

THE FIX
-------
Never materialise a full Julian Day as a float. Carry time as a pair:

    jdn   an arbitrary-precision int, the Julian Day Number of the calendar date
    frac  a float in [0.0, 1.0), the fraction of a day elapsed since midnight UT

The integer part stays exact no matter how far from J2000 we are, and `frac`
never exceeds 1.0, so it keeps its full mantissa: about 6e-8 of a day, or 5
milliseconds, even in single precision.

The two are then used for different things, which is what makes this robust:

  * `julian_centuries()` feeds only SLOW quantities -- the sun's mean longitude,
    its mean anomaly, the obliquity of the ecliptic, the equation of time. These
    drift by fractions of a degree per day, so even a minute of error in T is
    worth well under 0.001 degrees.

  * `frac` feeds the hour angle DIRECTLY, in `sunpos.true_solar_time()`. That is
    the fast-varying term -- it sweeps 15 degrees per hour -- and it is exactly
    the one that keeps full precision here.

So the split is not merely a workaround for weak hardware. It routes each
quantity through the representation that suits it.

Run `tools/check_board.py` to find out which float regime your board is in. The
answer changes nothing in this module; it only tells you how much margin you have.
"""

# Julian Day Number of 1970-01-01 (the JDN of the calendar date, i.e. JD at noon).
JDN_UNIX_EPOCH = 2440588

# Julian Day Number of 2000-01-01. JD 2451545.0 is 2000-01-01 12:00 UT, so this
# integer is the reference point for Julian centuries.
JDN_J2000 = 2451545

SECONDS_PER_DAY = 86400
DAYS_PER_JULIAN_CENTURY = 36525.0

# MicroPython on the ESP32 counts time from 2000-01-01; the phone sends a Unix
# epoch. tools/check_board.py reports which one this board uses. Conversion
# happens once, in net/timesync.py, using this constant.
UNIX_TO_2000_OFFSET = 946_684_800


def jdn_and_frac(unix_epoch_s):
    """Split Unix epoch seconds into (Julian Day Number, fraction of day).

    `unix_epoch_s` may be an int or a float. Passing an int keeps the whole
    calculation exact up to the final division, which is the preferred path.

    Returns (jdn: int, frac: float) with 0.0 <= frac < 1.0, where `frac` is
    measured from midnight UT -- not from Julian noon.
    """
    whole = int(unix_epoch_s // SECONDS_PER_DAY)
    remainder = unix_epoch_s - whole * SECONDS_PER_DAY
    return JDN_UNIX_EPOCH + whole, remainder / SECONDS_PER_DAY


def jdn_from_civil(year, month, day):
    """Julian Day Number of a Gregorian calendar date (Fliegel-Van Flandern).

    Pure integer arithmetic, so it is exact for any year MicroPython's ints can
    hold. Used by the tests and by tools/; the firmware itself works from the
    Unix epoch the phone supplies.
    """
    # The published formula assumes integer division that TRUNCATES toward zero.
    # Python's // floors, so (1 - 14) // 12 yields -2 where the algorithm wants
    # -1, putting January and February two days out. Spell the intent directly.
    a = -1 if month <= 2 else 0
    return (
        (1461 * (year + 4800 + a)) // 4
        + (367 * (month - 2 - 12 * a)) // 12
        - (3 * ((year + 4900 + a) // 100)) // 4
        + day
        - 32075
    )


def unix_from_civil(year, month, day, hour=0, minute=0, second=0):
    """Unix epoch seconds for a UTC calendar date and time. Exact, integer-only."""
    days = jdn_from_civil(year, month, day) - JDN_UNIX_EPOCH
    return days * SECONDS_PER_DAY + hour * 3600 + minute * 60 + second


def julian_centuries(jdn, frac):
    """Julian centuries since J2000.0, computed to protect the mantissa.

    The naive form is `((jdn - 2451545) + (frac - 0.5)) / 36525`. That sums a
    number in the thousands with one below 1.0 BEFORE dividing, so on a
    single-precision build the small term is quantised to roughly 1/1024 of a
    day -- about 84 seconds.

    Dividing each term separately and adding afterwards keeps both near their own
    magnitude, which cuts that to under a minute. Since T only drives slowly
    varying quantities (see the module docstring), the residual is worth far less
    than 0.001 degrees of sun position.

    `jdn - JDN_J2000` is an exact integer subtraction: the large numbers cancel
    before any float is involved. That is the step that matters most.
    """
    whole_days = jdn - JDN_J2000  # exact, int
    return whole_days / DAYS_PER_JULIAN_CENTURY + (frac - 0.5) / DAYS_PER_JULIAN_CENTURY


def julian_day(jdn, frac):
    """The full Julian Day as a float.

    Provided for reporting and for cross-checks against external references.
    DO NOT feed this into the solar math -- reconstructing the large number is
    precisely what this module exists to avoid.
    """
    return (jdn - 0.5) + frac
