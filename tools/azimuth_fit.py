"""The maths behind tools/calibrate_azimuth.py: true north for the yaw encoder.

The AtomS3R's magnetometer rides ON THE MIRROR. Two sweeps:

  * YAW SWEEP, mirror face-up: the sensor turns about the vertical, so the
    field it measures traces a circle. The circle's axis is "up" in the
    sensor frame; its centre is the sensor's own magnetic bias (hard iron)
    plus the vertical part of Earth's field; centre-to-sample is the
    HORIZONTAL field, pointing to magnetic north.
  * TILT SWEEP, at one yaw stop: the mirror tips toward its front, the sensor
    turns about the horizontal tilt axis, and the field traces a second
    circle whose axis is the tilt axis k in the sensor frame. The mirror's
    front is then f = k x up -- measured, not assumed, so it does not matter
    how the Atom was placed on the mirror.

With north and the front both known in the sensor frame, each yaw stop gives
the front's magnetic azimuth; add declination for true azimuth. The axis's
`offset_deg` then follows from the servo angle read at that stop, and the
spread of those per-stop offsets is the calibration's own error estimate.

Everything stays in the magnetometer's frame: the BMI270 and BMM150 axes on
the AtomS3R's board never need to be related to each other.

Rotation senses (mirror body vs. a fixed field, seen in the sensor frame):
  * yaw clockwise from above (azimuth increasing) -> the field turns
    counter-clockwise about up: positive about up_s;
  * tipping the face toward the front = body rotation +t about k = up x f
    -> the field turns by -t about k_s.
These fix the signs of the two fitted axes.
"""

import math

import numpy as np


def fit_circle_3d(points):
    """(centre, unit normal, radius) of the best-fit circle through 3-D points."""
    p = np.asarray(points, dtype=float)
    centroid = p.mean(axis=0)
    _, _, vt = np.linalg.svd(p - centroid)
    u, v, normal = vt[0], vt[1], vt[2]
    xy = np.column_stack(((p - centroid) @ u, (p - centroid) @ v))
    # Kasa algebraic fit: x^2 + y^2 + D x + E y + F = 0
    a = np.column_stack((xy[:, 0], xy[:, 1], np.ones(len(xy))))
    b = -(xy[:, 0] ** 2 + xy[:, 1] ** 2)
    d, e, f = np.linalg.lstsq(a, b, rcond=None)[0]
    cx, cy = -d / 2, -e / 2
    radius = math.sqrt(max(cx * cx + cy * cy - f, 0.0))
    return centroid + cx * u + cy * v, normal / np.linalg.norm(normal), radius


def rotation_sense(points, centre, axis, parameter):
    """+1 if the points turn positively about `axis` as `parameter` increases, else -1."""
    p = np.asarray(points, dtype=float) - centre
    ref = p[0] - axis * (p[0] @ axis)
    ref /= np.linalg.norm(ref)
    side = np.cross(axis, ref)
    angles = np.unwrap(np.arctan2(p @ side, p @ ref))
    slope = np.polyfit(np.asarray(parameter, dtype=float), angles, 1)[0]
    return 1.0 if slope > 0 else -1.0


def solve(yaw_mag, yaw_az_mech, yaw_servo, tilt_mag, tilt_el_mech,
          direction=-1, gear_ratio=1.0, declination_deg=0.0):
    """Calibrate the azimuth axis from the two sweeps.

    yaw_mag      field (uT) at each yaw stop, mirror face-up
    yaw_az_mech  the azimuth the CURRENT calibration reports at each stop
                 (only its direction of increase matters)
    yaw_servo    the yaw servo's raw angle (deg) at each stop
    tilt_mag     field at each tilt stop (one yaw position)
    tilt_el_mech elevation at each tilt stop (decreasing = tipping to the front)

    Returns a dict: offset_deg (new), per_stop offsets, spread_deg, radius_uT,
    vertical_uT, and the fitted vectors, for the report.
    """
    yaw_mag = np.asarray(yaw_mag, dtype=float)
    centre, up, radius = fit_circle_3d(yaw_mag)
    up *= rotation_sense(yaw_mag, centre, up, yaw_az_mech)  # field turns + about up as az grows

    t_centre, k, _ = fit_circle_3d(tilt_mag)
    tilt_turned = _arc_deg(tilt_mag, t_centre, k)
    # Tipping toward the front (elevation decreasing) turns the field by -t about k.
    k *= -rotation_sense(tilt_mag, t_centre, k, -np.asarray(tilt_el_mech, dtype=float))
    k -= up * (k @ up)  # the tilt axis is horizontal: remove what noise left of "up"
    k /= np.linalg.norm(k)
    front = np.cross(k, up)

    offsets = []
    for m, servo in zip(yaw_mag, yaw_servo):
        north = m - centre
        north -= up * (north @ up)
        north /= np.linalg.norm(north)
        east = np.cross(north, up)
        magnetic_az = math.degrees(math.atan2(front @ east, front @ north))
        true_az = magnetic_az + declination_deg
        # config: servo = offset + direction * gear * mech
        offsets.append(servo - direction * gear_ratio * true_az)

    offset, spread = circular_mean_std(offsets)
    return {
        "offset_deg": offset,
        "per_stop": offsets,
        "spread_deg": spread,
        "radius_uT": radius,
        "vertical_uT": float(centre @ up),
        "up": up,
        "front": front,
        # How far the field turned during the tilt sweep: about the tilt range if
        # the sensor rides on the mirror, near zero if it does not tip with it.
        "tilt_turned_deg": tilt_turned,
    }


def _arc_deg(points, centre, axis):
    p = np.asarray(points, dtype=float) - centre
    ref = p[0] - axis * (p[0] @ axis)
    ref /= np.linalg.norm(ref)
    side = np.cross(axis, ref)
    angles = np.degrees(np.unwrap(np.arctan2(p @ side, p @ ref)))
    return float(angles.max() - angles.min())


def circular_mean_std(angles_deg):
    """(mean in [0, 360), circular standard deviation) of angles in degrees."""
    a = np.radians(np.asarray(angles_deg, dtype=float))
    c, s = np.cos(a).mean(), np.sin(a).mean()
    r = min(1.0, math.hypot(c, s))
    mean = math.degrees(math.atan2(s, c)) % 360.0
    std = math.degrees(math.sqrt(-2.0 * math.log(r))) if r > 0 else 180.0
    return mean, std


def wrap_offset(offset_deg, reference_deg):
    """The equivalent of `offset_deg` (mod 360) closest to `reference_deg`.

    Offsets above 360 are normal here (412.5 today): keep the new one in the
    same turn as the current one so the limits shift by the small difference.
    """
    return reference_deg + ((offset_deg - reference_deg + 180.0) % 360.0 - 180.0)
