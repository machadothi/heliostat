"""Firmware updates over WiFi: the protocol, with the flash slot faked."""

import asyncio
import hashlib

import pytest
from net import ota as ota_mod
from net.ota import Ota

IMAGE = bytes([0xE9]) + bytes(range(256)) * 40  # ~10 KB, an app image's magic first


class FakeSlot:
    def __init__(self, size=2_500_000):
        self._size = size
        self.blocks = {}
        self.active = False

    def size(self):
        return self._size

    def write(self, index, data):
        self.blocks[index] = bytes(data)

    def activate(self):
        self.active = True


class Reader:
    def __init__(self, data):
        self.data = data

    async def readexactly(self, n):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk


class Tracker:
    mode = "idle"


class Store:
    def __init__(self, token="s3cret"):
        self.data = {"ota": {"token": token}}


def run(image=IMAGE, headers=None, mode="idle", token="s3cret", tmp_path=None):
    slot = FakeSlot()
    reboots = []
    tracker = Tracker()
    tracker.mode = mode
    ota = Ota(tracker, Store(token), lambda: reboots.append(1), slot_factory=lambda: slot,
              flag_path=str(tmp_path / "ota_pending"))
    h = {"x-ota-token": "s3cret", "x-sha256": hashlib.sha256(image).hexdigest()}
    h.update(headers or {})
    status, body = asyncio.run(ota.receive(Reader(image), len(image), h))
    return status, body, slot, reboots


def test_a_good_image_is_written_activated_and_marked_pending(tmp_path):
    status, body, slot, reboots = run(tmp_path=tmp_path)
    assert status == 200 and body["rebooting"]
    written = b"".join(slot.blocks[i] for i in sorted(slot.blocks))
    assert written[:len(IMAGE)] == IMAGE
    assert set(written[len(IMAGE):]) == {0xFF}  # the last block padded like erased flash
    assert slot.active and reboots == [1]
    assert ota_mod.pending(str(tmp_path / "ota_pending"))


def test_a_damaged_image_changes_nothing(tmp_path):
    status, body, slot, reboots = run(headers={"x-sha256": "0" * 64}, tmp_path=tmp_path)
    assert status == 400 and "SHA-256" in body["error"]
    assert not slot.active and reboots == []
    assert not ota_mod.pending(str(tmp_path / "ota_pending"))


def test_a_wrong_token_is_refused(tmp_path):
    status, _, slot, _ = run(headers={"x-ota-token": "guess"}, tmp_path=tmp_path)
    assert status == 403 and not slot.blocks


def test_no_token_configured_means_no_ota(tmp_path):
    status, body, _, _ = run(token="", tmp_path=tmp_path)
    assert status == 409 and "USB" in body["error"]


def test_not_while_tracking(tmp_path):
    status, body, slot, _ = run(mode="track", tmp_path=tmp_path)
    assert status == 409 and not slot.blocks


def test_something_that_is_not_firmware_is_refused(tmp_path):
    junk = b"PK\x03\x04" + bytes(5000)
    status, body, slot, _ = run(image=junk, tmp_path=tmp_path)
    assert status == 400 and "image" in body["error"] and not slot.active


def test_mark_valid_clears_the_flag(tmp_path):
    flag = tmp_path / "ota_pending"
    flag.write_text("1")
    ota_mod.mark_valid(str(flag))
    assert not ota_mod.pending(str(flag))


def test_a_board_without_ota_slots_says_so(tmp_path):
    def no_slots():
        raise OSError("no next update partition")

    ota = Ota(Tracker(), Store(), lambda: None, slot_factory=no_slots, flag_path=str(tmp_path / "f"))
    h = {"x-ota-token": "s3cret", "x-sha256": hashlib.sha256(IMAGE).hexdigest()}
    status, body = asyncio.run(ota.receive(Reader(IMAGE), len(IMAGE), h))
    assert status == 409 and "USB" in body["error"]


def test_the_http_server_streams_a_big_upload_to_the_handler():
    """60 KB through the real server: far over its 4 KB body limit."""
    from net.httpd import HttpServer

    received = {}

    async def sink(reader, length, headers):
        received["data"] = await reader.readexactly(length)
        received["headers"] = headers
        return 200, {"ok": True}

    async def scenario():
        server = HttpServer(api=None, host="127.0.0.1", port=0)
        server.add_stream("POST", "/api/ota", sink)
        srv = await server.start()
        port = srv.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = bytes(range(256)) * 240
        writer.write(b"POST /api/ota HTTP/1.1\r\nContent-Length: %d\r\nX-SHA256: abc\r\n\r\n" % len(body))
        writer.write(body)
        await writer.drain()
        reply = await reader.read()
        writer.close()
        srv.close()
        return body, reply

    body, reply = asyncio.run(scenario())
    assert reply.startswith(b"HTTP/1.1 200")
    assert received["data"] == body
    assert received["headers"]["x-sha256"] == "abc"
