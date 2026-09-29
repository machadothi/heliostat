"""Enforce the rule that makes everything else testable without hardware.

`solar/` and `control/` must import nothing from `hal/` or `net/`. If that holds,
the solar math, the reflection geometry, the kinematics and the safety rules all
run under pytest on a laptop. The moment someone adds `from hal import board` to
`tracker.py` to save a constructor argument, the whole test suite needs a board.

This test exists so that regression is caught the same day it happens.
"""

import ast
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware"

PURE_PACKAGES = ("solar", "control")
FORBIDDEN_PACKAGES = ("hal", "net", "ble")


def _iter_pure_modules():
    for package in PURE_PACKAGES:
        yield from sorted((FIRMWARE / package).glob("*.py"))


def _imported_roots(source: str):
    """Yield the top-level name of every module imported by `source`."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        # `from . import x` has no module; relative imports stay in-package.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


@pytest.mark.parametrize("path", list(_iter_pure_modules()), ids=lambda p: p.name)
def test_pure_module_has_no_hardware_imports(path):
    offenders = sorted(
        {root for root in _imported_roots(path.read_text()) if root in FORBIDDEN_PACKAGES}
    )
    assert not offenders, (
        f"{path.relative_to(FIRMWARE)} imports {offenders}. "
        f"Pass hardware in through the constructor instead - see tests/conftest.py."
    )


def test_driver_is_a_verbatim_copy():
    """hal/scservo.py must stay byte-identical to the proven original.

    It is the validated hardware layer. Subclass it if behaviour needs to change
    (see the asyncio note in the plan); never edit it in place, so an upstream
    fix can be dropped straight in.
    """
    original = Path.home() / "esp32-servo" / "scservo.py"
    if not original.exists():
        pytest.skip("original driver not present on this machine")
    assert (FIRMWARE / "hal" / "scservo.py").read_bytes() == original.read_bytes()
