#!/usr/bin/env python3
"""Deploy the firmware to the board.

    python3 tools/deploy.py                    # build MicroPython with the firmware frozen in, flash it
    python3 tools/deploy.py --board atoms3r    # pick the board (default: the one plugged in)
    python3 tools/deploy.py --first-flash      # full image: once per new board; erases its files
                                               # (config.json is backed up and put back)
    python3 tools/deploy.py --ota              # build, then update over WiFi (AtomS3R)
    python3 tools/deploy.py --ota --host 192.168.50.226
    python3 tools/deploy.py --mpy              # quick: copy precompiled .mpy files instead
    python3 tools/deploy.py --dry-run          # build/compile only

Boards are described in boards/<name>.json (see boards/README.md): the
MicroPython board to build, the chip, the USB device to look for. The build
freezes a one-line `board_id` module naming the board, which selects its pin
profile in firmware/hal/profiles.py.

Why frozen (the default): this ESP32 has no PSRAM, and the Python heap shares
RAM with the WiFi driver. Loaded from the filesystem, even as .mpy, the firmware's
code occupies ~88 KB of heap. Under HTTP load an allocation then fails, and
MicroPython's split heap grows by taking the ESP-IDF heap's LARGEST free block
-- all of it, with no reserve -- after which WiFi cannot allocate buffers, the
board stops answering even ping, and asyncio dies with EIO. Frozen bytecode runs
from flash and costs almost no RAM.

Why .mpy (--mpy): no firmware build, for quick iteration. It shadows the frozen
modules (the filesystem is first on sys.path) and brings the RAM cost back, so
finish with a plain deploy.

Both keep main.py and boot.py as source on the board (MicroPython runs those by
name), and never touch config.json -- WiFi credentials, site, calibration. A
frozen deploy backs it up to build/config.backup.json before flashing anyway.

Needs: ESP-IDF v5.2.2 in ~/esp/esp-idf and MicroPython v1.24.1 in
~/esp/micropython (frozen); mpy-cross==1.24.1.post3 from pip (--mpy).
"""

import datetime
import hashlib
import http.client
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "firmware"
OUT = REPO / "build" / "mpy"
ESP = Path(os.environ.get("ESP_DIR", Path.home() / "esp"))
IDF = ESP / "esp-idf"
MICROPYTHON = ESP / "micropython"
sys.path.insert(0, str(REPO / "tools"))
import discovery  # noqa: E402
from port import PortError, boards, resolve  # noqa: E402
PACKAGES = ("hal", "solar", "control", "net", "ble", "util")
KEEP_AS_SOURCE = {"main.py", "boot.py"}

# Remove old modules (.py or .mpy) so a stale .py can never shadow new bytecode.
# The package directories go too: an empty `util/` on the filesystem is still a
# namespace package, found before the frozen one, and `util.jsonio` then fails
# to import. config.json and anything else not listed is left alone. (MicroPython's
# str.endswith() does not accept a tuple, hence the helper.)
CLEAN = (
    "import os\n"
    "code = lambda f: f.endswith('.py') or f.endswith('.mpy')\n"
    "for d in {packages!r}:\n"
    "    try:\n"
    "        for f in os.listdir(d):\n"
    "            if code(f): os.remove(d + '/' + f)\n"
    "        os.rmdir(d)\n"
    "    except OSError: pass\n"
    "for f in os.listdir():\n"
    "    if code(f) and f not in ('main.py', 'boot.py'): os.remove(f)\n"
    "print('board cleaned')\n"
)


def mpy_cross():
    exe = shutil.which("mpy-cross") or str(Path.home() / ".local" / "bin" / "mpy-cross")
    if not Path(exe).exists():
        sys.exit("mpy-cross not found: pip install mpy-cross==1.24.1.post3")
    return exe


def compile_all():
    if OUT.exists():
        shutil.rmtree(OUT)
    exe = mpy_cross()
    count = 0
    for src in sorted(SRC.rglob("*.py")):
        rel = src.relative_to(SRC)
        if rel.name in KEEP_AS_SOURCE and len(rel.parts) == 1:
            continue
        dst = OUT / rel.with_suffix(".mpy")
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([exe, "-o", str(dst), str(src)], check=True)
        count += 1
    for name in KEEP_AS_SOURCE:
        shutil.copy(SRC / name, OUT / name)
    size = sum(f.stat().st_size for f in OUT.rglob("*.mpy"))
    print(f"compiled {count} modules to .mpy ({size // 1024} KB)")


def mpr(port):
    return [sys.executable, str(REPO / "tools" / "mpr.py"), "--port", port]


def deploy_mpy(port):
    roots = [p.name for p in sorted(OUT.iterdir()) if p.is_file()]
    args = mpr(port) + ["exec", CLEAN.format(packages=PACKAGES),
                  "+", "fs", "cp", "-r", *PACKAGES, ":",
                  "+", "fs", "cp", *roots, ":"]
    result = subprocess.run(args, cwd=OUT)
    if result.returncode != 0:
        sys.exit("deploy failed")
    print("deployed; the board has restarted into the app")


def build_dir(board):
    return REPO / "build" / board


def build_firmware(board, cfg):
    if not (IDF / "export.sh").exists() or not (MICROPYTHON / "ports" / "esp32").exists():
        sys.exit(f"need ESP-IDF v5.2.2 in {IDF} and MicroPython v1.24.1 in {MICROPYTHON}: "
                 "run tools/setup_env.sh")
    out = build_dir(board)
    out.mkdir(parents=True, exist_ok=True)
    # The board's identity and the build time, frozen in: the profile selects
    # the pins at runtime; BUILD lets --ota confirm the new image is running.
    build = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (out / "board_id.py").write_text(f'BOARD = "{cfg["profile"]}"\nBUILD = "{build}"\n')
    (out / "manifest.py").write_text(
        f'include("{REPO / "tools" / "manifest.py"}")\n'
        f'module("board_id.py", base_path="{out}")\n'
    )
    if "micropython_board_dir" in cfg:
        target = f"BOARD_DIR={REPO / 'boards' / cfg['micropython_board_dir']}"
    else:
        target = f"BOARD={cfg['micropython_board']}"
    port_dir = MICROPYTHON / "ports" / "esp32"
    script = (
        f"source {IDF}/export.sh >/dev/null"
        f" && make -s -C {MICROPYTHON}/mpy-cross -j{os.cpu_count()}"
        f" && make -C {port_dir} {target} BUILD={out}"
        f" FROZEN_MANIFEST={out / 'manifest.py'} -j{os.cpu_count()}"
    )
    result = subprocess.run(["bash", "-c", script], stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit("firmware build failed (rerun without output suppressed to see why)")
    size = (out / "micropython.bin").stat().st_size
    print(f"built {board} firmware {build} with the heliostat frozen in ({size // 1024} KB)")
    return build


def ota_token(board):
    """This board's OTA token, kept in build/ (gitignored); created on first use."""
    path = REPO / "build" / f"ota_token.{board}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(16))
        path.chmod(0o600)
    return path.read_text().strip()


# Merge the OTA token into the board's config.json (creating it if absent).
SET_TOKEN = (
    "import json\n"
    "try:\n"
    "    c = json.load(open('config.json'))\n"
    "except OSError:\n"
    "    c = {{}}\n"
    "c.setdefault('ota', {{}})['token'] = {token!r}\n"
    "f = open('config.json', 'w'); json.dump(c, f); f.close()\n"
    "print('OTA token set')\n"
)


def deploy_frozen(board, cfg, port, first_flash=False):
    out = build_dir(board)
    backup = REPO / "build" / f"config.backup.{board}.json"
    have_backup = subprocess.run(
        mpr(port) + ["--stay", "fs", "cp", ":config.json", str(backup)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if have_backup:
        print(f"config.json backed up to {backup.relative_to(REPO)}")
    if first_flash:
        # Bootloader + partition table + app: replaces whatever the board shipped
        # with, or changes its partition table. Erasing also wipes its files.
        image, offset = out / "firmware.bin", cfg["full_image_offset"]
        erase = [sys.executable, "-m", "esptool", "--chip", cfg["chip"], "-p", port, "erase_flash"]
        if subprocess.run(erase, stdout=subprocess.DEVNULL).returncode != 0:
            sys.exit("erasing failed")
    else:
        # Only the application partition: the filesystem (config.json) is untouched.
        image, offset = out / "micropython.bin", cfg["app_offset"]
    esptool = [sys.executable, "-m", "esptool", "--chip", cfg["chip"], "-p", port, "-b", "460800"]
    if not first_flash and cfg.get("otadata"):
        # An OTA board may be booting its OTHER slot (after an update over WiFi);
        # this writes the first. Clearing the slot selection makes the bootloader
        # boot the first slot -- the image just written. Found the hard way: the
        # board kept running the old image from the second slot.
        start, size = cfg["otadata"]
        if subprocess.run(esptool + ["erase_region", start, size], stdout=subprocess.DEVNULL).returncode != 0:
            sys.exit("clearing the OTA slot selection failed")
    flash = esptool + ["write_flash", "-z", offset, str(image)]
    if subprocess.run(flash, stdout=subprocess.DEVNULL).returncode != 0:
        sys.exit("flashing failed")
    print(f"flashed {board} on {port}")
    args = mpr(port) + ["exec", CLEAN.format(packages=PACKAGES),
                        "+", "fs", "cp", str(SRC / "main.py"), str(SRC / "boot.py"), ":"]
    if first_flash and have_backup:
        args += ["+", "fs", "cp", str(backup), ":config.json"]
    if cfg.get("ota"):
        args += ["+", "exec", SET_TOKEN.format(token=ota_token(board))]
    if subprocess.run(args).returncode != 0:
        sys.exit("cleaning the board failed")
    if first_flash and have_backup:
        print("config.json restored")
    print("deployed; the board has restarted into the app")


def find_host(board):
    """The address of the one heliostat on the network that runs this board."""
    found = discovery.find()
    matches = [h for h in found if h.get("board") == board]
    if len(matches) == 1:
        return matches[0]["host"]
    if not matches and len(found) == 1 and "board" not in found[0]:
        return found[0]["host"]  # firmware from before the board field
    listed = ", ".join(f"{h['host']} ({h.get('board', '?')})" for h in found) or "none"
    sys.exit(f"cannot tell which heliostat to update (found: {listed}); pass --host")


def deploy_ota(board, cfg, build, host):
    if not cfg.get("ota"):
        sys.exit(f"{board} has no OTA slots: deploy over USB")
    image = (build_dir(board) / "micropython.bin").read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    host = host or find_host(board)
    try:
        previous = json.load(urllib.request.urlopen(f"http://{host}/api/status", timeout=6))["device"].get("build")
    except Exception:  # noqa: BLE001
        previous = None
    print(f"sending {len(image) // 1024} KB to {host} (it runs {previous}) ...")
    conn = http.client.HTTPConnection(host, 80, timeout=30)
    conn.putrequest("POST", "/api/ota")
    conn.putheader("Content-Length", str(len(image)))
    conn.putheader("X-OTA-Token", ota_token(board))
    conn.putheader("X-SHA256", digest)
    conn.endheaders()
    sent, t0 = 0, time.monotonic()
    for i in range(0, len(image), 16384):
        conn.send(image[i:i + 16384])
        sent = min(len(image), i + 16384)
        print(f"\r  {100 * sent // len(image):3d}%", end="", flush=True)
    reply = conn.getresponse()
    body = json.loads(reply.read() or b"{}")
    print(f"\r  sent in {time.monotonic() - t0:.0f} s")
    if reply.status != 200:
        sys.exit(f"the board refused the update ({reply.status}): {body.get('error')}")

    print("restarting into the new firmware ...")
    deadline = time.monotonic() + 120
    verified = False
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            device = json.load(urllib.request.urlopen(f"http://{host}/api/status", timeout=4))["device"]
        except Exception:  # noqa: BLE001 - rebooting
            continue
        if previous and device.get("build") == previous and time.monotonic() > deadline - 90:
            sys.exit(f"the new firmware failed to start and the board ROLLED BACK to {previous} "
                     "by itself; it is running and healthy. Check the build (or deploy over USB "
                     "and read the console to see why).")
        if device.get("build") != build:
            continue
        if not device.get("ota_pending"):
            verified = True
            break
    if not verified:
        sys.exit("the new firmware did not confirm itself healthy within 2 minutes; "
                 "if it stays unhealthy a reset rolls it back")
    print(f"updated: {board} runs build {build}, verified (rollback cancelled)")


def main(argv):
    dry = "--dry-run" in argv
    board = None
    if "--board" in argv:
        board = argv[argv.index("--board") + 1]
    known = boards()
    if "--ota" in argv:
        board = board or ("atoms3r" if sum(1 for c in known.values() if c.get("ota")) == 1 else None)
        if board not in known:
            sys.exit("name the board: --board <name>")
        host = argv[argv.index("--host") + 1] if "--host" in argv else None
        build = build_firmware(board, known[board])
        if not dry:
            deploy_ota(board, known[board], build, host)
        return
    if dry and board:
        port = None
    else:
        try:
            board, port = resolve(board)
        except PortError as exc:
            if not (dry and board is None):
                sys.exit(str(exc))
            sys.exit(f"{exc}; for a dry run name the board: --board {'|'.join(known)}")
    if "--mpy" in argv:
        compile_all()
        if not dry:
            deploy_mpy(port)
        return
    build_firmware(board, known[board])
    if not dry:
        deploy_frozen(board, known[board], port, first_flash="--first-flash" in argv)


if __name__ == "__main__":
    main(sys.argv[1:])
