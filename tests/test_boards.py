"""The per-board configs (boards/*.json) and port detection (tools/port.py)."""

import sys
from pathlib import Path

import pytest
from hal import profiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import port  # noqa: E402


def test_every_board_config_has_a_matching_firmware_profile():
    for name, cfg in port.boards().items():
        assert cfg["profile"] in profiles.PROFILES, name
        assert profiles.PROFILES[cfg["profile"]]["chip"] == cfg["chip"], name
        assert ("micropython_board" in cfg) != ("micropython_board_dir" in cfg), name
        if "micropython_board_dir" in cfg:
            assert (port.BOARDS / cfg["micropython_board_dir"] / "mpconfigboard.cmake").exists()


def test_every_firmware_profile_is_buildable():
    profiles_with_boards = {cfg["profile"] for cfg in port.boards().values()}
    assert profiles_with_boards == set(profiles.PROFILES)


@pytest.fixture
def by_id(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIOSTAT_PORT", raising=False)
    monkeypatch.delenv("HELIOSTAT_BOARD", raising=False)

    def plug(*names):
        for i, n in enumerate(names):
            target = tmp_path / f"tty{i}"
            target.touch()
            (tmp_path / n).symlink_to(target)
        return tmp_path

    return plug


def test_the_only_board_plugged_in_is_picked(by_id):
    d = by_id("usb-Espressif_USB_JTAG_serial_debug_unit_E4:B3-if00")
    board, p = port.resolve(by_id=d)
    assert board == "atoms3r" and p.endswith("tty0")


def test_two_boards_need_a_choice(by_id):
    d = by_id("usb-Espressif_USB_JTAG_serial_debug_unit-if00",
              "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge-if00-port0")
    with pytest.raises(port.PortError, match="several"):
        port.resolve(by_id=d)
    assert port.resolve("esp32_devkit", by_id=d)[1].endswith("tty1")


def test_nothing_plugged_in_is_said_plainly(by_id):
    with pytest.raises(port.PortError, match="no heliostat board"):
        port.resolve(by_id=by_id())


def test_an_explicit_port_wins(by_id, monkeypatch):
    monkeypatch.setenv("HELIOSTAT_PORT", "/dev/ttyX")
    assert port.resolve(by_id=by_id())[1] == "/dev/ttyX"
