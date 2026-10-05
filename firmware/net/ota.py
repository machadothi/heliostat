"""Firmware updates over WiFi, into the spare application slot (AtomS3R).

    POST /api/ota      body: the application image (build/<board>/micropython.bin)
                       X-OTA-Token: the board's token     X-SHA256: hex digest

The image is streamed into the OTA slot that is not running, 4 KB at a time,
hashed on the way, and only made the boot slot if the hash matches. The board
then restarts into it. tools/deploy.py --ota does all of this.

Safety, because this machine aims concentrated sunlight:
  * a token is required. It is set over USB by tools/deploy.py and lives in the
    board's config.json (masked in GET /api/config); without one, OTA is off;
  * only while nothing is being driven automatically (idle, manual, fault,
    E-stop): flash writes stall the CPU in bursts, and a reboot mid-track would
    leave the mirror wherever it was;
  * ROLLBACK. The new firmware boots "pending verification". It marks itself
    valid only once it is healthy (app.mark_healthy_after: WiFi joined and the
    control loop cycling). If it crashes first, main.py resets, and the
    bootloader -- seeing an unverified image -- goes back to the previous one.

The flash access goes through a small "slot" object so the protocol logic runs
under pytest; on the board it is esp32.Partition.
"""

BLOCK = 4096
PENDING_FLAG = "ota_pending"  # on the filesystem while a new image is unverified
APP_IMAGE_MAGIC = 0xE9  # first byte of every ESP32 application image
SAFE_MODES = ("idle", "manual", "fault", "estop")


class OtaError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class PartitionSlot:
    """The next OTA partition, as esp32.Partition exposes it."""

    def __init__(self):
        from esp32 import Partition

        self.part = Partition(Partition.RUNNING).get_next_update()

    def size(self):
        return self.part.info()[3]

    def write(self, block_index, data):
        # The 2-argument form erases the block before writing it.
        self.part.writeblocks(block_index, data)

    def activate(self):
        self.part.set_boot()


def _equal(a, b):
    """Constant-time comparison, so the token cannot be guessed byte by byte."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


class Ota:
    def __init__(self, tracker, store, schedule_reboot, slot_factory=PartitionSlot, flag_path=PENDING_FLAG):
        self.tracker = tracker
        self.store = store
        self.schedule_reboot = schedule_reboot
        self.slot_factory = slot_factory
        self.flag_path = flag_path
        self.busy = False

    async def receive(self, reader, length, headers):
        """The streaming handler httpd calls for POST /api/ota. Returns (status, dict)."""
        try:
            return 200, await self._receive(reader, length, headers)
        except OtaError as exc:
            return exc.status, {"error": str(exc)}

    async def _receive(self, reader, length, headers):
        import hashlib

        token = self.store.data.get("ota", {}).get("token") or ""
        if not token:
            raise OtaError(409, "OTA is not set up on this board: deploy once over USB")
        if not _equal(headers.get("x-ota-token", ""), token):
            raise OtaError(403, "wrong OTA token")
        if self.tracker.mode not in SAFE_MODES:
            raise OtaError(409, "update in idle (now {})".format(self.tracker.mode))
        expected = headers.get("x-sha256", "").lower()
        if len(expected) != 64:
            raise OtaError(400, "X-SHA256 header missing")
        if self.busy:
            raise OtaError(409, "an update is already in progress")
        try:
            slot = self.slot_factory()
        except Exception:  # noqa: BLE001 - a build with one app partition (the devkit's)
            raise OtaError(409, "this board's firmware has no OTA slots: deploy over USB") from None
        if length <= 0 or length > slot.size():
            raise OtaError(413, "image of {} bytes does not fit the {}-byte slot".format(length, slot.size()))

        self.busy = True
        try:
            digest = hashlib.sha256()
            remaining, index = length, 0
            buf = bytearray(BLOCK)
            while remaining:
                n = min(BLOCK, remaining)
                chunk = await reader.readexactly(n)
                if index == 0 and chunk[0] != APP_IMAGE_MAGIC:
                    raise OtaError(400, "not an ESP32 application image")
                digest.update(chunk)
                buf[:n] = chunk
                for i in range(n, BLOCK):
                    buf[i] = 0xFF  # erased flash
                slot.write(index, buf)
                remaining -= n
                index += 1
            got = "".join("{:02x}".format(b) for b in digest.digest())
            if got != expected:
                raise OtaError(400, "SHA-256 mismatch: the image was damaged on the way; nothing changed")
            self._set_pending()
            slot.activate()
        finally:
            self.busy = False
        self.schedule_reboot()
        return {"ok": True, "bytes": length, "rebooting": True}

    def _set_pending(self):
        with open(self.flag_path, "w") as f:
            f.write("1")


def pending(flag_path=PENDING_FLAG):
    try:
        import os

        os.stat(flag_path)
        return True
    except OSError:
        return False


def mark_valid(flag_path=PENDING_FLAG):
    """The running image is healthy: cancel the rollback, clear the flag."""
    try:
        from esp32 import Partition

        Partition.mark_app_valid_cancel_rollback()
    except Exception:  # noqa: BLE001 - not an OTA build, or already valid
        pass
    try:
        import os

        os.remove(flag_path)
    except OSError:
        pass
