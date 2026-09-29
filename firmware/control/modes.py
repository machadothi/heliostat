"""Operating modes and the transitions allowed between them.

Modes are plain strings so they cross the HTTP and BLE boundaries unchanged and
read properly in a log. MicroPython has no enum module, and an int would only
have to be translated at every edge.

The table is the point. Without it, "can I go from HOME to TRACK?" gets answered
slightly differently in the API handler, the tracker and the supervisor, and the
disagreement surfaces as a mirror doing something nobody asked for.
"""

IDLE = "idle"  # powered, torque on, holding position, not tracking
TRACK = "track"  # following the sun onto the target
DEFOCUS = "defocus"  # tracking loop live, but deliberately off target
STOW = "stow"  # parked at fixed mechanical angles, mirror face-down
MANUAL = "manual"  # operator jogging; automatic motion suspended
HOME = "home"  # running the homing routine
FAULT = "fault"  # a safety rule tripped; needs an explicit clear
ESTOP = "estop"  # emergency stop latched; servo power is cut in HARDWARE

ALL_MODES = (IDLE, TRACK, DEFOCUS, STOW, MANUAL, HOME, FAULT, ESTOP)

# Modes in which the tracker computes and commands positions automatically.
AUTOMATIC_MODES = (TRACK, DEFOCUS)

# Modes that require torque to be enabled.
TORQUE_MODES = (IDLE, TRACK, DEFOCUS, STOW, MANUAL, HOME)

_TRANSITIONS = {
    IDLE: (TRACK, DEFOCUS, STOW, MANUAL, HOME, FAULT, ESTOP),
    TRACK: (IDLE, DEFOCUS, STOW, FAULT, ESTOP),
    # Defocus is a holding state during a fault OR a deliberate operator choice,
    # so it can return to tracking once whatever caused it has cleared.
    DEFOCUS: (IDLE, TRACK, STOW, FAULT, ESTOP),
    # A stow caused by night or weather resumes whichever automatic mode the
    # operator had asked for -- tracking, or a deliberate defocus.
    STOW: (IDLE, TRACK, DEFOCUS, FAULT, ESTOP),
    # STOW is reachable from MANUAL and HOME so the safety supervisor can park
    # the mirror when a storm arrives or the base tilts mid-jog or mid-homing.
    # Neither can go straight to TRACK: the operator re-arms deliberately.
    MANUAL: (IDLE, STOW, FAULT, ESTOP),
    HOME: (IDLE, STOW, FAULT, ESTOP),
    # A fault must be acknowledged. It cannot go straight back to tracking --
    # somebody has to look at the machine first.
    FAULT: (IDLE, STOW, ESTOP),
    # E-stop is latching by design. The only way out is the physical twist-release
    # plus an explicit API call, which lands in IDLE.
    ESTOP: (IDLE,),
}


def is_valid(mode):
    return mode in ALL_MODES


def can_transition(current, requested):
    """Is this mode change allowed?"""
    if current == requested:
        return True
    return requested in _TRANSITIONS.get(current, ())


def allowed_from(current):
    """Every mode reachable from `current`, for the app to grey out the rest."""
    return _TRANSITIONS.get(current, ())


def assert_transition(current, requested):
    """Raise ValueError on an illegal transition."""
    if not is_valid(requested):
        raise ValueError(f"unknown mode: {requested}")
    if not can_transition(current, requested):
        raise ValueError(
            "illegal transition {} -> {} (allowed: {})".format(
                current, requested, ", ".join(allowed_from(current))
            )
        )


def is_automatic(mode):
    """True when the tracker is driving the mirror on its own."""
    return mode in AUTOMATIC_MODES


def wants_torque(mode):
    """True when the servos should be holding position."""
    return mode in TORQUE_MODES
