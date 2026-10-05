"""Network discovery: the phone finds a heliostat it set up, with no Bluetooth
and no typed address -- including after DHCP moved it to a new IP."""

import asyncio
import json
import socket

from net.discovery import PROBE, DiscoveryResponder
from test_api import _api

DESCRIPTION = {"service": "heliostat", "id": "84cca85ed290", "name": "my_heliostat", "port": 80}


def test_a_probe_gets_a_json_description():
    responder = DiscoveryResponder(lambda: DESCRIPTION)
    reply = responder.reply_for(PROBE + b" 1")
    assert json.loads(reply) == DESCRIPTION


def test_anything_else_is_ignored():
    responder = DiscoveryResponder(lambda: DESCRIPTION)
    assert responder.reply_for(b"") is None
    assert responder.reply_for(b"GET / HTTP/1.1") is None


def test_the_api_describes_who_and_where_it_is(tmp_path):
    api, tracker, store = _api(tmp_path)
    described = api.describe()
    assert described["service"] == "heliostat"
    assert described["name"] == store["ble"]["name"]
    assert described["port"] == 80
    assert described["mode"] == tracker.mode
    assert len(described["id"]) == 12
    # The same id the phone later reads from /api/status, to confirm it found
    # the heliostat it remembers.
    status = api.get_status({}, {})
    assert status["device"]["id"] == described["id"]


def test_round_trip_over_real_udp():
    """The responder as the board runs it, answering a probe on localhost."""

    async def scenario():
        probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe_socket.bind(("127.0.0.1", 0))
        free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()

        responder = DiscoveryResponder(lambda: DESCRIPTION, port=port)
        task = asyncio.ensure_future(responder.run())
        await asyncio.sleep(0.05)
        probe_socket.sendto(PROBE + b" 1", ("127.0.0.1", port))
        probe_socket.setblocking(False)
        for _ in range(40):
            await asyncio.sleep(0.05)
            try:
                data, _ = probe_socket.recvfrom(512)
                break
            except BlockingIOError:
                continue
        else:
            raise AssertionError("no reply")
        task.cancel()
        probe_socket.close()
        return json.loads(data), responder.answered

    reply, answered = asyncio.run(scenario())
    assert reply == DESCRIPTION
    assert answered == 1
