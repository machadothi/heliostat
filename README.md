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

## Deploying: the firmware is frozen into MicroPython

`tools/deploy.py` builds MicroPython v1.24.1 (ESP-IDF v5.2.2, both in `~/esp`)
with `firmware/` compiled into flash via `tools/manifest.py`, flashes only the
application partition, and leaves `main.py`, `boot.py` and `config.json` on the
board's filesystem (`config.json` is backed up to `build/` first). A rebuild
after a change takes about a minute.

Why: this ESP32 has no PSRAM. Loaded from the filesystem -- even as `.mpy` --
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

## Recovering a bricked board

Hold **BOOT** while resetting. `firmware/main.py` skips the application and
drops to the REPL.

## Safety

This machine concentrates sunlight. Use a **flat first-surface mirror, never a
concave one**. Configure keep-out zones before first light. Nobody stands in the
beam path during calibration. See the safety section of `docs/calibration.md`.

## Hardware

| | |
|---|---|
| Controller | ESP32, MicroPython 1.24.1 |
| Azimuth | Waveshare ST3020, id 1, 0–4095 over 360°, absolute encoder |
| Elevation | Waveshare SC09, id 2, 0–1023 over 300° |
| Bus | UART2 (GPIO17 TX / GPIO16 RX) at 1 Mbps through a half-duplex adapter |
| Sensors | MPU6050 (0x68) tilt, BMP180 (0x77) pressure — both on 3V3, never 5 V |
