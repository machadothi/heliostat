"""The BLE GATT contract. Mirrored exactly by the Android app's BleConstants.kt.

Change a UUID, a name or a byte layout here and it must change there, and in
docs/protocol.md -- which is the full reference, with example payloads.

Every characteristic carries a Characteristic User Description (0x2901), which
is what nRF Connect and similar tools display as its name. GATT has no standard
way to name a SERVICE, so tools show a custom one as "Unknown Service"; the
INFO characteristic reports the service name instead.

Service  8a4f1000-5a10-4c6b-9e2a-68656c696f73     ("...68656c696f73" is "helios")

  1001 INFO        read          UTF-8 JSON {"service","fw","name","wifi","ip","ssid"}
  1002 SSID        write         UTF-8 network name (framing optional, see below)
  1003 PSK         write         UTF-8 password     (framing optional)
  1004 COMMAND     write         1 byte: 0x01 join, 0x02 scan, 0x03 forget
  1005 WIFI_STATE  read, notify  6 bytes: state u8, reason u8, IPv4 4 bytes
  1006 SCAN        read          UTF-8 lines "ssid<TAB>channel<TAB>rssi"
  1007 TIME        write         6 bytes little-endian: u32 unix seconds, i16 tz minutes
  1008 LOCATION    write         12 bytes little-endian: f32 lat, f32 lon, f32 elevation m

SSID/PSK FRAMING. At the default MTU of 23 a write carries only 20 bytes, while
an SSID can be 32 and a password 64. So a value may be sent in chunks, each
prefixed with a sequence byte: 0x00, 0x01, ... with bit 7 (0x80) set on the
LAST chunk. A value sent whole in one framed write is 0x80 + text.

The sequence byte is OPTIONAL when the whole value fits in one write: a write
whose first byte is an ordinary character (anything outside 0x00-0x0F and
0x80-0x8F) is taken as the complete value. That lets nRF Connect write the SSID
as plain TEXT. UTF-8 never starts a string with a byte in 0x80-0x8F, and an SSID
starting with a control character is not a thing, so the two cannot collide.
"""

import bluetooth

_BASE = "8a4f{}-5a10-4c6b-9e2a-68656c696f73"
SERVICE_NAME = "Heliostat provisioning"

_READ = 0x0002
_WRITE = 0x0008
_NOTIFY = 0x0010
_DESCRIPTOR_READ = 0x0002  # same flag values as characteristics; 0x0001 is BROADCAST
_USER_DESCRIPTION = bluetooth.UUID(0x2901)

# (key, uuid suffix, properties, name shown in nRF Connect)
CHARACTERISTICS = (
    ("info", "1001", _READ, "Info (JSON)"),
    ("ssid", "1002", _WRITE, "WiFi SSID (text)"),
    ("psk", "1003", _WRITE, "WiFi password (text)"),
    ("command", "1004", _WRITE, "Command: 01 join, 02 scan, 03 forget"),
    ("wifi_state", "1005", _READ | _NOTIFY, "WiFi state: state,reason,ip[4]"),
    ("scan", "1006", _READ, "WiFi scan (text)"),
    ("time", "1007", _WRITE, "Time: u32 unix, i16 tz min (LE)"),
    ("location", "1008", _WRITE, "Location: f32 lat, lon, elev (LE)"),
)

SERVICE_UUID = bluetooth.UUID(_BASE.format("1000"))
UUIDS = {key: bluetooth.UUID(_BASE.format(suffix)) for key, suffix, _, _ in CHARACTERISTICS}

SERVICE = (
    SERVICE_UUID,
    tuple(
        (UUIDS[key], flags, ((_USER_DESCRIPTION, _DESCRIPTOR_READ),))
        for key, _, flags, _ in CHARACTERISTICS
    ),
)


def map_handles(registered):
    """Turn gatts_register_services' flat tuple into two dicts keyed by name.

    The stack returns, per characteristic, its value handle followed by the
    handles of the descriptors WE declared (the notify CCCD it adds itself is
    not listed) -- confirmed on this board: ((16, 17, 19, 21, 23, 24),) for
    three characteristics with one descriptor each.
    """
    values, names = {}, {}
    flat = list(registered)
    for i, (key, _, _, _) in enumerate(CHARACTERISTICS):
        values[key] = flat[2 * i]
        names[key] = flat[2 * i + 1]
    return values, names


CMD_JOIN = 1
CMD_SCAN = 2
CMD_FORGET = 3

# WIFI_STATE byte 0
STATE_IDLE = 0
STATE_JOINING = 1
STATE_JOINED = 2
STATE_FAILED = 3
STATE_SCANNING = 4
STATE_SCAN_READY = 5

# WIFI_STATE byte 1, meaningful when state is FAILED
REASON_NONE = 0
REASON_WRONG_PASSWORD = 1
REASON_NOT_FOUND = 2
REASON_TIMEOUT = 3
REASON_OTHER = 4

SEQ_FINAL = 0x80

# Value buffer sizes. The stack's default is 20 bytes, far too small for these.
BUFFER_SIZES = {"info": 200, "ssid": 64, "psk": 96, "scan": 512}
NAME_BUFFER = 48


def is_framed(value):
    """True if the write starts with a chunk sequence byte rather than text."""
    return len(value) > 0 and (value[0] <= 0x0F or 0x80 <= value[0] <= 0x8F)
