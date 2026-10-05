# heliostat

A mirror that tracks the sun so the *reflected* beam stays fixed on a target.

ESP32 + MicroPython firmware driving two Waveshare serial bus servos, configured
and monitored from an Android app over BLE (provisioning) and WiFi (control).

## The idea in one equation

With `s` the unit vector from the mirror toward the sun and `t` the unit vector
from the mirror toward the target, the required mirror normal is their bisector:

```
n = normalize(s + t)
```

Two consequences worth knowing before reading any code: the mirror sweeps at
**half** the sun's rate, and a pointing error of δ in the normal puts the beam
off by **2δ**.

## Layout

```
firmware/    deployed to the board
  hal/       everything that touches a pin (plus *_sim.py doubles)
  solar/     sun position - pure math, no hardware imports
  control/   geometry, kinematics, tracking, safety - pure math except tracker/safety
  net/       WiFi, HTTP API
  ble/       GATT provisioning service
tools/       host-side scripts (deploy, simulate, validate)
tests/       pytest, runs on the host with no board
docs/        protocol.md is the firmware <-> app contract
```

`solar/` and `control/` import nothing from `hal/` or `net/`, so the entire
tracking brain runs under pytest on a laptop.

## Getting started

```bash
pip install -e '.[dev]'
python3 tools/check_board.py          # run this FIRST on a new board
pytest
python3 tools/deploy.py               # build MicroPython with the firmware frozen in, flash it
```

## Two controller boards, one firmware

| Board | Notes |
|---|---|
| **ESP32 devkit** (`esp32_devkit`) | no PSRAM; CP2102 on /dev/ttyUSB0; status LED on GPIO2; MPU6050 on external I2C |
| **M5Stack AtomS3R** (`atoms3r`) | ESP32-S3, 8 MB PSRAM (8 MB free for Python); native USB on /dev/ttyACM0; status on its screen; BMI270 IMU + BMM150 magnetometer onboard |

Build settings live in `boards/<name>.json` (see `boards/README.md`), pins in
`firmware/hal/profiles.py`. `tools/deploy.py` picks the board that is plugged in
(or `--board <name>`) and freezes its name into the build, which selects the pin
profile at runtime. A new AtomS3R needs `--first-flash` once (full image; erases
M5's stock firmware), then copy its `config.json` over with `tools/mpr.py`.
Every tool finds the port through `tools/port.py` (`HELIOSTAT_PORT` /
`HELIOSTAT_BOARD` override).

The AtomS3R's console is the chip's USB-Serial-JTAG, which only takes input
while something reads stdin: `app.drain_console` and `main.py`'s grace window do
that, so `tools/mpr.py` can always break in.

**Updates over WiFi (AtomS3R):** `python3 tools/deploy.py --ota`. After a one-time
`--first-flash` over USB, firmware updates need no cable. A bad update rolls
itself back. The full procedure, including doing it by hand with `curl` and
recovering from problems, is in **[docs/ota.md](docs/ota.md)**.

`tools/calibrate_azimuth.py` (AtomS3R only) finds true north for the yaw axis
with the magnetometer: tape the Atom flat on the mirror, run it, and it sweeps
the yaw and the tilt and fits both (`tools/azimuth_fit.py`).

## Deploying: the firmware is frozen into MicroPython

`tools/deploy.py` builds MicroPython v1.24.1 (ESP-IDF v5.2.2, both in `~/esp`)
with `firmware/` compiled into flash via `tools/manifest.py`, flashes only the
application partition, and leaves `main.py`, `boot.py` and `config.json` on the
board's filesystem (`config.json` is backed up to `build/` first). A rebuild
after a change takes about a minute.

Why, on the devkit: this ESP32 has no PSRAM. Loaded from the filesystem -- even as `.mpy` --
the firmware's code occupied ~88 KB of the Python heap. Under steady HTTP polling
an allocation then failed, and MicroPython's split heap grew by taking the
ESP-IDF heap's largest free block, all of it (the ESP32 port keeps no reserve).
WiFi then could not allocate buffers: the board stopped answering even ping, and
asyncio died with `OSError: EIO`. Frozen, the Python heap has ~100 KB free and
the IDF heap ~90 KB, and neither moves under load. `/api/status` reports both
(`mem_free`, `idf_heap`), and the log prints them every minute.

`python3 tools/deploy.py --mpy` copies `.mpy` files instead, for quick
iteration without a build. They shadow the frozen modules (the filesystem is
first on `sys.path`) and bring the RAM cost back, so finish with a plain deploy.
Never copy loose `.py` modules onto the board for the same reason -- and note
that even an EMPTY package directory there (`util/`) shadows the frozen package.

`tools/check_board.py` reports the board's float precision and epoch. On this
board: **single-precision floats** and a **2000-01-01 epoch**, which is why
`solar/julian.py` never materialises a full Julian Day as a float.

## Talking to the board: use `tools/mpr.py`, not bare `mpremote`

```bash
python3 tools/mpr.py exec "import gc; print(gc.mem_free())"
python3 tools/mpr.py fs ls
python3 tools/mpr.py --run-app      # hard-reset and let the app start
```

Bare `mpremote` soft-resets the interpreter on every connection, and on the
ESP32 a soft reset after Bluetooth has been active can hang the chip until the
watchdog fires; then only the reset button gets you back in. `tools/mpr.py`
hard-resets the board over the USB-serial lines instead, catches `main.py`'s
1-second grace window with Ctrl-C (so the app and Bluetooth never start), and
runs `mpremote ... resume`, which skips the soft reset.

## Servo tuning and the settle trim

Both ST3020s run at the **factory settings: P 32, D 32, I 0, dead zone 1 step**.
`GET /api/servo?axis=az|el` shows a servo's registers, and `POST /api/servo`
changes the gains and dead zones (idle or manual only; see `docs/protocol.md`).

Measured on the mounted frame (2026-10-05):

- At P 32 the azimuth servo does not move until its command is ~4 steps
  (0.35 deg) ahead, so tracking advances in ~0.3 deg jumps every couple of
  minutes. The elevation servo, holding the mirror's weight, sat ~0.9 deg low,
  which put the beam 1.8 deg low.
- Raising P removes the jumps (P 64 moves on 1 step), but **P 64 starts to hunt and
  P 96 shook the wooden frame nearly over**. Keep the factory gains on this frame,
  and change gains only in small steps with someone watching it.

The firmware compensates in software instead: once an axis has settled more than
3 steps short of its target, it adds 70% of the error to the command (the
"trim", capped at 3 deg, shown per axis as `trim_deg` in `/api/status`). On the
frame it learned ~1 deg on the elevation axis by itself. See `hal/axis_base.py`.

## Recovering a bricked board

Hold the board's button while resetting (BOOT on the devkit, the screen on the
AtomS3R). `firmware/main.py` skips the application and
drops to the REPL.

## Safety

This machine concentrates sunlight. Use a **flat first-surface mirror, never a
concave one**. Configure keep-out zones before first light. Nobody stands in the
beam path during calibration. See the safety section of `docs/calibration.md`.

## Hardware

| | |
|---|---|
| Controller | ESP32 devkit or M5Stack AtomS3R, MicroPython 1.24.1 |
| Yaw (azimuth) | Waveshare ST3020, id 2, 0–4095 over 360°, absolute encoder |
| Tilt (elevation) | Waveshare ST3020, id 1 |
| Bus | 1 Mbps half-duplex through a Waveshare bus-servo adapter: devkit UART2 TX 17 / RX 16, AtomS3R UART1 on its Grove socket, TX G1 (white) / RX G2 (yellow) |
| Sensors | devkit: MPU6050 (0x68) on 3V3, never 5 V; AtomS3R: onboard BMI270 + BMM150. BMP180 (0x77) optional on either |
