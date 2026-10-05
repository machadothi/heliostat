"""The AtomS3R's 128x128 screen as the status light: same contract as the LED.

hal/heartbeat.py blinks GPIO2 on the devkit; this draws on the screen instead,
with the same rule -- the "alive" pulse moves only while the control loop is
completing cycles (`beat()`), so a frozen screen means a stopped tracker:

    green dot    healthy, cycling       amber SETUP card   Bluetooth setup is on
    red FAULT    latched fault/E-stop   STOPPED            control loop stalled

What it shows is laid out in hal/status_ui.py: one item at a time, large,
rotating every 2 s (preview it with tools/preview_display.py).

Panel and pins follow M5Stack's own M5GFX autodetect for the AtomS3R: 3-wire
SPI (SCLK 15, MOSI 21, CS 14, DC 42, RST 48); the controller is a GC9107 on
early units (memory 128x160, visible rows offset by 32) and an ST7735S since
2026-05 (offset 2/1, inverted, rotated 180), told apart by the RDDID register.
The backlight is the white channel of the LP5562 LED driver on the internal
I2C bus (0x30). Pixels are RGB565 sent big-endian, so colours are byte-swapped.
"""

import asyncio

from hal.heartbeat import FAULT, HEALTHY, PROVISIONING, STALL_AFTER_S, STALLED

LP5562 = 0x30


GC9107_INIT = (  # M5GFX Panel_GC9107, then COLMOD/MADCTL as Panel_GC9xxx sets them
    (0xFE, b"", 5), (0xEF, b"", 5),
    (0xB0, b"\xC0", 0), (0xB2, b"\x2F", 0), (0xB3, b"\x03", 0), (0xB6, b"\x19", 0),
    (0xB7, b"\x01", 0), (0xAC, b"\xCB", 0), (0xAB, b"\x0E", 0), (0xB4, b"\x04", 0),
    (0xA8, b"\x19", 0), (0xB8, b"\x08", 0), (0xE8, b"\x24", 0), (0xE9, b"\x48", 0),
    (0xEA, b"\x22", 0), (0xC6, b"\x30", 0), (0xC7, b"\x18", 0),
    (0xF0, bytes((0x01, 0x2B, 0x23, 0x3C, 0xB7, 0x12, 0x17, 0x60, 0x00, 0x06, 0x0C, 0x17, 0x12, 0x1F)), 0),
    (0xF1, bytes((0x05, 0x2E, 0x2D, 0x44, 0xD6, 0x15, 0x17, 0xA0, 0x02, 0x0D, 0x0D, 0x1A, 0x18, 0x1F)), 0),
    (0x3A, b"\x05", 0),  # 16 bits per pixel
    (0x36, b"\x08", 0),  # BGR order
    (0x11, b"", 120), (0x29, b"", 0),
)
ST7735S_INIT = (
    (0x01, b"", 150), (0x11, b"", 255),
    (0x3A, b"\x05", 0),
    (0x36, b"\xC8", 0),  # rotated 180 (MX|MY), BGR
    (0x21, b"", 0),  # inverted, per M5GFX
    (0x13, b"", 10), (0x29, b"", 0),
)
PANELS = {"gc9107": (GC9107_INIT, 0, 32), "st7735s": (ST7735S_INIT, 2, 1)}  # init, x0, y0


class Panel:
    W = H = 128

    def __init__(self, pins, i2c=None, sleep_ms=None):
        import time

        from machine import SPI, Pin

        self._sleep = sleep_ms or time.sleep_ms
        self.kind = read_panel_kind(pins)
        init, self.x0, self.y0 = PANELS[self.kind]
        self.cs = Pin(pins["cs"], Pin.OUT, value=1)
        self.dc = Pin(pins["dc"], Pin.OUT, value=1)
        self.spi = SPI(1, baudrate=20_000_000, sck=Pin(pins["sck"]), mosi=Pin(pins["mosi"]))
        for cmd, data, delay in init:
            self.command(cmd, data)
            if delay:
                self._sleep(delay)
        if i2c is not None:
            backlight(i2c, 160)

    def command(self, cmd, data=b""):
        self.cs.value(0)
        self.dc.value(0)
        self.spi.write(bytes((cmd,)))
        if data:
            self.dc.value(1)
            self.spi.write(data)
        self.cs.value(1)

    def show(self, buf):
        """Send a full 128x128 RGB565 frame."""
        x0, y0 = self.x0, self.y0
        self.command(0x2A, bytes((0, x0, 0, x0 + self.W - 1)))
        self.command(0x2B, bytes((0, y0, 0, y0 + self.H - 1)))
        self.command(0x2C, buf)


def read_panel_kind(pins):
    """'gc9107' or 'st7735s', from RDDID over the 3-wire bus (bit-banged).

    Also resets the panel. The ID arrives after one dummy bit; M5GFX compares
    the bytes little-endian: GC9107 0x079100, ST7735S 0x7683 or 0x897C.
    """
    import time

    from machine import Pin

    sck = Pin(pins["sck"], Pin.OUT, value=0)
    cs = Pin(pins["cs"], Pin.OUT, value=1)
    dc = Pin(pins["dc"], Pin.OUT, value=1)
    rst = Pin(pins["rst"], Pin.OUT, value=1)
    rst.value(0)
    time.sleep_ms(10)
    rst.value(1)
    time.sleep_ms(150)
    sda = Pin(pins["mosi"], Pin.OUT, value=0)
    cs.value(0)
    dc.value(0)
    for i in range(7, -1, -1):
        sda.value((0x04 >> i) & 1)
        sck.value(1)
        sck.value(0)
    dc.value(1)
    sda = Pin(pins["mosi"], Pin.IN)
    raw = 0
    for _ in range(33):  # 1 dummy bit + 32
        sck.value(1)
        raw = (raw << 1) | sda.value()
        sck.value(0)
    cs.value(1)
    return panel_kind_from_id(raw)


def panel_kind_from_id(raw33):
    """The 33 bits read after RDDID -> 'gc9107' or 'st7735s' (default)."""
    word = raw33 & 0xFFFFFFFF  # drop the dummy bit
    b = ((word >> 24) & 0xFF, (word >> 16) & 0xFF, (word >> 8) & 0xFF, word & 0xFF)
    little = b[0] | (b[1] << 8) | (b[2] << 16)
    if little == 0x079100:
        return "gc9107"
    return "st7735s"


def backlight(i2c, level):
    """LP5562 white channel = the LCD backlight (M5GFX Light_M5StackAtomS3R)."""
    import time

    i2c.writeto_mem(LP5562, 0x00, b"\x40")  # chip enable
    time.sleep_ms(1)
    i2c.writeto_mem(LP5562, 0x08, b"\x01")  # internal clock
    i2c.writeto_mem(LP5562, 0x70, b"\x00")  # every LED driven from its PWM register
    i2c.writeto_mem(LP5562, 0x0E, bytes((level,)))  # W PWM


class _Swapped:
    """framebuf with colours byte-swapped on the way in: the panel wants RGB565
    big-endian, framebuf stores it little-endian, and swapping each colour is
    far cheaper than swapping 32 KB of pixels per frame."""

    def __init__(self, fb):
        self.fb = fb

    @staticmethod
    def _c(c):
        return ((c & 0xFF) << 8) | (c >> 8)

    def fill(self, c):
        self.fb.fill(self._c(c))

    def fill_rect(self, x, y, w, h, c):
        self.fb.fill_rect(x, y, w, h, self._c(c))

    def text(self, s, x, y, c):
        self.fb.text(s, x, y, self._c(c))

    def ellipse(self, x, y, rx, ry, c, f=False):
        self.fb.ellipse(x, y, rx, ry, self._c(c), f)

    def pixel(self, x, y):
        return self.fb.pixel(x, y)


def _mono(w, h):
    import framebuf

    return framebuf.FrameBuffer(bytearray(w * ((h + 7) // 8)), w, h, framebuf.MONO_VLSB)


class StatusDisplay:
    """Drop-in for hal.heartbeat.Heartbeat: beat() + run(is_latched, is_provisioning).

    What it draws is hal/status_ui.py: one large item at a time, rotating.
    """

    def __init__(self, pins, i2c, monotonic, info):
        """`info()` -> dict: name, mode, intent, ip, rssi, az, el, sun, reason."""
        import framebuf

        self.panel = Panel(pins, i2c)
        self.buf = bytearray(Panel.W * Panel.H * 2)
        self.fb = _Swapped(framebuf.FrameBuffer(self.buf, Panel.W, Panel.H, framebuf.RGB565))
        self._monotonic = monotonic
        self._info = info
        self.last_cycle = monotonic()
        self._beats = 0
        # Scale the characters that change most, now, at boot: done lazily, the
        # first appearance of each costs ~10 ms and a new card could stall the
        # event loop -- and with it the control loop -- by 0.2 s.
        from hal import status_ui

        for ch in "0123456789.-":
            status_ui.glyph(ch, 2, _mono)
            status_ui.glyph(ch, 3, _mono)

    def beat(self):
        self.last_cycle = self._monotonic()
        self._beats += 1

    def pattern(self, latched, provisioning):
        if self._monotonic() - self.last_cycle > STALL_AFTER_S:
            return STALLED
        if latched:
            return FAULT
        return PROVISIONING if provisioning else HEALTHY

    async def run(self, is_latched, is_provisioning):
        from hal import status_ui

        while True:
            try:
                state = self.pattern(is_latched(), is_provisioning())
                status_ui.draw(self.fb, _mono, state, self._info(), self._monotonic(),
                               self._beats % 2 == 1)
                self.panel.show(self.buf)
            except Exception:  # noqa: BLE001 - the screen must never stop the machine
                pass
            await asyncio.sleep(0.5)
