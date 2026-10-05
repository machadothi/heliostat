# Freeze manifest for a custom MicroPython build: the stock ESP32 modules plus
# the heliostat firmware, compiled into flash. Used by tools/deploy.py.
#
# Frozen bytecode runs straight from flash. Loaded from the filesystem -- even
# as .mpy -- the same code costs ~88 KB of RAM, and on this board (no PSRAM)
# that pushed the Python heap into growing over the WiFi driver's memory.
#
# main.py and boot.py stay on the filesystem. Note that the filesystem comes
# BEFORE .frozen on sys.path, so a stray module file on the board shadows the
# frozen one -- deploy.py removes them.

include("$(PORT_DIR)/boards/manifest.py")

FIRMWARE = "../firmware"

for name in ("hal", "solar", "control", "net", "ble", "util"):
    package(name, base_path=FIRMWARE)

# startup.py is the boot logic main.py imports: frozen, so updates over WiFi
# bring it too.
for name in ("app", "config", "version", "startup"):
    module(name + ".py", base_path=FIRMWARE)
