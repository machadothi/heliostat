"""The real servo axis against a fake bus: what it puts on the wire."""

import sys
import types

import pytest

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


class RegisterBus:
    """A servo's register file, little-endian like the STS family."""

    def __init__(self):
        self.mem = bytearray(80)
        self.mem[26] = self.mem[27] = 4  # what the bench suggested
        self.calls = []

    def read(self, sid, addr, n):
        return bytes(self.mem[addr:addr + n])

    def read_byte(self, sid, addr):
        return self.mem[addr]

    def write_byte(self, sid, addr, value):
        self.calls.append(("write", addr, value))
        self.mem[addr] = value

    def unlock_eeprom(self, sid):
        self.calls.append(("unlock",))

    def lock_eeprom(self, sid):
        self.calls.append(("lock",))

    def _unpack(self, data, offset=0):
        return data[offset] | (data[offset + 1] << 8)


def _axis(bus):
    return ServoAxis(Axis(name="el", servo_id=1, family="STS", offset_deg=122.6, min_deg=-30, max_deg=180), bus)


def test_tuning_unlocks_writes_and_locks_the_eeprom():
    bus = RegisterBus()
    tuned = _axis(bus).tune({"cw_dead": 1, "ccw_dead": 1})
    assert bus.calls == [("unlock",), ("write", 26, 1), ("write", 27, 1), ("lock",)]
    assert tuned["cw_dead"] == 1 and tuned["ccw_dead"] == 1


def test_tuning_refuses_unknown_registers_and_bad_values_before_writing():
    bus = RegisterBus()
    with pytest.raises(ValueError, match="not tunable"):
        _axis(bus).tune({"id": 7})  # renumbering stays a job for tools/servo_id.py
    with pytest.raises(ValueError, match="0..32"):
        _axis(bus).tune({"cw_dead": 200})
    assert bus.calls == []


def test_registers_read_back_by_name():
    regs = _axis(RegisterBus()).registers()
    assert regs["cw_dead"] == 4 and "offset" in regs  # STS-only registers included
