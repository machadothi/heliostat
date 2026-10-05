"""Find heliostats on the local network from the laptop (docs/protocol.md section 3)."""

import json
import socket
import time

PORT = 47474
PROBE = b"HELIOSTAT_DISCOVER 1"


def find(broadcast="255.255.255.255", wait_s=2.0):
    """[{host, id, name, board, mode, fw, port}] for every heliostat that answers."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    found = {}
    try:
        for _ in range(2):
            s.sendto(PROBE, (broadcast, PORT))
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(1024)
            except socket.timeout:
                continue
            try:
                reply = json.loads(data)
            except ValueError:
                continue
            if reply.get("service") == "heliostat":
                port = reply.get("port", 80)
                reply["host"] = ip if port == 80 else f"{ip}:{port}"
                found[reply["id"]] = reply
    finally:
        s.close()
    return list(found.values())


if __name__ == "__main__":
    for h in find():
        print(f"{h['host']:22s} {h['id']}  {h['name']}  {h.get('board', '?'):13s} {h['mode']}")
