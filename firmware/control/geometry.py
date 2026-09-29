"""Reflection geometry. The mathematical core of the whole machine.

FRAME
-----
Local ENU with its origin at the mirror's pivot point:

    x = East      y = North      z = Up

Azimuth is measured from true North, clockwise, 0 <= az < 360. Elevation is
degrees above the horizon. These conventions are fixed here and in
docs/protocol.md and are never varied anywhere else.

THE CENTRAL RESULT
------------------
Let `s` be the unit vector pointing from the mirror TOWARD the sun, and `t` the
unit vector pointing from the mirror TOWARD the target.

Light travels from the sun to the mirror, so the incoming ray's direction is
`d = -s`. Reflection about a surface with unit normal `n` gives

    r = d - 2(d . n) n  =  -s + 2(s . n) n

We need the reflected ray to hit the target, so r = t:

    -s + 2(s . n) n = t
         2(s . n) n = s + t

The left-hand side is `n` multiplied by a scalar, so `n` must be parallel to
`s + t`. Therefore

    n = normalize(s + t)

The mirror normal is the unit bisector of the sun vector and the target vector.
Equivalently: the angle of incidence equals the angle of reflection, so the
normal sits exactly halfway between where the light comes from and where it must
go.

TWO CONSEQUENCES WORTH INTERNALISING
------------------------------------
1. The mirror turns at HALF the sun's rate. Over a day the sun sweeps roughly
   180 degrees of azimuth while the mirror normal sweeps about 90. This is why
   the tracker can run at 1 Hz with a one-step deadband.

2. Any pointing error is DOUBLED at the beam. Rotating the normal by delta
   rotates the reflected ray by 2*delta. Every error budget in this project is
   quoted doubled for that reason.

Vectors are plain 3-tuples. No class, no allocation churn in the control loop.
"""

import math

_RAD = math.pi / 180.0
_DEG = 180.0 / math.pi

# Below this dot product the target is so nearly opposite the sun that the
# required incidence angle approaches 90 degrees -- grazing, and physically
# useless. 0.999 corresponds to about 178.4 degrees between s and t.
UNREACHABLE_DOT = -0.999

# Warn below this: cosine efficiency has fallen under about 22%.
POOR_EFFICIENCY_DOT = -0.9


class UnreachableTarget(Exception):
    """The target cannot be illuminated from the sun's current direction.

    Raised when the target lies (almost) diametrically opposite the sun, which
    would need light to graze the mirror at 90 degrees of incidence. The tracker
    responds by defocusing.
    """


# --- vector primitives -------------------------------------------------------


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a):
    return math.sqrt(dot(a, a))


def scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def negate(a):
    return (-a[0], -a[1], -a[2])


def normalize(a):
    length = norm(a)
    if length < 1e-12:
        raise ValueError("cannot normalize a zero-length vector")
    return scale(a, 1.0 / length)


# --- directions <-> vectors --------------------------------------------------


def vector_from_azel(azimuth_deg, elevation_deg):
    """Unit vector for a direction given as azimuth (from North, clockwise) and elevation."""
    az, el = azimuth_deg * _RAD, elevation_deg * _RAD
    cos_el = math.cos(el)
    return (cos_el * math.sin(az), cos_el * math.cos(az), math.sin(el))


def azel_from_vector(v):
    """Inverse of vector_from_azel. Returns (azimuth_deg in [0,360), elevation_deg)."""
    x, y, z = v
    horizontal = math.sqrt(x * x + y * y)
    if horizontal < 1e-12:
        # Straight up or straight down: azimuth is undefined, any value serves.
        return 0.0, 90.0 if z > 0 else -90.0
    azimuth = math.atan2(x, y) * _DEG % 360.0
    # A hair below zero, such as -1e-15, rounds UP to exactly 360.0 under the
    # modulo, which breaks the [0, 360) contract everything downstream assumes.
    if azimuth >= 360.0:
        azimuth = 0.0
    return azimuth, math.atan2(z, horizontal) * _DEG


def sun_vector(azimuth_deg, elevation_deg):
    """Unit vector from the mirror TOWARD the sun."""
    return vector_from_azel(azimuth_deg, elevation_deg)


def target_vector_from_azel(azimuth_deg, elevation_deg):
    """Unit vector from the mirror TOWARD the target, given as a direction.

    Correct only when the target is far enough away that its direction does not
    depend on where on the mirror you stand. Use target_vector_from_point() for
    anything closer than about 50 m.
    """
    return vector_from_azel(azimuth_deg, elevation_deg)


def target_vector_from_point(x_m, y_m, z_m):
    """Unit vector toward a target at a position in metres from the pivot (ENU).

    Prefer this for a nearby target. At 10 m, ignoring a 0.2 m offset between the
    pivot and the mirror face is already 1.1 degrees of aiming error -- more than
    twice the sun's diameter once doubled at the beam.
    """
    return normalize((x_m, y_m, z_m))


# --- reflection --------------------------------------------------------------


def reflect(direction, normal):
    """Reflect a ray travelling along `direction` off a surface with unit `normal`."""
    return sub(direction, scale(normal, 2.0 * dot(direction, normal)))


def mirror_normal(s, t):
    """The mirror normal that sends sunlight from `s` onto the target at `t`.

    n = normalize(s + t). See the module docstring for the derivation.

    Raises UnreachableTarget when the target is essentially opposite the sun.
    """
    alignment = dot(s, t)
    if alignment < UNREACHABLE_DOT:
        raise UnreachableTarget(
            f"target is opposite the sun (s.t = {alignment:.5f}); "
            "reflection would need grazing incidence"
        )
    return normalize(add(s, t))


def target_from_normal(s, n):
    """Where the beam is ACTUALLY landing, given the sun and the current normal.

    This is the inverse of mirror_normal, and it buys the best calibration
    experience available: jog the mirror until the spot lands where you want it,
    then press "capture current aim" and the firmware derives and stores the
    target. One function, and it turns an awkward survey into a single tap.
    """
    return reflect(negate(s), n)


def angle_between_deg(a, b):
    """Angle between two vectors, in degrees, accurate at BOTH ends of the range.

    The obvious `acos(a . b)` loses precision badly for small angles: near
    alignment the dot product sits at 1 - eps, and acos there amplifies the
    rounding error to about sqrt(2*eps) -- some 1e-8 radians for double
    precision, which is 1e-6 of a degree out of nowhere. The atan2 form stays
    accurate near 0 and near 180, at the cost of one cross product.

    It is called at telemetry rates, not in an inner loop, so that is cheap.
    """
    return math.atan2(norm(cross(a, b)), dot(a, b)) * _DEG


def incidence_angle_deg(s, n):
    """Angle between the incoming sunlight and the mirror normal, in degrees.

    Equals the angle of reflection, by definition of a mirror.
    """
    return angle_between_deg(s, n)


def cosine_efficiency(s, t):
    """Fraction of the mirror's area presented to the sun, in [0, 1].

    Equal to cos(theta) where theta is the half-angle between `s` and `t`, so
    cos(theta) = sqrt((1 + s.t) / 2). Also equal to n.s.

    Report this as telemetry. When the sun sits 120 degrees away from the target
    direction only half the flux survives, and the operator should be able to see
    that rather than wonder why the spot is dim.
    """
    return math.sqrt(max(0.0, (1.0 + dot(s, t)) * 0.5))


def is_poorly_conditioned(s, t):
    """True when the geometry is legal but the yield is poor -- worth logging."""
    return dot(s, t) < POOR_EFFICIENCY_DOT


# --- defocus -----------------------------------------------------------------


def defocus_normal(s, t, offset_deg):
    """A normal that deliberately misses the target by `offset_deg`.

    The normal is rotated by HALF the requested offset, because the reflected ray
    turns through twice the angle the normal does. Getting that factor of two
    wrong is the easiest mistake in this whole project, so it is asserted
    directly in tests/test_geometry.py.

    The rotation axis is perpendicular to the sun-target plane, which moves the
    beam the shortest way off target.

    Defocus is the FIRST response to any fault: it takes a fraction of a second
    of servo travel and is instantly reversible, whereas stowing takes tens of
    seconds. Always defocus first, then stow.
    """
    n = mirror_normal(s, t)
    axis = cross(s, t)
    if norm(axis) < 1e-9:
        # Sun and target are collinear (normal incidence, or the degenerate
        # opposite case). Any perpendicular axis will do.
        axis = _any_perpendicular(n)
    return _rotate_about_axis(n, normalize(axis), offset_deg * 0.5)


def beam_direction(s, n):
    """Direction the reflected beam is travelling. Alias of target_from_normal.

    Named for the safety supervisor, which checks the BEAM against keep-out
    zones -- not the normal, which points somewhere else entirely.
    """
    return target_from_normal(s, n)


# --- internals ---------------------------------------------------------------


def _rotate_about_axis(v, axis, angle_deg):
    """Rodrigues' rotation formula. `axis` must already be a unit vector."""
    theta = angle_deg * _RAD
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return add(
        add(scale(v, cos_t), scale(cross(axis, v), sin_t)),
        scale(axis, dot(axis, v) * (1.0 - cos_t)),
    )


def _any_perpendicular(v):
    """Some unit vector perpendicular to `v`. Which one does not matter."""
    # Cross with whichever basis vector is least aligned with v, for conditioning.
    basis = (1.0, 0.0, 0.0) if abs(v[0]) < 0.9 else (0.0, 1.0, 0.0)
    return normalize(cross(v, basis))


def _clamp(value, low, high):
    if value < low:
        return low
    return high if value > high else value
