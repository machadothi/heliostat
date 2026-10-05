# Firmware updates over WiFi (OTA)

How to update the heliostat's firmware without a USB cable, by hand or with the
script, how it protects itself from a bad update, and how to recover when
something goes wrong. Everything here works from a plain shell. No special
tools beyond what this repo already needs.

**Boards:** OTA works on the **M5Stack AtomS3R** only. The ESP32 devkit's 4 MB
flash cannot hold two copies of the firmware, so it is updated over USB
(`python3 tools/deploy.py`).

## Contents

1. [How it works](#how-it-works)
2. [One-time setup (USB)](#one-time-setup-usb)
3. [Update with the script](#update-with-the-script)
4. [Update by hand (curl)](#update-by-hand-curl)
5. [What the board checks](#what-the-board-checks)
6. [Rollback: what happens when an update is bad](#rollback-what-happens-when-an-update-is-bad)
7. [Recovery](#recovery)
8. [What OTA does not update](#what-ota-does-not-update)
9. [Reference](#reference)

## How it works

The AtomS3R's flash has **two application slots** (`ota_0` and `ota_1`) next to
the file system that holds `config.json`. The firmware runs from one slot. An
update:

1. arrives over HTTP (`POST /api/ota`) and is written into the **other** slot,
   4 KB at a time, while its SHA-256 is computed;
2. is accepted only if the SHA-256 matches the one sent with it. If it does not
   match, nothing changes;
3. is marked as the slot to boot, a file `ota_pending` is written, and the
   board restarts;
4. boots **on probation**. Once it has been healthy for 20 seconds (WiFi joined
   and the control loop cycling without a software fault), it confirms itself,
   cancels the rollback and deletes `ota_pending`;
5. if instead it crashes, cannot start, or is reset before confirming, the
   bootloader goes back to the previous firmware by itself.

`config.json` (WiFi, site, calibration) lives on the file system and is never
touched by an update.

## One-time setup (USB)

Needed once per board, and again only if the partition table changes.

```bash
python3 tools/deploy.py --board atoms3r --first-flash
```

This:

- builds the firmware (ESP-IDF v5.2.2 and MicroPython v1.24.1 in `~/esp`; see
  the main README);
- backs up the board's `config.json` to `build/config.backup.atoms3r.json`;
- **erases the whole flash** and writes the bootloader, the two-slot partition
  table (`boards/atoms3r/micropython/partitions-8MiB-ota.csv`) and the
  firmware;
- puts `config.json` back, and stores an **OTA token** in it.

The token is a random password that every update must carry. A copy is kept on
the laptop in `build/ota_token.atoms3r` (git-ignored, readable only by you).
**Keep that file**: without it you cannot update over WiFi (see
[Recovery](#recovery) to set a new one).

## Update with the script

```bash
python3 tools/deploy.py --ota                        # finds the heliostat on the network
python3 tools/deploy.py --ota --host 192.168.50.226  # or say where it is
```

It builds the firmware, sends it (about 30 s), waits for the board to restart
and confirm itself, and reports one of:

- `updated: atoms3r runs build <time>, verified (rollback cancelled)`: done;
- `the board refused the update (409): update in idle (now track)`: put it in
  idle first (app → Idle, or the curl line below), then run again;
- `... ROLLED BACK to <previous build> by itself`: the new firmware failed
  to start; the board is running the old one and is fine. See
  [Rollback](#rollback-what-happens-when-an-update-is-bad).

## Update by hand (curl)

Everything the script does, step by step. Replace `192.168.50.226` with your
heliostat's address (shown on its screen's WIFI card, in the app, or found with
`python3 tools/discovery.py`).

**1. Build the image** (on the laptop, in the repo):

```bash
python3 tools/deploy.py --board atoms3r --dry-run
```

The image is `build/atoms3r/micropython.bin`. The build prints its time, e.g.
`built atoms3r firmware 2026-10-05T13:52:43Z ...`. That time is the build ID
you will check for at the end.

<details><summary>Building without the script</summary>

```bash
source ~/esp/esp-idf/export.sh
make -C ~/esp/micropython/mpy-cross
printf 'BOARD = "atoms3r"\nBUILD = "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > build/atoms3r/board_id.py
printf 'include("%s/tools/manifest.py")\nmodule("board_id.py", base_path="%s/build/atoms3r")\n' "$PWD" "$PWD" > build/atoms3r/manifest.py
make -C ~/esp/micropython/ports/esp32 BOARD_DIR=$PWD/boards/atoms3r/micropython \
     BUILD=$PWD/build/atoms3r FROZEN_MANIFEST=$PWD/build/atoms3r/manifest.py
```

</details>

**2. Put the heliostat in idle.** Updates are refused while it is tracking,
stowing or homing:

```bash
curl -X POST -H 'Content-Type: application/json' -d '{"mode":"idle"}' http://192.168.50.226/api/mode
```

**3. Send it:**

```bash
IMG=build/atoms3r/micropython.bin
curl -X POST --data-binary @$IMG \
     -H "Content-Type: application/octet-stream" -H "Expect:" \
     -H "X-OTA-Token: $(cat build/ota_token.atoms3r)" \
     -H "X-SHA256: $(sha256sum $IMG | cut -d' ' -f1)" \
     http://192.168.50.226/api/ota
```

It answers `{"ok": true, "bytes": ..., "rebooting": true}` after about 30 s,
then restarts. (`-H "Expect:"` stops curl from waiting for a reply the board
does not send before the upload. Leaving it out costs a second, not an error.)

**4. Check it took**, after about 30 seconds:

```bash
curl -s http://192.168.50.226/api/status | python3 -m json.tool | grep -E '"build"|"ota_pending"'
```

| You see | Meaning |
|---|---|
| your new build time, `"ota_pending": false` | done: running and confirmed |
| your new build time, `"ota_pending": true` | still on probation (first 20 s); check again |
| the **old** build time, `"ota_pending": false` | the new firmware failed and the board rolled back by itself |
| no answer for more than 2 minutes | see [Recovery](#recovery) |

## What the board checks

| Answer | Why | What to do |
|---|---|---|
| `403` (curl may also print "Connection reset") | wrong or missing `X-OTA-Token` | use `build/ota_token.atoms3r`. The board stops reading, so curl sees the connection close |
| `409 OTA is not set up on this board` | no token in its config | do the [one-time setup](#one-time-setup-usb), or set a token ([Recovery](#recovery)) |
| `409 update in idle (now track)` | it is moving the mirror | put it in idle (step 2) |
| `409 an update is already in progress` | two uploads at once | wait for the first |
| `400 X-SHA256 header missing` | header absent or not 64 hex digits | check step 3 |
| `400 not an ESP32 application image` | wrong file (e.g. `firmware.bin`) | send `micropython.bin` |
| `400 SHA-256 mismatch` | the image changed on the way | send again. Nothing was changed on the board |
| `413` | image bigger than the 2.5 MB slot | the firmware has outgrown the slot |

## Rollback: what happens when an update is bad

The board **always** goes back to the previous firmware if the new one does not
confirm itself. In detail:

- **It crashes at start-up** (an exception in the app, or even before it):
  `main.py` / `startup.py` see the `ota_pending` file and reset. The bootloader
  sees an unconfirmed image and boots the previous one.
- **It was built for the wrong board** (the devkit build sent to the Atom):
  start-up refuses to run it and resets, as above.
- **It starts but is unhealthy** (no WiFi, the control loop failing): it never
  confirms. It stays up so you can look at it, and **the next reset or power
  cycle rolls it back**.

To see why it failed, connect USB and look at the console. `tools/mpr.py`
gives a REPL, and the log is printed at boot.

Tested on the real board on 2026-10-05: a deliberately wrong build was sent; it
refused to start, the board reset and was back on the previous firmware,
healthy, about 40 s later, untouched by hand.

## Recovery

**The board does not answer after an update.** Power-cycle it. An unconfirmed
firmware is rolled back at the next start.

**Lost the token** (`build/ota_token.atoms3r` gone, or a different laptop).
Connect USB and run a normal deploy. It stores this laptop's token on the
board:

```bash
python3 tools/deploy.py --board atoms3r
```

**Anything worse, or the board seems stuck on old firmware after a USB
deploy.** A USB deploy always works and always boots what it wrote:

```bash
python3 tools/deploy.py --board atoms3r            # app only; config kept
python3 tools/deploy.py --board atoms3r --first-flash   # everything; config backed up and restored
```

(A USB deploy writes the first slot and clears the slot selection, so the board
boots what was just written even if it had been running the second slot.)

**Safe mode.** Hold the screen (the button) while pressing reset: the app does
not start and you get the REPL over USB.

## What OTA does not update

- **`config.json`**: kept across updates on purpose. Change settings with
  `POST /api/config` or the app.
- **`main.py` and `boot.py`** on the board's file system. They are deliberately
  tiny stubs: the real start-up logic is `firmware/startup.py`, which is inside
  the image and so does get updated. If you ever change `firmware/main.py` or
  `firmware/boot.py`, deploy once over USB.
- **The bootloader and the partition table**: only `--first-flash` writes those.

## Reference

| Item | Where |
|---|---|
| Board side | `firmware/net/ota.py` (upload, checks, `mark_valid`), `firmware/app.py` `confirm_firmware`, `firmware/startup.py` and `firmware/main.py` (rollback on failure) |
| Laptop side | `tools/deploy.py` (`--ota`, `--first-flash`), `tools/discovery.py` |
| Partition table | `boards/atoms3r/micropython/partitions-8MiB-ota.csv`: `ota_0` and `ota_1` 2.5 MB each at 0x10000 and 0x290000, slot selection (`otadata`) at 0xd000, files from 0x510000 |
| Token | board: `config.json` → `ota.token` (shown as `***` by `GET /api/config`); laptop: `build/ota_token.atoms3r` |
| Status | `GET /api/status` → `device.build` (build time of the running firmware), `device.ota_pending` |
| HTTP | `POST /api/ota`: body is the image; headers `Content-Length`, `X-OTA-Token`, `X-SHA256` (see `docs/protocol.md`) |
