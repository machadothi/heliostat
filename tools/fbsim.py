"""A CPython stand-in for MicroPython's `framebuf`, enough to preview the screen.

Implements the calls firmware/hal/status_ui.py makes (fill, fill_rect, pixel,
text, ellipse) with the same font and the same text rasterisation as
MicroPython's extmod/modframebuf.c, so a preview shows what the board draws.

The font is MicroPython's font_petme128_8x8 (extmod/font_petme128_8x8.h),
MIT licence, Copyright (c) 2013, 2014 Damien P. George.
"""

MONO_VLSB = 0
RGB565 = 1

_FONT = bytes.fromhex(
    "00000000000000000000004f4f0000000007070000070700147f7f14147f7f14"
    "00242e6b6b3a1200006333180c66630000327f4d4d7772500000000406030100"
    "00001c3e63410000000041633e1c0000082a3e1c1c3e2a080008083e3e080800"
    "000080e0600000000008080808080800000000606000000000406030180c0602"
    "003e7f49457f3e000040447f7f40400000627351494f460000226349497f3600"
    "00181814167f7f1000276745457d3900003e7f49497b3200000303797d070300"
    "00367f49497f360000266f49497f3e000000002424000000000080e464000000"
    "00081c3663414100001414141414140000414163361c080000020351590f0600"
    "003e7f414d4f2e00007c7e0b0b7e7c00007f7f49497f3600003e7f4141632200"
    "007f7f41633e1c00007f7f4949414100007f7f0909010100003e7f41497b3a00"
    "007f7f08087f7f000000417f7f410000002060417f3f0100007f7f1c36634100"
    "007f7f4040404000007f7f060c067f7f007f7f0e1c7f7f00003e7f41417f3e00"
    "007f7f09090f0600001e3f21617f5e00007f7f19396f460000266f49497b3200"
    "0001017f7f010100003f7f40407f3f00001f3f60603f1f00007f7f3018307f7f"
    "0063771c1c77630000070f78780f0700006171594d47430000007f7f41410000"
    "0002060c18306040000041417f7f000000080c06060c0800c0c0c0c0c0c0c0c0"
    "000001030604000000207454547c7800007f7f44447c380000387c44446c2800"
    "00387c44447f7f0000387c54545c580000087e7f090302000098bca4a4fc7c00"
    "007f7f04047c78000000007d7d0000000040c08080fd7d00007f7f30386c4400"
    "0000417f7f400000007c7c1830187c7c007c7c04047c780000387c44447c3800"
    "00fcfc24243c180000183c2424fcfc00007c7c04040c080000485c5454742000"
    "04043f7f44642000003c7c40407c3c00001c3c60603c1c00001c7c3018307c1c"
    "00446c38386c4400009cbca0a0fc7c00004464745c4c44000008083e77414100"
    "000000ffff000000004141773e0808000002030103020301aa55aa55aa55aa55"
)


class FrameBuffer:
    def __init__(self, buf, width, height, fmt=RGB565):
        self.width, self.height = width, height
        self.px = [[0] * width for _ in range(height)]

    def fill(self, c):
        for row in self.px:
            row[:] = [c] * self.width

    def pixel(self, x, y, c=None):
        if not (0 <= x < self.width and 0 <= y < self.height):
            return 0 if c is None else None
        if c is None:
            return self.px[y][x]
        self.px[y][x] = c

    def fill_rect(self, x, y, w, h, c):
        for yy in range(max(0, y), min(self.height, y + h)):
            for xx in range(max(0, x), min(self.width, x + w)):
                self.px[yy][xx] = c

    def text(self, s, x0, y0, c=1):
        for ch in s:
            code = ord(ch)
            if code < 32 or code > 127:
                code = 127
            cols = _FONT[(code - 32) * 8:(code - 32) * 8 + 8]
            for j, col in enumerate(cols):
                y = y0
                while col:
                    if col & 1:
                        self.pixel(x0 + j, y, c)
                    col >>= 1
                    y += 1
            x0 += 8

    def ellipse(self, cx, cy, rx, ry, c, fill=False):
        # Close enough to MicroPython's midpoint ellipse for a preview.
        for y in range(-ry, ry + 1):
            for x in range(-rx, rx + 1):
                d = (x * x) / (rx * rx or 1) + (y * y) / (ry * ry or 1)
                if (fill and d <= 1.0) or (not fill and 0.55 <= d <= 1.25):
                    self.pixel(cx + x, cy + y, c)


def mono(width, height):
    """A small 1-bit framebuffer, as status_ui reads glyphs back from one."""
    return FrameBuffer(None, width, height, MONO_VLSB)


def to_rgb(c):
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    return (r * 255 // 31, g * 255 // 63, b * 255 // 31)
