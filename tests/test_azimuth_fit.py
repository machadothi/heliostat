"""The magnetometer azimuth calibration, on a simulated sensor riding the mirror.

Synthetic but physical: Stockholm's field (about 51 uT, dipping 72 deg), the
sensor taped to the mirror at an arbitrary unknown angle, a hard-iron bias, a
230 deg yaw range like the real frame, and sensor noise.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import azimuth_fit as fit  # noqa: E402


def rot(axis, deg):
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    t = math.radians(deg)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(t) * k + (1 - math.cos(t)) * (k @ k)


B_WORLD = np.array([0.0, 15.8, -48.5])  # ENU: north 15.8 uT, down 48.5 uT


def body_to_world(az, el):
    """Mirror body axes (front, tilt axis, normal) in ENU for a pose."""
    f = np.array([math.sin(math.radians(az)), math.cos(math.radians(az)), 0.0])
    u = np.array([0.0, 0.0, 1.0])
    k = np.cross(u, f)
    level = np.column_stack((f, k, u))
    return rot(k, 90.0 - el) @ level


# Per reading. One raw BMM150 sample varies by about 0.4 uT on the real board;
# the tool averages 20 per stop (GET /api/imu?n=20).
NOISE = 0.1


def measure(az, el, sensor_in_body, hard_iron, rng, noise=NOISE):
    world_to_sensor = (body_to_world(az, el) @ sensor_in_body).T
    return world_to_sensor @ B_WORLD + hard_iron + rng.normal(0, noise, 3)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_recovers_the_offset_whatever_way_the_atom_lies(seed):
    rng = np.random.default_rng(seed)
    sensor_in_body = rot(rng.normal(size=3), rng.uniform(0, 360))  # unknown placement
    hard_iron = rng.uniform(-40, 40, 3)  # the real board showed ~100 uT total
    true_offset = 412.5 + rng.uniform(-8, 8)

    az_stops = np.arange(72.5, 296.8, 10.0)  # the frame's real yaw range
    yaw_mag = [measure(az, 90.0, sensor_in_body, hard_iron, rng) for az in az_stops]
    yaw_servo = true_offset - az_stops  # direction -1: servo = offset - az
    el_stops = np.arange(90.0, 5.0, -10.0)  # what the tool sweeps: face-up to near-vertical
    tilt_mag = [measure(180.0, el, sensor_in_body, hard_iron, rng) for el in el_stops]

    result = fit.solve(yaw_mag, az_stops, yaw_servo, tilt_mag, el_stops, direction=-1)
    got = fit.wrap_offset(result["offset_deg"], 412.5)
    assert got == pytest.approx(true_offset, abs=0.5)
    assert result["spread_deg"] < 1.0
    assert result["radius_uT"] == pytest.approx(15.8, abs=1.0)  # the horizontal field


def test_a_wrong_yaw_direction_shows_as_a_huge_spread():
    """Treating clockwise as anticlockwise smears the per-stop offsets everywhere."""
    rng = np.random.default_rng(7)
    sensor_in_body = rot([1, 2, 3], 40)
    az_stops = np.arange(72.5, 296.8, 10.0)
    yaw_mag = [measure(az, 90.0, sensor_in_body, np.zeros(3), rng) for az in az_stops]
    el_stops = np.array([90.0, 70, 50, 30])
    tilt_mag = [measure(180.0, el, sensor_in_body, np.zeros(3), rng) for el in el_stops]
    result = fit.solve(yaw_mag, az_stops, 412.5 - az_stops, tilt_mag, el_stops, direction=+1)
    assert result["spread_deg"] > 30


def test_declination_shifts_the_offset_by_exactly_that():
    rng = np.random.default_rng(3)
    sensor_in_body = rot([0, 1, 0], 10)
    az_stops = np.arange(80.0, 290.0, 10.0)
    yaw_mag = [measure(az, 90.0, sensor_in_body, np.zeros(3), rng, noise=0) for az in az_stops]
    el_stops = np.array([90.0, 60, 30])
    tilt_mag = [measure(180.0, el, sensor_in_body, np.zeros(3), rng, noise=0) for el in el_stops]
    a = fit.solve(yaw_mag, az_stops, 400 - az_stops, tilt_mag, el_stops)
    b = fit.solve(yaw_mag, az_stops, 400 - az_stops, tilt_mag, el_stops, declination_deg=6.6)
    assert fit.wrap_offset(b["offset_deg"] - a["offset_deg"], 0) == pytest.approx(6.6, abs=1e-6)


def test_wrap_keeps_the_offset_in_the_current_turn():
    assert fit.wrap_offset(48.7, 412.5) == pytest.approx(408.7)
    assert fit.wrap_offset(415.0, 412.5) == pytest.approx(415.0)
