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
./tools/deploy.sh
```

`tools/check_board.py` reports the board's float precision and epoch. Both
constrain how the solar math must be written — see `docs/wiring.md`.

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
