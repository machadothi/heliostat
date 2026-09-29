"""Solar position by the NOAA / Meeus algorithm.

Accuracy is about 0.01 degrees for 1901-2099, which is far better than anything
else in this machine:

    sun's angular diameter (irreducible beam spread)   0.53 deg
    SC09 step (0.293 deg), doubled at the beam         0.59 deg
    ST3020 step (0.088 deg), doubled at the beam       0.18 deg
    measured servo error +/-2 steps, doubled           0.35 / 1.17 deg
    this algorithm, doubled at the beam                0.02 deg

A pointing error in the mirror normal shows up DOUBLED in the reflected beam,
which is why every row above is quoted that way. The full NREL SPA would reach
0.0003 degrees at the cost of some 200 periodic-term coefficients; against a
0.53-degree sun that precision would be pure cost.

Function names deliberately mirror the column headings of NOAA's published
solar calculator spreadsheet, so the two can be diffed line by line when
something disagrees.

CONVENTIONS, fixed once and never varied (see docs/protocol.md):
    azimuth    degrees from true North, clockwise, 0 <= az < 360
    elevation  degrees above the horizon, negative below
    longitude  East positive
    latitude   North positive
"""

import math

from solar.julian import jdn_and_frac, julian_centuries

_RAD = math.pi / 180.0
_DEG = 180.0 / math.pi


def geom_mean_long_sun(t):
    """Geometric mean longitude of the sun, degrees, wrapped to [0, 360)."""
    return (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0


def geom_mean_anom_sun(t):
    """Geometric mean anomaly of the sun, degrees (deliberately not wrapped)."""
    return 357.52911 + t * (35999.05029 - 0.0001537 * t)


def eccent_earth_orbit(t):
    """Eccentricity of Earth's orbit, dimensionless."""
    return 0.016708634 - t * (0.000042037 + 0.0000001267 * t)


def sun_eq_of_center(t):
    """Equation of centre, degrees: the correction from mean to true anomaly."""
    m = geom_mean_anom_sun(t) * _RAD
    return (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )


def sun_true_long(t):
    """True longitude of the sun, degrees."""
    return geom_mean_long_sun(t) + sun_eq_of_center(t)


def sun_app_long(t):
    """Apparent longitude, degrees: true longitude corrected for nutation and aberration."""
    return sun_true_long(t) - 0.00569 - 0.00478 * math.sin((125.04 - 1934.136 * t) * _RAD)


def mean_obliq_ecliptic(t):
    """Mean obliquity of the ecliptic, degrees."""
    seconds = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))
    return 23.0 + (26.0 + seconds / 60.0) / 60.0


def obliq_corr(t):
    """Obliquity corrected for nutation, degrees."""
    return mean_obliq_ecliptic(t) + 0.00256 * math.cos((125.04 - 1934.136 * t) * _RAD)


def sun_declination(t):
    """Solar declination, degrees."""
    sin_decl = math.sin(obliq_corr(t) * _RAD) * math.sin(sun_app_long(t) * _RAD)
    return math.asin(_clamp(sin_decl, -1.0, 1.0)) * _DEG


def var_y(t):
    """tan^2(obliquity / 2). An intermediate of the equation of time."""
    y = math.tan(obliq_corr(t) * _RAD / 2.0)
    return y * y


def equation_of_time(t):
    """Equation of time in MINUTES: apparent solar time minus mean solar time.

    Swings roughly -14 to +16 minutes over a year, which is up to 4 degrees of
    hour angle. Omitting it would be the single largest error in the system.
    """
    y = var_y(t)
    l0 = geom_mean_long_sun(t) * _RAD
    m = geom_mean_anom_sun(t) * _RAD
    e = eccent_earth_orbit(t)
    radians = (
        y * math.sin(2 * l0)
        - 2.0 * e * math.sin(m)
        + 4.0 * e * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * e * e * math.sin(2 * m)
    )
    return 4.0 * radians * _DEG


def true_solar_time(frac, eot_minutes, lon_deg):
    """Minutes since local apparent midnight, in [0, 1440).

    `frac` is the fraction of a UT day from solar.julian -- passed in directly
    rather than reconstructed from a Julian Day, which is what keeps the
    fast-varying term at full precision. See the julian module docstring.
    """
    return (frac * 1440.0 + eot_minutes + 4.0 * lon_deg) % 1440.0


def hour_angle(tst_minutes):
    """Solar hour angle in degrees: negative before local solar noon."""
    ha = tst_minutes / 4.0 - 180.0
    return ha + 360.0 if ha < -180.0 else ha


def solar_zenith(lat_deg, decl_deg, ha_deg):
    """True (geometric) solar zenith angle in degrees, ignoring refraction."""
    lat, decl, ha = lat_deg * _RAD, decl_deg * _RAD, ha_deg * _RAD
    cos_z = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    return math.acos(_clamp(cos_z, -1.0, 1.0)) * _DEG


def solar_azimuth(lat_deg, decl_deg, ha_deg, zenith_deg):
    """Solar azimuth in degrees clockwise from true North, in [0, 360)."""
    lat, decl, zenith = lat_deg * _RAD, decl_deg * _RAD, zenith_deg * _RAD
    sin_zenith = math.sin(zenith)

    # At the zenith and the nadir azimuth is undefined; the sun is directly
    # overhead or underfoot and any bearing is as good as another.
    if abs(sin_zenith) < 1e-9:
        return 0.0

    cos_az = (math.sin(lat) * math.cos(zenith) - math.sin(decl)) / (math.cos(lat) * sin_zenith)
    az = math.acos(_clamp(cos_az, -1.0, 1.0)) * _DEG
    return (az + 180.0) % 360.0 if ha_deg > 0.0 else (540.0 - az) % 360.0


def sun_position(unix_epoch_s, lat_deg, lon_deg, pressure_hpa=None, temp_c=None):
    """Sun position for a Unix timestamp and a site.

    Returns (azimuth_deg, elevation_true_deg, elevation_apparent_deg).

    `elevation_true` is geometric. `elevation_apparent` adds atmospheric
    refraction, which lifts the sun by about 0.55 degrees at the horizon, 0.1 at
    10 degrees and 0.03 at 30. Track against the APPARENT elevation -- that is
    where the light actually comes from.

    Supplying `pressure_hpa` and `temp_c` from the BMP180 makes the refraction
    correction site-specific rather than assuming a standard atmosphere. This is
    a genuine use for that sensor, not just logging.
    """
    from solar.refraction import apparent_elevation

    jdn, frac = jdn_and_frac(unix_epoch_s)
    t = julian_centuries(jdn, frac)

    decl = sun_declination(t)
    eot = equation_of_time(t)
    tst = true_solar_time(frac, eot, lon_deg)
    ha = hour_angle(tst)

    zenith = solar_zenith(lat_deg, decl, ha)
    elevation = 90.0 - zenith
    azimuth = solar_azimuth(lat_deg, decl, ha, zenith)

    return azimuth, elevation, apparent_elevation(elevation, pressure_hpa, temp_c)


def solar_noon_minutes(unix_epoch_s, lon_deg):
    """Minutes after UT midnight at which the sun transits the local meridian.

    Useful as an independent cross-check: compare against published transit times
    for your city, which should agree within about a minute.
    """
    jdn, frac = jdn_and_frac(unix_epoch_s)
    t = julian_centuries(jdn, frac)
    return 720.0 - 4.0 * lon_deg - equation_of_time(t)


def _clamp(value, low, high):
    """Guard acos/asin against domain errors from rounding at the limits."""
    if value < low:
        return low
    return high if value > high else value
