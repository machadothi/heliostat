"""Atmospheric refraction.

The atmosphere bends light, so the sun appears higher than it geometrically is.
The effect is about 0.55 degrees at the horizon -- more than the sun's own
diameter, which is why the whole disc is already below the horizon at the moment
it appears to touch it -- falling to 0.1 degrees at 10 degrees elevation and
0.03 at 30.

Track against the APPARENT elevation. That is where the light actually arrives
from, and it is the light we are reflecting.

Uses the Saemundsson formula, the standard companion to the NOAA algorithm, with
the usual pressure and temperature scaling. Feeding it real readings from the
BMP180 makes the correction site-specific instead of assuming a standard
atmosphere.

Note where this matters: refraction is largest exactly where we refuse to track
anyway (the safety supervisor stows below 5 degrees). So its practical role is
to keep the dawn and dusk transitions honest rather than to improve midday
aiming.
"""

import math

_RAD = math.pi / 180.0

# ICAO standard atmosphere at sea level, used when no sensor reading is given.
STANDARD_PRESSURE_HPA = 1010.0
STANDARD_TEMP_C = 10.0

# Below this elevation the formula is extrapolating badly; hold it flat rather
# than letting it diverge. The supervisor will have stowed long before here.
_MIN_ELEVATION_DEG = -1.0


def refraction_arcmin(true_elevation_deg, pressure_hpa=None, temp_c=None):
    """Refraction correction in ARCMINUTES to add to a true elevation.

    Returns 0.0 well below the horizon, where the correction is meaningless.
    """
    if true_elevation_deg < _MIN_ELEVATION_DEG:
        return 0.0

    pressure = STANDARD_PRESSURE_HPA if pressure_hpa is None else pressure_hpa
    temperature = STANDARD_TEMP_C if temp_c is None else temp_c

    h = true_elevation_deg
    # Saemundsson: R = 1.02 / tan(h + 10.3 / (h + 5.11)), h in degrees, R in arcmin.
    # The inner term keeps the tangent finite as h approaches the horizon.
    raw = 1.02 / math.tan((h + 10.3 / (h + 5.11)) * _RAD)

    # Denser, colder air refracts more.
    scale = (pressure / STANDARD_PRESSURE_HPA) * (283.0 / (273.0 + temperature))
    return raw * scale


def apparent_elevation(true_elevation_deg, pressure_hpa=None, temp_c=None):
    """True elevation plus refraction, in degrees."""
    return true_elevation_deg + refraction_arcmin(true_elevation_deg, pressure_hpa, temp_c) / 60.0
