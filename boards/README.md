# Boards

One firmware tree, several controller boards. Each `<name>.json` here tells the
tools how to build for and talk to one board; `firmware/hal/profiles.py` holds
the same board's pin map for the firmware.

| Board | File | MicroPython build |
|---|---|---|
| ESP32 devkit (no PSRAM) | `esp32_devkit.json` | stock `ESP32_GENERIC` |
| M5Stack AtomS3R | `atoms3r.json` | `atoms3r/micropython/` (our own board definition) |

Fields:

- `profile`: the key in `firmware/hal/profiles.py`. `tools/deploy.py` freezes it
  into the build as `board_id.BOARD`, so the firmware knows its pins without
  guessing.
- `micropython_board` or `micropython_board_dir`: a stock MicroPython board name,
  or a board definition kept here (relative to this directory).
- `chip`: for esptool.
- `usb_match`: substrings of the `/dev/serial/by-id` name that identify the
  board's USB interface. `tools/port.py` uses them to find the port, and
  `tools/deploy.py` to pick the board when only one is plugged in.
- `app_offset`, `full_image_offset`: where the application and the full image
  (bootloader + partition table + app, `--first-flash`) go.
- `otadata`: [offset, size] of the OTA slot-selection partition (OTA boards only).
  A USB deploy clears it so the board boots the slot it just wrote.
- `ota`: whether the build has two application slots, so `tools/deploy.py --ota`
  can update it over WiFi. The devkit's 4 MB flash cannot hold two copies of
  its 1.7 MB firmware; the AtomS3R's 8 MB holds two 2.5 MB slots
  (`atoms3r/micropython/partitions-8MiB-ota.csv`).

The AtomS3R definition differs from `ESP32_GENERIC_S3/SPIRAM_OCT` in two ways: no
TinyUSB, so the REPL runs on the chip's USB-Serial-JTAG (which keeps the DTR/RTS
hard reset `tools/mpr.py` and esptool use working); and two OTA app slots
instead of one factory app (see `docs/ota.md`).

Adding a board: a JSON here, a profile in `firmware/hal/profiles.py`, and, if
MicroPython has no suitable stock board, a definition directory like
`atoms3r/micropython/`.
