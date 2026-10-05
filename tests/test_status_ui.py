"""The AtomS3R screen's cards, drawn into the laptop framebuf stand-in."""

import sys
from pathlib import Path

import pytest
from hal import status_ui as ui

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fbsim  # noqa: E402

INFO = {"name": "my_heliostat", "mode": "track", "intent": "track", "ip": "192.168.50.226",
        "rssi": -56, "az": 182.4, "el": 41.7, "sun": (214.6, 31.2), "reason": None}


def render(state, info, now):
    fb = fbsim.FrameBuffer(None, ui.W, ui.H)
    return ui.draw(fb, fbsim.mono, state, info, now, True), fb


@pytest.mark.parametrize("state,info", [
    ("healthy", INFO),
    ("healthy", dict(INFO, ip=None, sun=None, az=None, el=None, mode=None)),
    ("fault", dict(INFO, mode="fault", reason="el: load 1000 over limit")),
    ("fault", dict(INFO, mode="fault", reason="a very long reason that will not fit in three lines")),
    ("provisioning", INFO),
    ("stalled", INFO),
])
def test_every_card_draws_in_every_state(state, info):
    for now in range(0, 20, 2):
        render(state, info, float(now))


def test_cards_rotate_every_two_seconds():
    names = [render("healthy", INFO, t)[0] for t in (0.0, 1.9, 2.0, 4.0, 6.0, 8.0)]
    assert names == ["card_mode", "card_mode", "card_mirror", "card_sun", "card_wifi", "card_mode"]


def test_a_fault_is_never_more_than_one_card_away():
    names = [render("fault", dict(INFO, reason="x"), t)[0] for t in range(0, 16, 2)]
    assert names[::2] == ["card_fault"] * 4
    assert set(names[1::2]) == {"card_mode", "card_mirror", "card_sun", "card_wifi"}


def test_big_text_that_is_shown_fits_the_screen():
    for s in ("TRACK", "MANUAL", "DEFOCUS", "182.4", "-24.3", "192.168", ".50.226", "1000 over"):
        assert ui.text_width(s, ui.best_scale(s)) <= ui.W, s
    assert ui.best_scale("TRACK") == 3 and ui.best_scale("DEFOCUS") == 2


def test_scale2x_rounds_a_diagonal_where_plain_scaling_would_not():
    g = [[False, True], [True, False]]  # an anti-diagonal: a staircase when fattened
    out = ui.scale2x(g)
    # Plain 2x leaves the two inner corners empty (a jagged step); EPX fills them.
    assert out[1][1] and out[2][2]
    assert not out[0][0] and not out[3][3]  # the outer corners stay empty
    assert ui.scale3x([[True]]) == [[True] * 3] * 3


def test_wrap_never_exceeds_the_width():
    for line in ui.wrap("el: load 1000 over limit and then some more words", 9):
        assert len(line) <= 9
