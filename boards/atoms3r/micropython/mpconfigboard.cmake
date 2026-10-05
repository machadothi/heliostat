# MicroPython board definition for the heliostat on an M5Stack AtomS3R
# (ESP32-S3-PICO-1-N8R8: 8 MB flash, 8 MB octal PSRAM). Built out-of-tree with
# `make BOARD_DIR=...` by tools/deploy.py --board atoms3r.
#
# Differs from ESP32_GENERIC_S3/SPIRAM_OCT in two ways:
#  * no TinyUSB (no boards/sdkconfig.usb), so the REPL is on the chip's
#    USB-Serial-JTAG -- keeps the DTR/RTS hard reset tools/mpr.py and esptool
#    rely on working;
#  * two OTA application slots (partitions-8MiB-ota.csv), for updates over WiFi.
set(IDF_TARGET esp32s3)

# Our own partition table (two OTA slots). sdkconfig defaults cannot refer to
# this directory, so the setting is written into the build directory here.
set(HELIOSTAT_PARTITIONS_SDKCONFIG ${CMAKE_BINARY_DIR}/sdkconfig.partitions)
file(WRITE ${HELIOSTAT_PARTITIONS_SDKCONFIG}
    "CONFIG_PARTITION_TABLE_CUSTOM=y\n"
    "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME=\"${MICROPY_BOARD_DIR}/partitions-8MiB-ota.csv\"\n")

set(SDKCONFIG_DEFAULTS
    boards/sdkconfig.base
    ${SDKCONFIG_IDF_VERSION_SPECIFIC}
    boards/sdkconfig.ble
    boards/sdkconfig.spiram_sx
    boards/sdkconfig.240mhz
    boards/sdkconfig.spiram_oct
    ${MICROPY_BOARD_DIR}/sdkconfig.board
    ${HELIOSTAT_PARTITIONS_SDKCONFIG}
)
