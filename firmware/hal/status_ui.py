"""What the AtomS3R's 128x128 screen shows: one thing at a time, large.

Cards rotate every CARD_S seconds:

    MODE     the mode, big, in its colour (amber when tracking: the sun)
    MIRROR   where the mirror points: azimuth and elevation
    SUN      the sun's elevation, big, and its azimuth
    WIFI     the IP address, and signal bars

A latched fault interleaves a red FAULT card with every other card, and
Bluetooth setup does the same with an amber SETUP card, so the thing that
needs a person is never more than one card away. A stopped control loop
replaces everything with STALLED.

MicroPython's framebuf has one 8x8 font. Larger text here is that font scaled
2x and 3x with the EPX / AdvMAME3x pixel-art scalers, which round off the
staircase on diagonals instead of just fattening the pixels. Glyphs are read
back through framebuf itself (drawn into an 8x8 buffer), so this module needs
nothing but a FrameBuffer-like object and runs on the laptop for previews
(tools/preview_display.py) and tests.

Colours are given as plain RGB565 here; the panel driver byte-swaps them.
"""

CARD_S = 2.0
W = H = 128


def rgb565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


# The app's palette: solar amber on deep navy.
BG = rgb565(11, 18, 32)
PANEL = rgb565(23, 34, 58)
TEXT = rgb565(236, 240, 247)
MUTED = rgb565(128, 144, 170)
AMBER = rgb565(255, 179, 64)
GREEN = rgb565(70, 200, 120)
BLUE = rgb565(110, 170, 255)
RED = rgb565(240, 70, 70)
RED_BG = rgb565(60, 14, 18)
GREY = rgb565(150, 150, 150)

MODE_COLOUR = {
    "track": AMBER, "defocus": AMBER, "idle": TEXT, "manual": BLUE, "home": BLUE,
    "stow": BLUE, "fault": RED, "estop": RED,
}


# --- scaled, smoothed text ------------------------------------------------------

_glyphs = {}


def _bitmap(ch, fb_factory):
    """8x8 bool grid for one character, read back from framebuf's own font."""
    fb8 = fb_factory(8, 8)
    fb8.fill(0)
    fb8.text(ch, 0, 0, 1)
    return [[bool(fb8.pixel(x, y)) for x in range(8)] for y in range(8)]


def scale2x(g):
    """EPX: each pixel becomes 2x2, corners taken from matching neighbours."""
    h, w = len(g), len(g[0])

    def p(x, y):
        return g[y][x] if 0 <= x < w and 0 <= y < h else False

    out = [[False] * (w * 2) for _ in range(h * 2)]
    for y in range(h):
        for x in range(w):
            e, a, b, c, d = p(x, y), p(x, y - 1), p(x + 1, y), p(x - 1, y), p(x, y + 1)
            out[2 * y][2 * x] = a if (c == a and c != d and a != b) else e
            out[2 * y][2 * x + 1] = b if (a == b and a != c and b != d) else e
            out[2 * y + 1][2 * x] = c if (d == c and d != b and c != a) else e
            out[2 * y + 1][2 * x + 1] = d if (b == d and b != a and d != c) else e
    return out


def scale3x(g):
    """AdvMAME3x: each pixel becomes 3x3 with smoothed edges."""
    h, w = len(g), len(g[0])

    def p(x, y):
        return g[y][x] if 0 <= x < w and 0 <= y < h else False

    out = [[False] * (w * 3) for _ in range(h * 3)]
    for y in range(h):
        for x in range(w):
            a, b, c = p(x - 1, y - 1), p(x, y - 1), p(x + 1, y - 1)
            d, e, f = p(x - 1, y), p(x, y), p(x + 1, y)
            gg, hh, i = p(x - 1, y + 1), p(x, y + 1), p(x + 1, y + 1)
            cells = (
                d if (d == b and b != f and d != hh) else e,
                b if ((d == b and b != f and d != hh and e != c) or
                      (b == f and b != d and f != hh and e != a)) else e,
                f if (b == f and b != d and f != hh) else e,
                d if ((d == b and b != f and d != hh and e != gg) or
                      (d == hh and d != f and hh != b and e != a)) else e,
                e,
                f if ((b == f and b != d and f != hh and e != i) or
                      (hh == f and d != hh and b != f and e != c)) else e,
                d if (d == hh and d != f and hh != b) else e,
                hh if ((d == hh and d != f and hh != b and e != i) or
                       (hh == f and d != hh and b != f and e != gg)) else e,
                f if (hh == f and d != hh and b != f) else e,
            )
            for k in range(9):
                out[3 * y + k // 3][3 * x + k % 3] = cells[k]
    return out


def _runs(grid):
    """Horizontal runs (y, x, length) of set pixels: few fill_rect calls per glyph."""
    runs = []
    for y, row in enumerate(grid):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                runs.append((y, start, x - start))
            else:
                x += 1
    return runs


def glyph(ch, scale, fb_factory):
    key = (ch, scale)
    if key not in _glyphs:
        g = _bitmap(ch, fb_factory)
        if scale == 2:
            g = scale2x(g)
        elif scale == 3:
            g = scale3x(g)
        _glyphs[key] = _runs(g)
    return _glyphs[key]


# Advance per character: the font is 8 px wide but its glyphs leave a blank
# column, so tighten big text a little.
_ADVANCE = {1: 8, 2: 14, 3: 21}


def text_width(s, scale):
    return len(s) * _ADVANCE[scale] - (_ADVANCE[scale] - 7 * scale if s else 0)


class Painter:
    def __init__(self, fb, fb_factory):
        self.fb = fb
        self.mk = fb_factory

    def text(self, s, x, y, scale, colour):
        if scale == 1:
            self.fb.text(s, x, y, colour)
            return
        for ch in s:
            for gy, gx, n in glyph(ch, scale, self.mk):
                self.fb.fill_rect(x + gx, y + gy, n, 1, colour)
            x += _ADVANCE[scale]

    def centred(self, s, y, scale, colour):
        self.text(s, (W - text_width(s, scale)) // 2, y, scale, colour)

    def degree(self, x, y, scale, colour):
        """A small ring for the degree sign (the font has none)."""
        r = 2 if scale > 1 else 1
        self.fb.ellipse(x + r, y + r, r, r, colour)


def fits(s, scale):
    return text_width(s, scale) <= W - 8


def best_scale(s, largest=3):
    for scale in (largest, 2, 1):
        if scale <= largest and fits(s, scale):
            return scale
    return 1


def wrap(text, width):
    """Word-wrap to lines of at most `width` characters."""
    lines, line = [], ""
    for word in text.split():
        while len(word) > width:
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _num(value, digits=1):
    return "--" if value is None else "{:.{}f}".format(value, digits)


# --- the cards -------------------------------------------------------------------

def card_mode(p, info):
    mode = (info.get("mode") or "?").upper()
    colour = MODE_COLOUR.get(info.get("mode"), TEXT)
    _title(p, "MODE")
    p.centred(mode, 50, best_scale(mode), colour)
    intent = info.get("intent")
    if intent and intent != info.get("mode") and intent in ("track", "defocus"):
        p.centred("will " + intent, 86, 1, MUTED)
    else:
        p.centred(info.get("name") or "", 86, 1, MUTED)


def card_mirror(p, info):
    _title(p, "MIRROR")
    for label, key, y in (("AZ", "az", 36), ("EL", "el", 72)):
        p.text(label, 8, y + 6, 1, MUTED)
        value = _num(info.get(key))
        x = W - 14 - text_width(value, 2)
        p.text(value, x, y, 2, TEXT)
        if info.get(key) is not None:
            p.degree(W - 11, y, 2, TEXT)


def card_sun(p, info):
    _title(p, "SUN")
    sun = info.get("sun")
    if not sun:
        p.centred("NO", 40, 3, MUTED)
        p.centred("TIME", 70, 3, MUTED)
        return
    az, el = sun
    value = _num(el)
    scale = best_scale(value + " ")
    x = (W - text_width(value, scale) - 6) // 2
    p.text(value, x, 44, scale, AMBER if el > 0 else BLUE)
    p.degree(x + text_width(value, scale) + 2, 44, scale, AMBER if el > 0 else BLUE)
    p.centred("elevation", 72, 1, MUTED)
    p.centred("az " + _num(az, 0), 88, 2, TEXT)


def card_wifi(p, info):
    _title(p, "WIFI")
    ip = info.get("ip")
    if not ip:
        p.centred("OFF", 46, 3, MUTED)
        p.centred("no network", 82, 1, MUTED)
        return
    parts = ip.split(".")
    p.centred(".".join(parts[:2]), 34, 2, TEXT)
    p.centred("." + ".".join(parts[2:]), 58, 2, TEXT)
    _signal(p, info.get("rssi"), 86)


def card_fault(p, info):
    p.fb.fill(RED_BG)
    p.fb.fill_rect(0, 0, W, 4, RED)
    p.centred("FAULT", 14, 3, RED)
    reason = info.get("reason") or "latched"
    # Large if it fits in three lines without splitting a word, else small.
    lines, scale, step = wrap(reason, 9), 2, 21
    if len(lines) > 3 or any(len(w) > 9 for w in reason.split()):
        lines, scale, step = wrap(reason, 15), 1, 12
    lines = lines[:3 if scale == 2 else 5]
    y = 48 + ((3 if scale == 2 else 5) - len(lines)) * step // 2  # centre the block
    for line in lines:
        p.centred(line, y, scale, TEXT)
        y += step
    p.centred("clear in app", 116, 1, MUTED)


def card_setup(p, info):
    p.fb.fill_rect(0, 0, W, 4, AMBER)
    p.centred("BLE", 22, 3, AMBER)
    p.centred("SETUP", 52, 2, TEXT)
    p.centred("open the app to", 84, 1, MUTED)
    p.centred("connect WiFi", 96, 1, MUTED)


def card_stalled(p, info):
    p.fb.fill_rect(0, 0, W, 4, GREY)
    p.centred("STOPPED", 40, 2, GREY)
    p.centred("control loop", 74, 1, MUTED)
    p.centred("not running", 86, 1, MUTED)


def _title(p, s):
    p.text(s, 8, 10, 1, MUTED)


def _signal(p, rssi, y):
    """Four bars, filled by strength, and the dBm beside them, centred."""
    level = 0 if rssi is None else 4 if rssi >= -55 else 3 if rssi >= -65 else 2 if rssi >= -75 else 1
    label = "" if rssi is None else "{} dBm".format(rssi)
    x = (W - (22 + (6 + text_width(label, 1) if label else 0))) // 2
    for i in range(4):
        h = 4 + 4 * i
        colour = GREEN if i < level else PANEL
        p.fb.fill_rect(x + i * 6, y + 16 - h, 4, h, colour)
    if label:
        p.text(label, x + 28, y + 8, 1, MUTED)


NORMAL = (card_mode, card_mirror, card_sun, card_wifi)


def sequence(state):
    """The card rotation for a state: urgent cards interleaved with the rest."""
    if state == "stalled":
        return [card_stalled]
    urgent = {"fault": card_fault, "provisioning": card_setup}.get(state)
    if urgent is None:
        return list(NORMAL)
    out = []
    for card in NORMAL:
        out += [urgent, card]
    return out


def draw(fb, fb_factory, state, info, now, alive):
    """Render the card due at time `now` into `fb`. `alive` toggles per control cycle."""
    cards = sequence(state)
    index = int(now // CARD_S) % len(cards)
    card = cards[index]
    p = Painter(fb, fb_factory)
    fb.fill(BG)
    card(p, info)
    if card in NORMAL:
        # Page dots for the normal cards, current one in amber.
        n = len(NORMAL)
        x0 = (W - (n * 8 - 4)) // 2
        for i, c in enumerate(NORMAL):
            fb.fill_rect(x0 + i * 8, 118, 4, 4, AMBER if c is card else PANEL)
    if state != "stalled" and card in NORMAL:
        # The pulse: flips once per completed control cycle, so it freezes with the loop.
        colour = GREEN if state != "fault" else RED
        if alive:
            fb.ellipse(119, 13, 3, 3, colour, True)
        else:
            fb.ellipse(119, 13, 3, 3, colour)
    return card.__name__
