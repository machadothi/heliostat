#!/usr/bin/env python3
"""Render the AtomS3R status screen's cards to a PNG, on the laptop.

    python3 tools/preview_display.py [out.png]

Uses firmware/hal/status_ui.py unchanged, drawing into tools/fbsim.py (a
CPython framebuf with MicroPython's own font), so what you see is what the
board draws. Each 128x128 card is shown at 3x.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "firmware"))
sys.path.insert(0, str(REPO / "tools"))
import fbsim  # noqa: E402
from hal import status_ui as ui  # noqa: E402

ZOOM = 3
GAP = 24

INFO = {
    "name": "my_heliostat", "mode": "track", "intent": "track",
    "ip": "192.168.50.226", "rssi": -56, "az": 182.4, "el": 41.7, "sun": (214.6, 31.2),
    "reason": None,
}

SCENES = [
    ("healthy", INFO, 0.0),
    ("healthy", INFO, 2.0),
    ("healthy", INFO, 4.0),
    ("healthy", INFO, 6.0),
    ("healthy", dict(INFO, mode="idle", intent="track", sun=(310.2, -24.3), rssi=-78), 4.0),
    ("fault", dict(INFO, mode="fault", reason="el: load 1000 over limit"), 0.0),
    ("fault", dict(INFO, mode="fault", reason="az: not responding"), 2.0),
    ("provisioning", dict(INFO, ip=None, mode="idle"), 0.0),
    ("fault", dict(INFO, mode="stow", reason="base tilted 7.4 deg"), 0.0),
    ("stalled", INFO, 0.0),
]


def render(state, info, now, alive=True):
    fb = fbsim.FrameBuffer(None, ui.W, ui.H)
    name = ui.draw(fb, fbsim.mono, state, info, now, alive)
    img = Image.new("RGB", (ui.W, ui.H))
    img.putdata([fbsim.to_rgb(c) for row in fb.px for c in row])
    return img.resize((ui.W * ZOOM, ui.H * ZOOM), Image.NEAREST), name


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "build" / "display_preview.png"
    cols = 5
    rows = (len(SCENES) + cols - 1) // cols
    cell = ui.W * ZOOM
    sheet = Image.new("RGB", (cols * (cell + GAP) + GAP, rows * (cell + GAP + 18) + GAP), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    for i, (state, info, now) in enumerate(SCENES):
        img, name = render(state, info, now)
        x = GAP + (i % cols) * (cell + GAP)
        y = GAP + (i // cols) * (cell + GAP + 18)
        sheet.paste(img, (x, y + 18))
        draw.text((x, y), f"{state}: {name}", fill=(200, 200, 200))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(out)


if __name__ == "__main__":
    main()
