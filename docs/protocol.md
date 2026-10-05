# Heliostat protocol

The contract between the firmware and anything that talks to it: the Android app,
`tools/provision.py`, or you with nRF Connect. The firmware side is
`firmware/ble/gatt_service.py` and `firmware/net/api.py`. If either changes, this
file changes with it.

Setup has two stages:

1. **BLE**: hand the ESP32 the credentials of the phone's own WiFi network, and
   optionally the time and location.
2. **HTTP over the home network**: everything else, once the board has joined.
   The app finds boards on the network by **UDP discovery** (section 3), so BLE
   is needed only once per board.

---

## 1. BLE provisioning

### When BLE is on

The board advertises as **`my_heliostat`** (config: `ble.name`) when:

- it has no stored network (first boot, or after *forget*);
- its stored network has been unreachable for **5 minutes**;
- **BOOT is held for 3 s**, which opens a 5-minute window.

It turns BLE off about **15 s after a successful join**, giving the phone time to
read the IP.

While a phone is connected the board **stops advertising**. It accepts one
connection at a time. If a second device can't find it, check whether a phone is
still connected (nRF Connect keeps the connection open until you tap *Disconnect*).

The status LED on GPIO2 shows three quick pulses, repeating, while BLE is on.

### Advertisement

| Packet | Content |
|---|---|
| Advertisement | Flags, **Complete Local Name** `my_heliostat` |
| Scan response | Complete list of 128-bit service UUIDs: the service below |

They're split because name + flags + a 128-bit UUID is 35 bytes, and one
advertisement holds 31. Apps should filter scans on the **service UUID** and
display the name.

### Service

**`8a4f1000-5a10-4c6b-9e2a-68656c696f73`**: Heliostat provisioning.

The last 12 hex digits spell `helios` in ASCII. GATT has no standard way to name a
*service*, so nRF Connect shows it as "Unknown Service". The characteristics do
carry names, in a User Description descriptor (0x2901), which nRF Connect displays.

All multi-byte numbers are **little-endian**.

| UUID suffix | Name in nRF Connect | Access | Payload |
|---|---|---|---|
| `1001` | Info (JSON) | read | UTF-8 JSON |
| `1002` | WiFi SSID (text) | write | UTF-8 text |
| `1003` | WiFi password (text) | write | UTF-8 text |
| `1004` | Command: 01 join, 02 scan, 03 forget | write | 1 byte |
| `1005` | WiFi state: state,reason,ip[4] | read, notify | 6 bytes |
| `1006` | WiFi scan (text) | read | UTF-8 text |
| `1007` | Time: u32 unix, i16 tz min (LE) | write | 6 bytes |
| `1008` | Location: f32 lat, lon, elev (LE) | write | 12 bytes |

Full UUIDs follow the pattern `8a4fXXXX-5a10-4c6b-9e2a-68656c696f73`.

---

#### `1001` Info — read

UTF-8 JSON. In nRF Connect, switch the value display to **UTF-8/text**.

```json
{"service": "Heliostat provisioning", "fw": "0.1.0", "name": "my_heliostat",
 "wifi": "idle", "ip": null, "ssid": ""}
```

`wifi` is one of `idle`, `joining`, `up`, `down`.

---

#### `1002` WiFi SSID and `1003` WiFi password — write

The network name and its password, UTF-8. Maximum 32 bytes for the SSID and 64
for the password.

**Simplest (nRF Connect):** write the value as **TEXT** in one write.

    SSID  MACHADO_HOME      → bytes 4D 41 43 48 41 44 4F 5F 48 4F 4D 45

This works whenever the whole value fits in one write. nRF Connect negotiates a
large MTU, so it always does there.

**Framed (the app):** at the default MTU of 23, a write carries only 20 bytes, so
the app sends values in chunks. Each chunk starts with a **sequence byte**:
`00`, `01`, `02`, … with bit 7 set (`0x80`) on the **last** chunk. The chunks'
data is concatenated.

    one chunk     80 4D 41 43 48 41 44 4F 5F 48 4F 4D 45          ("MACHADO_HOME")

    two chunks    00 61 2D 6C 6F 6E 67 2D 70 61 73 73 77 6F 72 64 2D 6F 76
                  81 65 72 2D 31 38 2D 62 79 74 65 73
                  → "a-long-password-over-18-bytes"

How the board tells the two forms apart: a first byte in `00`–`0F` or `80`–`8F`
is a sequence byte; anything else means the whole write is the value. UTF-8 text
never starts with a byte in `80`–`8F`, so they can't be confused. A chunk with
sequence `00` always starts a new value, so an interrupted write can't corrupt
the next one.

Writing SSID/password only stores them in RAM. Nothing happens until **Command 01**.

The password can't be read back. It's saved to flash only after a successful join.

---

#### `1004` Command — write, 1 byte

| Byte | Command | What happens |
|---|---|---|
| `01` | join | Try the SSID + password written above. Progress and outcome arrive on `1005`. On success the credentials are saved. |
| `02` | scan | Scan for WiFi networks. `1005` goes to *scanning*, then *scan ready*; then read `1006`. |
| `03` | forget | Clear the stored network. |

---

#### `1005` WiFi state — read, notify, 6 bytes

**Enable notifications before sending a command** (the triple-arrow icon in
nRF Connect), so you see each change as it happens.

| Byte | Meaning |
|---|---|
| 0 | state |
| 1 | reason, when state is *failed* |
| 2–5 | IPv4 address, when state is *joined* (e.g. `C0 A8 32 4D` = 192.168.50.77) |

| State | | Reason | |
|---|---|---|---|
| `00` | idle | `00` | none |
| `01` | joining | `01` | wrong password |
| `02` | joined | `02` | network not found (includes 5 GHz-only networks; see below) |
| `03` | failed | `03` | timed out (e.g. no DHCP address) |
| `04` | scanning | `04` | other |
| `05` | scan ready | | |

Examples:

    02 00 C0 A8 32 4D     joined, IP 192.168.50.77
    03 01 00 00 00 00     failed: wrong password
    05 00 00 00 00 00     scan results ready in 1006

---

#### `1006` WiFi scan — read

UTF-8 text, one network per line, strongest first, fields separated by a TAB:

    MACHADO_HOME<TAB>1<TAB>-78
    UniFi Wireless<TAB>6<TAB>-76

Fields: SSID, channel, signal (dBm). Up to 512 bytes. Read it after state `05`.

**The ESP32 is 2.4 GHz only.** Networks on 5 GHz never appear here. If the phone is
on `MACHADO_HOME_5G`, offer the 2.4 GHz sibling `MACHADO_HOME` from this list.
Dual-band routers usually use the same password for both.

---

#### `1007` Time — write, 6 bytes

| Bytes | Type | Meaning |
|---|---|---|
| 0–3 | u32 LE | Unix time, seconds (UTC) |
| 4–5 | i16 LE | the phone's timezone offset, minutes (display only) |

Example: 2026-09-29 16:40:00 UTC, UTC+2 →

    E0 E9 BB 6A 78 00

`1790700000` = `0x6ABBE9E0` → `E0 E9 BB 6A`; `120` = `0x0078` → `78 00`.

Once the board is on the network it also sets its own clock by NTP. Whichever
source spoke last wins.

---

#### `1008` Location — write, 12 bytes (or 8)

| Bytes | Type | Meaning |
|---|---|---|
| 0–3 | f32 LE | latitude, degrees, north positive |
| 4–7 | f32 LE | longitude, degrees, **east positive** |
| 8–11 | f32 LE | elevation, metres (optional) |

Example: 38.7223 N, 9.1393 W, 50 m →

    A3 E3 1A 42  93 3A 12 C1  00 00 48 42

Saved to flash. It sets where the firmware computes the sun from.

---

### Provisioning, step by step (nRF Connect)

1. Scan, find **my_heliostat**, connect.
2. Open the service `8a4f1000-…`. Enable notifications on **WiFi state (1005)**.
3. Optional: write `02` to **Command**, wait for `05 …` on WiFi state, then read
   **WiFi scan** as text to see which networks the ESP32 can hear.
4. Write the SSID as TEXT to **WiFi SSID**, and the password as TEXT to
   **WiFi password**.
5. Write `01` to **Command**.
6. Watch **WiFi state**: `01` (joining), then `02 00 <ip>` on success, or
   `03 <reason>`.
7. Optional: write **Time** and **Location**.
8. Disconnect. BLE turns off within about 15 s, and the board is at
   `http://<ip>/api/status` and `http://heliostat.local/api/status`.

Or from a laptop with Bluetooth: `python3 tools/provision.py join --ssid MACHADO_HOME`
(asks for the password).

---

## 2. HTTP API

Plain HTTP on port 80, JSON in and out, one request per connection. The board
answers at its IP and at **`heliostat.local`**. Every request counts as contact
for the comms-loss rule.

Errors are always `{"error": "..."}`:

| Status | Meaning |
|---|---|
| 400 | malformed request, or a value the config rejects |
| 404 / 405 | unknown path / wrong method |
| 409 | valid, but refused right now (latched fault, no valid time, wrong mode) |
| 413 | body over 4 KB |
| 500 | a bug |

Angles are degrees. Azimuth is from true north, clockwise, 0–360. Elevation is
above the horizon.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/status` | | everything: mode, intent, latch, sun, plan, beam, efficiency, axes (each with `trim_deg`, the learned settle correction), trips, allowed modes, device (`id`, `board`, `build`, `ota_pending`, memory, `reset_cause`), wifi, time sync |
| GET | `/api/telemetry` | | the small subset for 2 Hz polling |
| GET | `/api/sun?t=<unix>&lat=&lon=` | | sun position at any time, to validate the firmware's math |
| GET | `/api/config` | | config (WiFi password masked) |
| GET | `/api/log` | | the last 40 log events, stamped `HH:MM:SSZ` (UTC). The minutely memory report goes to the console only |
| GET | `/api/imu?n=20` | | averaged raw IMU readings (n = 1–100): `accel_g`, `mag_ut` (null without a magnetometer), sensor frames. 409 if no IMU |
| GET | `/api/servo?axis=el` | | that axis's servo registers by name: limits, PID gains, dead zones, position, load, volts… 409 on simulated servos or a silent servo |
| POST | `/api/config` | partial config | deep-merged, validated, saved. WiFi can't be set here. Axis calibration applies at once; a changed servo ID or family replies `"restart_required": true` |
| POST | `/api/time` | `{"epoch": 1790700000, "tz_offset_min": 120}` | |
| POST | `/api/location` | `{"lat": 38.72, "lon": -9.14, "elev_m": 50}` | saved |
| POST | `/api/target` | `{"az": 180, "el": 10}` or `{"mode": "point", "x": 0, "y": 10, "z": 2}` | saved |
| POST | `/api/target/capture` | | target derived from where the beam lands now; saved |
| POST | `/api/mode` | `{"mode": "track"}` | `idle track defocus stow manual home estop` |
| POST | `/api/jog` | `{"axis": 0, "delta_deg": 1.0}` or `{"axis": 1, "abs_deg": 30}` | manual mode only; axis 0 = azimuth, 1 = elevation |
| POST | `/api/clear` | | clear a latched fault → idle (refused while the physical E-stop is pressed) |
| POST | `/api/wifi/forget` | | clear the network, reboot into BLE provisioning |
| POST | `/api/servo` | `{"axis": "el", "set": {"cw_dead": 1, "ccw_dead": 1}}` | tune a servo (EEPROM: unlocked, written, locked). Only `p_gain` `d_gain` `i_gain` (0–254) and `cw_dead` `ccw_dead` (0–32 steps); only in idle, manual, fault or E-stop |
| POST | `/api/ota` | the firmware image (`micropython.bin`), raw; headers `X-OTA-Token`, `X-SHA256` | update over WiFi (AtomS3R): written to the spare slot, checked, booted on probation, rolled back if it fails. Only in idle, manual, fault or E-stop. Step by step: [docs/ota.md](ota.md) |
| POST | `/api/imu/level` | | adopt the base's current attitude as level (once, after installing); saved. 409 if no IMU |

`POST /api/mode {"mode": "estop"}` is a **software** E-stop: it latches, releases
torque, and is cleared with `/api/clear`. The physical E-stop also cuts servo power
in hardware.

---

## 3. Discovery (UDP)

How the app finds heliostats already on the network: no BLE, no typed address,
and it keeps working after DHCP moves a board to a new IP. `firmware/net/discovery.py`.

Every heliostat listens on **UDP port 47474**. Send it this probe (ASCII, the
version after the space):

```
HELIOSTAT_DISCOVER 1
48 45 4C 49 4F 53 54 41 54 5F 44 49 53 43 4F 56 45 52 20 31
```

Each heliostat that hears it replies to the sender's address and port with one
JSON datagram:

```json
{"service": "heliostat", "protocol": 1, "id": "84cca85ed290",
 "name": "my_heliostat", "fw": "0.1.0", "port": 80, "mode": "idle"}
```

| Field | Meaning |
|---|---|
| `id` | 12 hex digits from the ESP32's factory MAC. Stable: remember a heliostat by this, not by its IP or name |
| `name` | the configured name (`ble.name`), for display |
| `port` | its HTTP API port (80 on a board; the mock server's own port otherwise) |
| `mode` | current mode, for the picker |

The HTTP API is then at the reply's **source address** on `port`.
`GET /api/status` reports the same `id` under `device`, so a client can confirm
that a saved address still belongs to the heliostat it remembers.

Send the probe to the subnet broadcast address (and 255.255.255.255). Some access
points filter broadcasts between WiFi clients; to cover those, also send it
unicast to each address in the subnet -- a /24 is 254 datagrams of 20 bytes.
Probes that are not `HELIOSTAT_DISCOVER` are ignored.

```bash
# from a laptop on the same network
printf 'HELIOSTAT_DISCOVER 1' | socat - UDP-DATAGRAM:192.168.50.255:47474,broadcast
```

