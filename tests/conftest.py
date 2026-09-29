"""Make the firmware importable from the host.

The architectural rule this relies on: `solar/` and `control/` import nothing
from `hal/` or `net/`. That is what lets the whole of the solar math, the
reflection geometry and the kinematics run under pytest on a laptop with no
board attached.
"""

import sys
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware"

if str(FIRMWARE) not in sys.path:
    sys.path.insert(0, str(FIRMWARE))
