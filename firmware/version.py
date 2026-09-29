"""Single source of truth for the firmware version.

Exposed over BLE (DEVICE_INFO) and HTTP (GET /api/status) so the phone can tell
what it is talking to.
"""

FW_VERSION = "0.1.0"
PROTOCOL_VERSION = 1
