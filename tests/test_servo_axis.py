"""The real servo axis against a fake bus: what it puts on the wire."""

import sys
import types

from control.kinematics import Axis

# hal.scservo imports machine.UART at module level; the bus here is a fake anyway.
sys.modules.setdefault("machine", types.SimpleNamespace(UART=None))
from hal.scservo import REG_GOAL_POSITION, REG_PRESENT_POSITION  # noqa: E402
from hal.servo_axis import ServoAxis  # noqa: E402



class FakeBus:
    def __init__(self, present):
        self.present = present
        self.calls = []

    def read_word(self, sid, addr):
        self.calls.append(("read", sid, addr))
        assert addr == REG_PRESENT_POSITION
        return self.present

    def write_word(self, sid, addr, value):
        self.calls.append(("write", sid, addr, value))

    def torque(self, sid, on):
        self.calls.append(("torque", sid, on))


def test_enabling_torque_first_sets_the_goal_to_the_present_position():
    """Otherwise the servo resumes its last goal, which may be into the frame."""
    bus = FakeBus(present=2900)
    axis = ServoAxis(Axis(name="el", servo_id=2, family="STS", offset_deg=180.0, min_deg=-90, max_deg=90), bus)
    axis.torque(True)
    assert bus.calls == [
        ("read", 2, REG_PRESENT_POSITION),
        ("write", 2, REG_GOAL_POSITION, 2900),
        ("torque", 2, True),
    ]


def test_releasing_torque_touches_nothing_else():
    bus = FakeBus(present=2900)
    axis = ServoAxis(Axis(name="el", servo_id=2, family="STS", offset_deg=180.0, min_deg=-90, max_deg=90), bus)
    axis.torque(False)
    assert bus.calls == [("torque", 2, False)]
