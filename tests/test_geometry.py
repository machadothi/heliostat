"""Reflection geometry, checked by property rather than by example.

The central claim -- that n = normalize(s + t) sends sunlight from s onto t --
is asserted directly against the law of reflection over thousands of random
configurations. If that identity holds, the beam lands where the firmware says
it will, and everything above this layer is arithmetic.
"""

import math
import random

import pytest
from control.geometry import (
    POOR_EFFICIENCY_DOT,
    UnreachableTarget,
    angle_between_deg,
    azel_from_vector,
    beam_direction,
    cosine_efficiency,
    cross,
    defocus_normal,
    dot,
    incidence_angle_deg,
    is_poorly_conditioned,
    mirror_normal,
    negate,
    norm,
    normalize,
    reflect,
    sun_vector,
    target_from_normal,
    target_vector_from_azel,
    target_vector_from_point,
    vector_from_azel,
)

# Enough samples that a geometry bug cannot hide in a corner of the sphere.
PROPERTY_SAMPLES = 10_000
TOLERANCE = 1e-9

# The reachable band. Below this the configuration is degenerate by design and
# mirror_normal is supposed to refuse, which is tested separately.
MIN_ALIGNMENT = -0.99


def _random_unit_vector(rng):
    """Uniform on the sphere. Sampling az/el uniformly would cluster at the poles."""
    z = rng.uniform(-1.0, 1.0)
    theta = rng.uniform(0.0, 2.0 * math.pi)
    r = math.sqrt(max(0.0, 1.0 - z * z))
    return (r * math.cos(theta), r * math.sin(theta), z)


def _random_reachable_pair(rng):
    """A (sun, target) pair that is not degenerate."""
    while True:
        s = _random_unit_vector(rng)
        t = _random_unit_vector(rng)
        if dot(s, t) > MIN_ALIGNMENT:
            return s, t


# Use the firmware's own robust form rather than acos, which would manufacture
# about 1e-6 degrees of error for vectors that are actually identical.
_angle_between = angle_between_deg


# --- the identity everything rests on ----------------------------------------


def test_reflection_identity_over_the_whole_sphere():
    """The bisector normal must send the sun's light onto the target. Always."""
    rng = random.Random(20260929)
    worst = 0.0
    for _ in range(PROPERTY_SAMPLES):
        s, t = _random_reachable_pair(rng)
        n = mirror_normal(s, t)
        landed = reflect(negate(s), n)
        error = norm((landed[0] - t[0], landed[1] - t[1], landed[2] - t[2]))
        worst = max(worst, error)
    assert worst < 1e-9, f"worst reflection error {worst:.3e}"


def test_angle_of_incidence_equals_angle_of_reflection():
    """The defining property of a mirror, and of the bisector."""
    rng = random.Random(7)
    for _ in range(PROPERTY_SAMPLES):
        s, t = _random_reachable_pair(rng)
        n = mirror_normal(s, t)
        assert dot(n, s) == pytest.approx(dot(n, t), abs=1e-12)


def test_normal_is_a_unit_vector():
    rng = random.Random(99)
    for _ in range(1000):
        s, t = _random_reachable_pair(rng)
        assert norm(mirror_normal(s, t)) == pytest.approx(1.0, abs=1e-12)


def test_normal_lies_in_the_sun_target_plane():
    """The bisector cannot tilt out of the plane the two rays define."""
    rng = random.Random(1234)
    for _ in range(1000):
        s, t = _random_reachable_pair(rng)
        n = mirror_normal(s, t)
        plane_normal = cross(s, t)
        if norm(plane_normal) < 1e-6:
            continue  # collinear: every plane contains them
        assert dot(n, normalize(plane_normal)) == pytest.approx(0.0, abs=1e-9)


# --- the inverse: "capture current aim" --------------------------------------


def test_target_from_normal_recovers_the_target():
    """The feature that turns calibration into a single tap."""
    rng = random.Random(555)
    for _ in range(PROPERTY_SAMPLES):
        s, t = _random_reachable_pair(rng)
        n = mirror_normal(s, t)
        recovered = target_from_normal(s, n)
        assert _angle_between(recovered, t) < 1e-6


def test_beam_direction_is_target_from_normal():
    """Same function, named for the safety supervisor's use of it."""
    rng = random.Random(8)
    for _ in range(200):
        s, t = _random_reachable_pair(rng)
        n = mirror_normal(s, t)
        assert beam_direction(s, n) == target_from_normal(s, n)


def test_an_arbitrary_normal_still_yields_a_definite_beam():
    """Jogging produces normals that solve nothing; the beam must still be defined."""
    rng = random.Random(31337)
    for _ in range(1000):
        s = _random_unit_vector(rng)
        n = _random_unit_vector(rng)
        assert norm(beam_direction(s, n)) == pytest.approx(1.0, abs=1e-9)


# --- efficiency ---------------------------------------------------------------


def test_cosine_efficiency_equals_the_projection_onto_the_normal():
    rng = random.Random(4242)
    for _ in range(PROPERTY_SAMPLES):
        s, t = _random_reachable_pair(rng)
        n = mirror_normal(s, t)
        assert cosine_efficiency(s, t) == pytest.approx(dot(n, s), abs=1e-9)


def test_efficiency_is_one_when_the_target_is_toward_the_sun():
    """Normal incidence: the mirror faces the sun and reflects straight back."""
    s = sun_vector(180.0, 45.0)
    assert cosine_efficiency(s, s) == pytest.approx(1.0, abs=1e-12)


def test_efficiency_falls_to_a_half_at_a_right_angle():
    """cos(45) for a 90-degree separation, since the half-angle is what counts."""
    s = vector_from_azel(0.0, 0.0)
    t = vector_from_azel(90.0, 0.0)
    assert cosine_efficiency(s, t) == pytest.approx(math.cos(math.radians(45.0)), abs=1e-12)


def test_efficiency_is_bounded():
    rng = random.Random(606)
    for _ in range(2000):
        s, t = _random_reachable_pair(rng)
        assert 0.0 <= cosine_efficiency(s, t) <= 1.0


def test_incidence_angle_agrees_with_efficiency():
    rng = random.Random(11)
    for _ in range(2000):
        s, t = _random_reachable_pair(rng)
        n = mirror_normal(s, t)
        theta = incidence_angle_deg(s, n)
        assert math.cos(math.radians(theta)) == pytest.approx(cosine_efficiency(s, t), abs=1e-9)


def test_poor_conditioning_is_flagged_before_it_becomes_unreachable():
    s = vector_from_azel(0.0, 10.0)
    assert not is_poorly_conditioned(s, s)
    nearly_opposite = negate(s)
    assert is_poorly_conditioned(s, nearly_opposite)
    assert POOR_EFFICIENCY_DOT > -1.0


# --- the degenerate case ------------------------------------------------------


def test_target_opposite_the_sun_is_refused():
    """t = -s needs grazing incidence, which no mirror can deliver."""
    s = sun_vector(90.0, 30.0)
    with pytest.raises(UnreachableTarget):
        mirror_normal(s, negate(s))


def test_refusal_threshold_is_where_it_claims_to_be():
    """Just inside the limit must work; just outside must raise."""
    s = vector_from_azel(0.0, 0.0)
    # 178 degrees apart -> dot about -0.9994, inside the -0.999 threshold.
    with pytest.raises(UnreachableTarget):
        mirror_normal(s, vector_from_azel(178.0, 0.0))
    # 170 degrees apart -> dot about -0.985, legal but poor.
    normal = mirror_normal(s, vector_from_azel(170.0, 0.0))
    assert norm(normal) == pytest.approx(1.0)


# --- defocus: the factor of two ----------------------------------------------


@pytest.mark.parametrize("offset_deg", [1.0, 4.0, 8.0, 15.0, 30.0])
def test_defocus_moves_the_beam_by_exactly_the_requested_angle(offset_deg):
    """THE test for the doubling.

    defocus_normal rotates the NORMAL by half the offset, because the reflected
    ray turns through twice the angle the normal does. If that factor of two is
    wrong the beam moves half or double what was asked -- and a defocus that only
    half works still burns whatever it was meant to move off.
    """
    rng = random.Random(2024)
    for _ in range(500):
        s, t = _random_reachable_pair(rng)
        if dot(s, t) < -0.9:
            continue  # poorly conditioned; the rotation axis gets unstable
        defocused = defocus_normal(s, t, offset_deg)
        moved = _angle_between(beam_direction(s, defocused), t)
        assert moved == pytest.approx(offset_deg, abs=1e-6)


def test_defocus_of_zero_is_the_aimed_normal():
    s = sun_vector(120.0, 40.0)
    t = target_vector_from_azel(200.0, 10.0)
    assert _angle_between(defocus_normal(s, t, 0.0), mirror_normal(s, t)) < 1e-9


def test_defocus_works_at_normal_incidence():
    """Sun and target collinear: the rotation axis is degenerate and must be chosen."""
    s = sun_vector(180.0, 45.0)
    defocused = defocus_normal(s, s, 8.0)
    assert norm(defocused) == pytest.approx(1.0, abs=1e-9)
    assert _angle_between(beam_direction(s, defocused), s) == pytest.approx(8.0, abs=1e-6)


def test_defocus_result_is_a_unit_vector():
    rng = random.Random(77)
    for _ in range(1000):
        s, t = _random_reachable_pair(rng)
        if dot(s, t) < -0.9:
            continue
        assert norm(defocus_normal(s, t, 8.0)) == pytest.approx(1.0, abs=1e-9)


# --- direction conversions ----------------------------------------------------


def test_azel_round_trips_through_the_vector_form():
    rng = random.Random(31)
    for _ in range(PROPERTY_SAMPLES):
        azimuth = rng.uniform(0.0, 360.0)
        elevation = rng.uniform(-89.9, 89.9)
        back_az, back_el = azel_from_vector(vector_from_azel(azimuth, elevation))
        assert back_el == pytest.approx(elevation, abs=1e-9)
        assert abs((back_az - azimuth + 180.0) % 360.0 - 180.0) < 1e-9


@pytest.mark.parametrize(
    ("azimuth", "elevation", "expected"),
    [
        (0.0, 0.0, (0.0, 1.0, 0.0)),  # North
        (90.0, 0.0, (1.0, 0.0, 0.0)),  # East
        (180.0, 0.0, (0.0, -1.0, 0.0)),  # South
        (270.0, 0.0, (-1.0, 0.0, 0.0)),  # West
        (0.0, 90.0, (0.0, 0.0, 1.0)),  # straight up
    ],
)
def test_cardinal_directions(azimuth, elevation, expected):
    """Pins the ENU convention: x East, y North, z Up, azimuth clockwise from North."""
    v = vector_from_azel(azimuth, elevation)
    for got, want in zip(v, expected, strict=True):
        assert got == pytest.approx(want, abs=1e-12)


def test_azimuth_is_normalised_to_the_positive_range():
    for azimuth in (-90.0, -1.0, 360.0, 450.0, 720.0):
        back_az, _ = azel_from_vector(vector_from_azel(azimuth, 20.0))
        assert 0.0 <= back_az < 360.0


def test_straight_up_has_no_defined_azimuth():
    azimuth, elevation = azel_from_vector((0.0, 0.0, 1.0))
    assert elevation == pytest.approx(90.0)
    assert azimuth == 0.0  # any value would serve; we pick zero deterministically


# --- target from a nearby point -----------------------------------------------


def test_target_from_point_is_a_unit_vector_toward_that_point():
    t = target_vector_from_point(0.0, 10.0, 2.0)
    assert norm(t) == pytest.approx(1.0, abs=1e-12)
    azimuth, elevation = azel_from_vector(t)
    assert azimuth == pytest.approx(0.0, abs=1e-9)  # due North
    assert elevation == pytest.approx(math.degrees(math.atan2(2.0, 10.0)), abs=1e-9)


def test_point_and_direction_targets_agree_when_scaled():
    """Only the direction matters, so distance must not change the answer."""
    near = target_vector_from_point(3.0, 4.0, 5.0)
    far = target_vector_from_point(300.0, 400.0, 500.0)
    assert _angle_between(near, far) < 1e-12


def test_a_target_at_the_pivot_is_rejected():
    with pytest.raises(ValueError):
        target_vector_from_point(0.0, 0.0, 0.0)


# --- a worked end-to-end case -------------------------------------------------


def test_worked_example_sun_east_target_west():
    """Sun low in the east, target low in the west: the mirror must face straight up.

    Both rays are horizontal and opposed, so their bisector is vertical. A good
    sanity check because the answer is obvious without any algebra.
    """
    s = sun_vector(90.0, 10.0)
    t = target_vector_from_azel(270.0, 10.0)
    n = mirror_normal(s, t)
    _, elevation = azel_from_vector(n)
    assert elevation == pytest.approx(90.0, abs=1e-9)


def test_worked_example_sun_and_target_coincide():
    """Retroreflection: the mirror faces the sun and the normal is the sun vector."""
    s = sun_vector(150.0, 35.0)
    n = mirror_normal(s, s)
    assert _angle_between(n, s) < 1e-9
    assert incidence_angle_deg(s, n) == pytest.approx(0.0, abs=1e-6)
