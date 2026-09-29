"""The transform between mechanical angles and servo angles, plus backlash.

Two different angles are easy to confuse, so they are named apart throughout:

    MECHANICAL angle  what the joint is physically doing, in the mount's own
                      convention -- 0 degrees elevation means the mirror faces
                      the horizon, and so on.

    SERVO angle       what the servo's encoder reads. Related to the mechanical
                      angle by a zero offset, a direction, and a gear ratio.

        servo = offset + direction * gear_ratio * mechanical

`offset_deg` is what the homing routine measures and writes to config once. It
is the only one of the three that is not known from the drawing.
"""


def to_servo_deg(mech_deg, offset_deg, direction, gear_ratio):
    """Mechanical angle -> servo angle."""
    return offset_deg + direction * gear_ratio * mech_deg


def from_servo_deg(servo_deg, offset_deg, direction, gear_ratio):
    """Servo angle -> mechanical angle. Exact inverse of to_servo_deg."""
    return direction * (servo_deg - offset_deg) / gear_ratio


def offset_from_known_position(servo_deg, known_mech_deg, direction, gear_ratio):
    """Derive `offset_deg` from a joint whose true angle you know.

    Two ways this gets used:
      * homing -- the limit switch sits at a surveyed mechanical angle;
      * manual calibration -- the operator jogs to a measured angle and says so.

    The caller writes the result to config explicitly. Nothing auto-saves; flash
    has a finite number of write cycles and the control loop must never spend one.
    """
    return servo_deg - direction * gear_ratio * known_mech_deg


def apply_backlash(target_mech_deg, previous_target_mech_deg, backlash_deg):
    """Return the angle to command so the joint always arrives from one side.

    Gear trains have play. Approaching an angle from the left and from the right
    leaves the output in two different places, and the difference is the
    backlash. The fix is to always arrive from the same direction: when a command
    reverses, overshoot past the target and come back.

    Returns (approach_deg, needs_overshoot). When `needs_overshoot` is True the
    caller moves to `approach_deg` first, then to the real target.

    Returns the target unchanged when there is no configured backlash, when the
    direction has not reversed, or when there is no previous command.

    Tracking naturally moves one way for hours at a stretch, so this fires rarely
    -- around solar noon, at a mode change, and during jogging. That is exactly
    when it matters.
    """
    if backlash_deg <= 0.0 or previous_target_mech_deg is None:
        return target_mech_deg, False

    delta = target_mech_deg - previous_target_mech_deg
    if delta == 0.0:
        return target_mech_deg, False

    # Always settle onto the target moving in the POSITIVE direction, so the
    # gear teeth are loaded the same way every time.
    if delta > 0.0:
        return target_mech_deg, False
    return target_mech_deg - backlash_deg, True


def steps_per_degree(steps, degrees):
    """Encoder resolution, for deadband and error-budget calculations.

    ST3020: 4096 steps / 360 deg  -> 11.38 steps/deg (0.088 deg per step)
    SC09:   1024 steps / 300 deg  ->  3.41 steps/deg (0.293 deg per step)
    """
    return steps / degrees


def deadband_deg(steps, degrees, steps_of_deadband=1.0):
    """Smallest command worth issuing, in degrees.

    The sun moves 0.00417 deg/s and the mirror normal half that, so one ST3020
    step takes 42 seconds to accumulate and one SC09 step takes 141. Commanding
    anything smaller just makes the servo hunt and whine for nothing.
    """
    return steps_of_deadband * degrees / steps
