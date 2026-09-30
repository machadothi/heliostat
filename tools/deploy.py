#!/usr/bin/env python3
"""Deploy the firmware to the board.

    python3 tools/deploy.py            # build MicroPython with the firmware frozen in, flash it
    python3 tools/deploy.py --mpy      # quick: copy precompiled .mpy files instead
    python3 tools/deploy.py --dry-run  # build/compile only

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

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "firmware"
OUT = REPO / "build" / "mpy"
ESP = Path(os.environ.get("ESP_DIR", Path.home() / "esp"))
IDF = ESP / "esp-idf"
MICROPYTHON = ESP / "micropython"
FW_BUILD = REPO / "build" / "esp32"
PORT = "/dev/ttyUSB0"
MPR = [sys.executable, str(REPO / "tools" / "mpr.py")]
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


def deploy_mpy():
    roots = [p.name for p in sorted(OUT.iterdir()) if p.is_file()]
    args = MPR + ["exec", CLEAN.format(packages=PACKAGES),
                  "+", "fs", "cp", "-r", *PACKAGES, ":",
                  "+", "fs", "cp", *roots, ":"]
    result = subprocess.run(args, cwd=OUT)
    if result.returncode != 0:
        sys.exit("deploy failed")
    print("deployed; the board has restarted into the app")


def build_firmware():
    if not (IDF / "export.sh").exists() or not (MICROPYTHON / "ports" / "esp32").exists():
        sys.exit(f"need ESP-IDF v5.2.2 in {IDF} and MicroPython v1.24.1 in {MICROPYTHON}")
    port = MICROPYTHON / "ports" / "esp32"
    script = (
        f"source {IDF}/export.sh >/dev/null"
        f" && make -s -C {MICROPYTHON}/mpy-cross -j{os.cpu_count()}"
        f" && make -C {port} BOARD=ESP32_GENERIC BUILD={FW_BUILD}"
        f" FROZEN_MANIFEST={REPO}/tools/manifest.py -j{os.cpu_count()}"
    )
    result = subprocess.run(["bash", "-c", script], stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit("firmware build failed (rerun without output suppressed to see why)")
    size = (FW_BUILD / "micropython.bin").stat().st_size
    print(f"built firmware with the heliostat frozen in ({size // 1024} KB)")


def deploy_frozen():
    backup = REPO / "build" / "config.backup.json"
    if subprocess.run(MPR + ["--stay", "fs", "cp", ":config.json", str(backup)]).returncode == 0:
        print(f"config.json backed up to {backup.relative_to(REPO)}")
    # Only the application partition: the bootloader and partition table are
    # stock, and the filesystem (config.json) lives in its own partition.
    flash = [sys.executable, "-m", "esptool", "--chip", "esp32", "-p", PORT, "-b", "460800",
             "write_flash", "0x10000", str(FW_BUILD / "micropython.bin")]
    if subprocess.run(flash, stdout=subprocess.DEVNULL).returncode != 0:
        sys.exit("flashing failed")
    print("flashed")
    args = MPR + ["exec", CLEAN.format(packages=PACKAGES),
                  "+", "fs", "cp", str(SRC / "main.py"), str(SRC / "boot.py"), ":"]
    if subprocess.run(args).returncode != 0:
        sys.exit("cleaning the board failed")
    print("deployed; the board has restarted into the app")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if "--mpy" in sys.argv:
        compile_all()
        if not dry:
            deploy_mpy()
    else:
        build_firmware()
        if not dry:
            deploy_frozen()
