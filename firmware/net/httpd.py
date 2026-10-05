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
# A streamed upload (a firmware image) may take a minute on weak WiFi; instead
# of one deadline for the whole body, each chunk must arrive within this.
STREAM_CHUNK_TIMEOUT_S = 15

_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
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
        self._streams = {}

    def add_stream(self, method, path, handler):
        """Route a request whose body is too big to buffer (> MAX_BODY) to
        `await handler(reader, length, headers) -> (status, dict)`, which reads
        the body itself. Headers arrive lower-cased."""
        self._streams[(method, path)] = handler

    async def start(self):
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        return self._server

    async def _serve(self, reader, writer):
        status, payload = 500, {"error": "internal error"}
        try:
            head = await asyncio.wait_for(self._read_head(reader), READ_TIMEOUT_S)
            if len(head) == 2:  # (status, error payload)
                status, payload = head
            else:
                method, path, query, length, headers = head
                stream = self._streams.get((method, path))
                if stream is not None:
                    status, payload = await stream(_TimedReader(reader), length, headers)
                else:
                    status, payload = await asyncio.wait_for(
                        self._dispatch(reader, method, path, query, length), READ_TIMEOUT_S
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

    async def _read_head(self, reader):
        """(method, path, query, length, headers), or (status, error) if malformed."""
        request_line = (await reader.readline()).decode().strip()
        if not request_line:
            return 400, {"error": "empty request"}
        parts = request_line.split()
        if len(parts) != 3:
            return 400, {"error": "bad request line"}
        method, target, _ = parts

        headers = {}
        for _ in range(MAX_HEADER_LINES):
            line = (await reader.readline()).decode().strip()
            if not line:
                break
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        path, query = parse_target(target)
        return method.upper(), path, query, length, headers

    async def _dispatch(self, reader, method, path, query, length):
        if length > MAX_BODY:
            return 413, {"error": f"body larger than {MAX_BODY} bytes"}
        body = (await reader.readexactly(length)).decode() if length else ""
        return await self.api.handle(method, path, query, body)

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


class _TimedReader:
    """The request stream, with a deadline per read rather than per request."""

    def __init__(self, reader):
        self._reader = reader

    async def readexactly(self, n):
        return await asyncio.wait_for(self._reader.readexactly(n), STREAM_CHUNK_TIMEOUT_S)
