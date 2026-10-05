#define MICROPY_HW_BOARD_NAME               "M5Stack AtomS3R"
#define MICROPY_HW_MCU_NAME                 "ESP32S3"

// REPL on USB-Serial-JTAG, not TinyUSB CDC: see mpconfigboard.cmake.
#define MICROPY_HW_ENABLE_USBDEV            (0)
#define MICROPY_HW_ENABLE_UART_REPL         (0)

#define MICROPY_PY_MACHINE_DAC              (0)

// Internal I2C: BMI270 (0x68, with the BMM150 on its aux bus) and the LP5562
// LED/backlight driver (0x30).
#define MICROPY_HW_I2C0_SCL                 (0)
#define MICROPY_HW_I2C0_SDA                 (45)
