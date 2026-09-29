"""Mapping a desired mirror normal onto two joint angles, within real limits.

An azimuth turntable carrying an elevation pivot. The mirror's normal is rigidly
attached to the elevation joint, so:

    az = atan2(n.x, n.y)        measured from North, clockwise
    el = asin(n.z)              90 degrees means the mirror faces straight up

Three things live here because they are pure arithmetic and must be unit-tested
without a board attached, and because each of them will bite otherwise:

  * AZIMUTH WRAP. The ST3020 is single-turn. It physically cannot rotate past its
    own zero, so there is a wedge of azimuth it can never traverse. Asking for
    "the short way round" without checking can wind the servo cable into a knot.

  * KEEP-OUT ZONES. Checked against the BEAM direction, never the normal. The
    normal points somewhere else entirely, and confusing the two would let the
    beam sweep across exactly what the zone was meant to protect.

  * SOFT LIMITS. Enforced before a command is ever sent, so the mechanism does
    not rely on the limit switches for routine operation.
"""

import math

from control.calibration import (
    deadband_deg,
    from_servo_deg,
    to_servo_deg,
)
from control.geometry import azel_from_vector, vector_from_azel

_RAD = math.pi / 180.0
_DEG = 180.0 / math.pi

# Encoder geometry per servo family, from the datasheets and confirmed on the
# bench: the ST3020 answered 0..4095 over 360 deg, the SC09 0..1023 over 300.
FAMILY_STEPS = {"STS": 4096, "SCS": 1024}
FAMILY_DEGREES = {"STS": 360.0, "SCS": 300.0}


class OutOfReach(Exception):
    """The requested angle cannot be reached within this axis's travel."""


class KeepOutViolation(Exception):
    """The beam would enter a forbidden region."""


# --- the mount ---------------------------------------------------------------


class AzElMount:
    """Azimuth turntable carrying an elevation pivot."""

    @staticmethod
    def joints_from_normal(n):
        """Mirror normal -> (azimuth_deg in [0,360), elevation_deg in [-90,90])."""
        return azel_from_vector(n)

    @staticmethod
    def normal_from_joints(azimuth_deg, elevation_deg):
        """Joint angles -> mirror normal. Exact inverse of joints_from_normal."""
        return vector_from_azel(azimuth_deg, elevation_deg)


# --- one axis ----------------------------------------------------------------


class Axis:
    """One mechanical degree of freedom, and everything known about its limits.

    Deliberately free of hardware: hal/servo_axis.py wraps this to talk to a
    servo, and hal/servo_axis_sim.py wraps the same thing to talk to nothing.
    """

    def __init__(
        self,
        name,
        servo_id,
        family,
        offset_deg=0.0,
        direction=1,
        gear_ratio=1.0,
        min_deg=-180.0,
        max_deg=180.0,
        backlash_deg=0.0,
        max_speed_dps=60.0,
        home_mech_deg=0.0,
        home_dir=-1,
    ):
        if family not in FAMILY_STEPS:
            raise ValueError(f"unknown servo family: {family}")
        if direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if gear_ratio <= 0.0:
            raise ValueError("gear_ratio must be positive")
        if min_deg >= max_deg:
            raise ValueError("min_deg must be below max_deg")

        self.name = name
        self.servo_id = servo_id
        self.family = family
        self.offset_deg = offset_deg
        self.direction = direction
        self.gear_ratio = gear_ratio
        self.min_deg = min_deg
        self.max_deg = max_deg
        self.backlash_deg = backlash_deg
        self.max_speed_dps = max_speed_dps
        self.home_mech_deg = home_mech_deg
        self.home_dir = home_dir

    # -- calibration transform ------------------------------------------------

    def to_servo_deg(self, mech_deg):
        return to_servo_deg(mech_deg, self.offset_deg, self.direction, self.gear_ratio)

    def from_servo_deg(self, servo_deg):
        return from_servo_deg(servo_deg, self.offset_deg, self.direction, self.gear_ratio)

    # -- resolution -----------------------------------------------------------

    @property
    def step_deg(self):
        """Mechanical degrees per encoder step, after gearing."""
        return FAMILY_DEGREES[self.family] / FAMILY_STEPS[self.family] / self.gear_ratio

    @property
    def deadband_deg(self):
        """Smallest command worth sending. Anything finer only makes it hunt."""
        one_step = deadband_deg(FAMILY_STEPS[self.family], FAMILY_DEGREES[self.family])
        return one_step / self.gear_ratio

    # -- limits ---------------------------------------------------------------

    def clamp(self, mech_deg):
        """Constrain to the soft limits. Returns (clamped_deg, hit_limit)."""
        if mech_deg < self.min_deg:
            return self.min_deg, True
        if mech_deg > self.max_deg:
            return self.max_deg, True
        return mech_deg, False

    def in_range(self, mech_deg):
        return self.min_deg <= mech_deg <= self.max_deg

    def unwrap(self, target_deg, current_deg=None):
        """Pick the reachable representation of `target_deg` nearest `current_deg`.

        An azimuth of 350 degrees is the same direction as -10, but an axis whose
        travel is -170..+170 can reach only the second. This searches every
        representation `target + k*360` that falls inside the travel and returns
        the one closest to where the axis is now -- which is both the shortest
        move and, since the unreachable wedge is simply outside the limits, one
        that cannot cross it.

        Raises OutOfReach when no representation fits, rather than silently
        choosing a path that would wind up the cable.
        """
        if current_deg is None:
            current_deg = 0.5 * (self.min_deg + self.max_deg)

        lowest_k = int(math.ceil((self.min_deg - target_deg) / 360.0))
        highest_k = int(math.floor((self.max_deg - target_deg) / 360.0))

        best = None
        best_distance = None
        for k in range(lowest_k, highest_k + 1):
            candidate = target_deg + k * 360.0
            distance = abs(candidate - current_deg)
            if best_distance is None or distance < best_distance:
                best, best_distance = candidate, distance

        if best is None:
            raise OutOfReach(
                f"{self.name}: {target_deg:.2f} deg is unreachable "
                f"within [{self.min_deg:.1f}, {self.max_deg:.1f}]"
            )
        return best

    def __repr__(self):
        return (
            f"<Axis {self.name} id={self.servo_id} {self.family} "
            f"[{self.min_deg:.1f},{self.max_deg:.1f}] step={self.step_deg:.3f}>"
        )


# --- keep-out zones ----------------------------------------------------------


def zone_contains(zone, azimuth_deg, elevation_deg):
    """Is this direction inside a keep-out zone?

    A zone is a dict with az_min, az_max, el_min, el_max. The azimuth span may
    wrap through North (az_min 350, az_max 10 is a 20-degree window straddling 0),
    which is handled explicitly -- getting it wrong would leave a gap in exactly
    the place people put things.
    """
    if not (zone["el_min"] <= elevation_deg <= zone["el_max"]):
        return False

    az_min = zone["az_min"] % 360.0
    az_max = zone["az_max"] % 360.0
    az = azimuth_deg % 360.0

    if az_min <= az_max:
        return az_min <= az <= az_max
    return az >= az_min or az <= az_max  # wraps through North


def keepout_violation(azimuth_deg, elevation_deg, zones):
    """Return the first zone this direction falls inside, or None.

    Call with the BEAM direction. The mirror normal points somewhere else.
    """
    for zone in zones or ():
        if zone_contains(zone, azimuth_deg, elevation_deg):
            return zone
    return None


def assert_beam_allowed(azimuth_deg, elevation_deg, zones):
    """Raise KeepOutViolation if the beam is aimed somewhere forbidden."""
    zone = keepout_violation(azimuth_deg, elevation_deg, zones)
    if zone is not None:
        raise KeepOutViolation(
            "beam at az={:.1f} el={:.1f} enters keep-out zone: {}".format(
                azimuth_deg, elevation_deg, zone.get("note", "unnamed")
            )
        )


# --- solving a normal onto the mount -----------------------------------------


def solve(mount, az_axis, el_axis, normal, current_az_deg=None):
    """Turn a desired mirror normal into commandable mechanical angles.

    Returns (az_mech_deg, el_mech_deg, hit_limit). Raises OutOfReach when the
    azimuth cannot be reached at all.

    `hit_limit` being True means the request was clamped, so the mirror will not
    point exactly where it was asked to -- the caller should treat the aim as
    approximate and, in tracking, defocus rather than pretend.
    """
    azimuth, elevation = mount.joints_from_normal(normal)

    az_mech = az_axis.unwrap(azimuth, current_az_deg)
    az_mech, az_clamped = az_axis.clamp(az_mech)
    el_mech, el_clamped = el_axis.clamp(elevation)

    return az_mech, el_mech, (az_clamped or el_clamped)
