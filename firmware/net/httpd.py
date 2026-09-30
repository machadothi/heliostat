"""A small asyncio HTTP/1.1 server. Just enough for a JSON API, and no more.

Runs unchanged on MicroPython and on CPython (both provide asyncio.start_server
with the same stream API), so tools/mock_server.py uses this very file.

Deliberately limited:
  * one request per connection (Connection: close) -- OkHttp copes fine, and it
    keeps the server free of connection state on a board with ~90 KB of RAM;
  * bodies capped at MAX_BODY, since the only large thing anyone should ever
    send is a config update;
  * no chunked encoding, no TLS. It lives on a home LAN.

Every connection is handled with a timeout, so a client that opens a socket and
goes quiet cannot tie up the event loop the control loop also runs on.
"""

import asyncio
import json

MAX_BODY = 4096
MAX_HEADER_LINES = 40
READ_TIMEOUT_S = 5

_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error"}


def parse_target(target):
    """Split '/api/sun?t=123&lat=4' into ('/api/sun', {'t': '123', 'lat': '4'})."""
    path, _, query_string = target.partition("?")
    query = {}
    for pair in query_string.split("&"):
        if pair:
            key, _, value = pair.partition("=")
            query[key] = value
    return path, query


class HttpServer:
    def __init__(self, api, host="0.0.0.0", port=80):
        self.api = api
        self.host = host
        self.port = port
        self.requests = 0
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        return self._server

    async def _serve(self, reader, writer):
        status, payload = 500, {"error": "internal error"}
        try:
            status, payload = await asyncio.wait_for(
                self._read_and_dispatch(reader), READ_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            status, payload = 400, {"error": "request timed out"}
        except Exception as exc:  # noqa: BLE001
            status, payload = 400, {"error": f"malformed request: {exc}"}
        try:
            await self._respond(writer, status, payload)
        except Exception:  # noqa: BLE001 - the client went away; nothing to do
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self.requests += 1

    async def _read_and_dispatch(self, reader):
        request_line = (await reader.readline()).decode().strip()
        if not request_line:
            return 400, {"error": "empty request"}
        parts = request_line.split()
        if len(parts) != 3:
            return 400, {"error": "bad request line"}
        method, target, _ = parts

        length = 0
        for _ in range(MAX_HEADER_LINES):
            line = (await reader.readline()).decode().strip()
            if not line:
                break
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())

        if length > MAX_BODY:
            return 413, {"error": f"body larger than {MAX_BODY} bytes"}
        body = (await reader.readexactly(length)).decode() if length else ""

        path, query = parse_target(target)
        return await self.api.handle(method.upper(), path, query, body)

    async def _respond(self, writer, status, payload):
        body = json.dumps(payload).encode()
        head = (
            "HTTP/1.1 {} {}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n\r\n"
        ).format(status, _REASONS.get(status, "Unknown"), len(body))
        writer.write(head.encode())
        writer.write(body)
        await writer.drain()
