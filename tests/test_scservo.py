"""The servo driver's half-duplex echo detection, against a fake serial line.

Found on the bench: the SC09 answered its first ping with status byte 0x01, which
makes the reply byte-identical to the ping itself. The old detection compared
content only, concluded the adapter was echoing, and from then on discarded
every real reply as "echo" -- the servo appeared dead. Detection must go by
byte count: a real echo delivers the packet AND a full reply.
"""

import sys
import types

import pytest


class FakeUART:
    """Answers each write with a scripted list of reply chunks."""

    def __init__(self, *args, **kwargs):
        self.replies = []
        self._rx = b""

    def write(self, data):
        self._rx += self.replies.pop(0) if self.replies else b""

    def any(self):
        return len(self._rx)

    def read(self, n=None):
        if not self._rx:
            return None
        n = len(self._rx) if n is None else n
        chunk, self._rx = self._rx[:n], self._rx[n:]
        return chunk


@pytest.fixture
def scservo(monkeypatch):
    fake_machine = types.ModuleType("machine")
    fake_machine.UART = FakeUART
    monkeypatch.setitem(sys.modules, "machine", fake_machine)
    if "time" in sys.modules and not hasattr(sys.modules["time"], "ticks_ms"):
        import time

        monkeypatch.setattr(time, "ticks_ms", lambda: int(time.monotonic() * 1000), raising=False)
        monkeypatch.setattr(time, "ticks_add", lambda a, b: a + b, raising=False)
        monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    sys.modules.pop("hal.scservo", None)
    import hal.scservo as module

    yield module
    sys.modules.pop("hal.scservo", None)


def _ping(sid):
    return bytes((0xFF, 0xFF, sid, 2, 1, (~(sid + 3)) & 0xFF))


def _status(sid, error):
    return bytes((0xFF, 0xFF, sid, 2, error, (~(sid + 2 + error)) & 0xFF))


def test_a_reply_identical_to_the_ping_is_not_mistaken_for_echo(scservo):
    bus = scservo.SCSBus(timeout=5)
    reply = _status(2, error=1)
    assert reply == _ping(2)  # the trap: byte-identical

    bus.uart.replies = [reply, _status(2, error=1)]
    assert bus.ping(2)
    assert bus._echo is False
    assert bus.ping(2), "a second transaction must still see the reply"


def test_a_genuine_echo_is_still_detected(scservo):
    bus = scservo.STSBus(timeout=5)
    bus.uart.replies = [_ping(1) + _status(1, 0), _ping(1) + _status(1, 0)]
    assert bus.ping(1)
    assert bus._echo is True
    assert bus.ping(1)


def test_a_normal_reply_means_no_echo(scservo):
    bus = scservo.STSBus(timeout=5)
    bus.uart.replies = [_status(1, 0)]
    assert bus.ping(1)
    assert bus._echo is False
