#!/usr/bin/env python3
"""Provision the heliostat over BLE from a laptop -- what the app will do.

    python3 tools/provision.py info
    python3 tools/provision.py scan
    python3 tools/provision.py join --ssid MACHADO_HOME        # asks for the password
    python3 tools/provision.py time                            # send this laptop's clock
    python3 tools/provision.py location --lat 38.72 --lon -9.14

The password is read with getpass, locally, and goes straight to the board. It
is never printed or logged.

This is also the M7 test harness: it talks to the GATT service exactly as the
Android app must (docs/protocol.md), including the chunked SSID/PSK framing, so
the firmware side can be proven before a line of Kotlin exists.

Requires `pip install bleak` and a Bluetooth adapter.
"""

import argparse
import asyncio
import getpass
import json
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

BASE = "8a4f{}-5a10-4c6b-9e2a-68656c696f73"
SERVICE = BASE.format("1000")
INFO, SSID, PSK, COMMAND, WIFI_STATE, SCAN, TIME, LOCATION = (
    BASE.format(n) for n in ("1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008")
)
CMD_JOIN, CMD_SCAN, CMD_FORGET = 1, 2, 3
STATES = {0: "idle", 1: "joining", 2: "joined", 3: "failed", 4: "scanning", 5: "scan_ready"}
REASONS = {0: "", 1: "wrong password", 2: "network not found", 3: "timed out", 4: "failed"}
SEQ_FINAL = 0x80
CHUNK = 18  # safe at the default MTU of 23: 20-byte payload minus the seq byte


def chunks(text):
    data = text.encode()
    pieces = [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
    last = len(pieces) - 1
    return [bytes([i | (SEQ_FINAL if i == last else 0)]) + p for i, p in enumerate(pieces)]


def decode_state(value):
    state, reason = value[0], value[1]
    ip = ".".join(str(b) for b in value[2:6])
    return STATES.get(state, state), REASONS.get(reason, reason), ip


async def find(name, timeout):
    print(f"looking for '{name}'...")
    device = await BleakScanner.find_device_by_name(name, timeout=timeout)
    if device is None:
        sys.exit(f"'{name}' not found. Is BLE on? (unprovisioned, or hold BOOT for 3 s)")
    print(f"found {device.address}")
    return device


async def wait_for(states, queue, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = await asyncio.wait_for(queue.get(), deadline - time.monotonic())
        except asyncio.TimeoutError:
            break
        state, reason, ip = decode_state(value)
        print(f"  state: {state} {reason} {ip if state == 'joined' else ''}".rstrip())
        if state in states:
            return state, reason, ip
    sys.exit("timed out waiting for the board")


async def main(args):
    device = await find(args.name, args.timeout)
    async with BleakClient(device) as client:
        queue = asyncio.Queue()
        await client.start_notify(WIFI_STATE, lambda _c, data: queue.put_nowait(bytes(data)))

        if args.command == "info":
            print(json.dumps(json.loads(await client.read_gatt_char(INFO)), indent=2))

        elif args.command == "scan":
            await client.write_gatt_char(COMMAND, bytes([CMD_SCAN]), response=True)
            await wait_for({"scan_ready"}, queue, 15)
            text = (await client.read_gatt_char(SCAN)).decode()
            print(f"{'network':<30}{'ch':>4}{'dBm':>6}")
            for line in text.splitlines():
                ssid, channel, rssi = line.split("\t")
                print(f"{ssid:<30}{channel:>4}{rssi:>6}")

        elif args.command == "join":
            ssid = args.ssid
            psk = args.psk if args.psk is not None else getpass.getpass(f"password for {ssid}: ")
            for piece in chunks(ssid):
                await client.write_gatt_char(SSID, piece, response=True)
            for piece in chunks(psk):
                await client.write_gatt_char(PSK, piece, response=True)
            await client.write_gatt_char(COMMAND, bytes([CMD_JOIN]), response=True)
            state, reason, ip = await wait_for({"joined", "failed"}, queue, 40)
            if state == "joined":
                print(f"joined {ssid}: http://{ip}/api/status")
            else:
                sys.exit(f"could not join {ssid}: {reason}")

        elif args.command == "time":
            tz_minutes = -time.timezone // 60 if not time.daylight else -time.altzone // 60
            await client.write_gatt_char(TIME, struct.pack("<Ih", int(time.time()), tz_minutes),
                                         response=True)
            print("time sent")

        elif args.command == "location":
            await client.write_gatt_char(
                LOCATION, struct.pack("<fff", args.lat, args.lon, args.elev), response=True
            )
            print("location sent")

        elif args.command == "forget":
            await client.write_gatt_char(COMMAND, bytes([CMD_FORGET]), response=True)
            print("stored network cleared")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("command", choices=["info", "scan", "join", "time", "location", "forget"])
    parser.add_argument("--name", default="my_heliostat")
    parser.add_argument("--ssid")
    parser.add_argument("--psk", help="avoid: prefer the interactive prompt")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--elev", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    if args.command == "join" and not args.ssid:
        parser.error("join needs --ssid")
    if args.command == "location" and (args.lat is None or args.lon is None):
        parser.error("location needs --lat and --lon")
    asyncio.run(main(args))
