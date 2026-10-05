"""Answer "is there a heliostat here?" on the local network, over UDP.

The phone broadcasts a probe to UDP port DISCOVERY_PORT (and, where broadcasts
are filtered, sends it to every address in its subnet); every heliostat that
hears it replies to the sender with one JSON datagram saying who it is and where
its HTTP API lives. That is how the app finds a board it already set up without
Bluetooth or a typed address, including after DHCP gives the board a new IP.

Why not mDNS/DNS-SD: MicroPython's built-in mDNS responder answers only the
hostname (heliostat.local), offers no service registration, and owns UDP 5353.
Android also resolves .local names unreliably. A probe/reply of our own is a
few lines, works on both sides, and returns every heliostat in one round.

Request:  b"HELIOSTAT_DISCOVER 1"  (anything else is ignored)
Reply:    {"service": "heliostat", "protocol": 1, "id": "84cca85ed290",
           "name": "my_heliostat", "fw": "0.1.0", "port": 80, "mode": "idle"}

Runs unchanged on MicroPython and CPython (tools/mock_server.py uses it).
"""

import asyncio
import json
import socket

DISCOVERY_PORT = 47474
PROBE = b"HELIOSTAT_DISCOVER"
POLL_S = 0.2


class DiscoveryResponder:
    def __init__(self, describe, port=DISCOVERY_PORT):
        """`describe` returns the reply dict; called per probe so it stays current."""
        self.describe = describe
        self.port = port
        self.answered = 0

    def reply_for(self, datagram):
        """The bytes to send back for one received datagram, or None to ignore it."""
        if not datagram.startswith(PROBE):
            return None
        return json.dumps(self.describe()).encode()

    async def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(socket.getaddrinfo("0.0.0.0", self.port)[0][-1])
        sock.setblocking(False)
        while True:
            # Polled rather than awaited: MicroPython's asyncio has no datagram
            # API, and five cheap checks a second is plenty for a human tapping
            # "search".
            try:
                datagram, sender = sock.recvfrom(64)
            except OSError:  # nothing waiting (EAGAIN); CPython: BlockingIOError
                await asyncio.sleep(POLL_S)
                continue
            try:
                reply = self.reply_for(datagram)
                if reply is not None:
                    sock.sendto(reply, sender)
                    self.answered += 1
            except Exception:  # noqa: BLE001 - one bad probe must not stop discovery
                pass
