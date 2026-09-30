"""The HTTP API: routes and handlers, with no knowledge of sockets.

Every handler takes the request's decoded pieces and returns (status, dict).
That makes the whole API importable on CPython: tools/mock_server.py serves these
exact handlers from a laptop, against a simulated tracker, so the Android app can
be built end to end with no ESP32 on the desk.

docs/protocol.md is the contract. Anything changed here changes there.

Errors are JSON too, always {"error": "..."}, so the app has one shape to parse:
    400  malformed request, or a value the config validator rejects
    404  no such route
    409  a valid request the machine refuses right now (latched, no time, ...)
    500  a bug
"""

import gc
import json

from config import ConfigError
from control import modes
from control.tracker import CommandRejected
from solar.sunpos import sun_position
from util.mem import idf_heap
from version import FW_VERSION, PROTOCOL_VERSION


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Api:
    """Binds the routes to one tracker, config store and (optionally) WiFi manager."""

    def __init__(self, tracker, store, wifi=None, uptime=None, timesync=None):
        self.tracker = tracker
        self.store = store
        self.wifi = wifi
        self.timesync = timesync
        self._uptime = uptime
        self._routes = {
            ("GET", "/api/status"): self.get_status,
            ("GET", "/api/telemetry"): self.get_telemetry,
            ("GET", "/api/sun"): self.get_sun,
            ("GET", "/api/config"): self.get_config,
            ("GET", "/api/log"): self.get_log,
            ("POST", "/api/config"): self.post_config,
            ("POST", "/api/time"): self.post_time,
            ("POST", "/api/location"): self.post_location,
            ("POST", "/api/target"): self.post_target,
            ("POST", "/api/target/capture"): self.post_capture,
            ("POST", "/api/mode"): self.post_mode,
            ("POST", "/api/jog"): self.post_jog,
            ("POST", "/api/clear"): self.post_clear,
            ("POST", "/api/wifi/forget"): self.post_wifi_forget,
            ("POST", "/api/imu/level"): self.post_imu_level,
        }

    async def handle(self, method, path, query, body):
        """Dispatch one request. Never raises; always returns (status, dict)."""
        handler = self._routes.get((method, path))
        if handler is None:
            known = [p for (_, p) in self._routes]
            status = 405 if path in known else 404
            return status, {"error": f"{method} {path} not found"}

        # Any request at all counts as contact for the comms-loss rule.
        self.tracker.touch()
        try:
            payload = _decode(body) if method == "POST" else {}
            result = handler(query, payload)
            if hasattr(result, "send"):  # a coroutine: jog moves a servo
                result = await result
            return 200, result
        except ApiError as exc:
            return exc.status, {"error": exc.message}
        except CommandRejected as exc:
            return 409, {"error": str(exc)}
        except (ConfigError, ValueError, KeyError, TypeError) as exc:
            return 400, {"error": f"bad request: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the server
            return 500, {"error": f"internal error: {exc}"}

    # -- reads ------------------------------------------------------------------

    def get_status(self, query, payload):
        status = self.tracker.status()
        gc.collect()
        status["device"] = {
            "fw": FW_VERSION,
            "protocol": PROTOCOL_VERSION,
            "name": self.store["ble"]["name"],
            "sim": self.store["sim"],
            "mem_free": gc.mem_free() if hasattr(gc, "mem_free") else None,
            "uptime_s": self._uptime() if self._uptime else None,
            "idf_heap": idf_heap(),
        }
        status["wifi"] = self.wifi.status() if self.wifi else None
        status["time_sync"] = self.timesync.status() if self.timesync else None
        status["site"] = self.store["site"]
        return status

    def get_telemetry(self, query, payload):
        """The small, frequent subset the dashboard polls at 2 Hz."""
        tr = self.tracker
        az, el = tr.az.state, tr.el.state
        return {
            "mode": tr.mode,
            "intent": tr.intent,
            "time": tr.clock.now(),
            "sun": tr._sun,
            "plan": tr._plan,
            "pos": (az["position_deg"], el["position_deg"]),
            "moving": (az["moving"], el["moving"]),
            "beam": tr._beam,
            "efficiency": tr._efficiency,
            "trips": [trip.rule for trip in tr.trips],
            "latched": tr.latch.reason if tr.latch else None,
            "tilt_deg": tr._tilt,
            "accel_g": tr._accel,
            "imu_calibrated": getattr(tr.sensors.imu, "calibrated", None),
            "volts": (az["volts"], el["volts"]),
            "temp_c": (az["temp_c"], el["temp_c"]),
        }

    def get_sun(self, query, payload):
        """Solar position at any Unix time: validate the firmware's own math with curl."""
        site = self.store["site"]
        if "t" in query:
            # int when it is one: float("1782042013") is 128 s off on the board.
            t = query["t"]
            epoch = int(t) if t.lstrip("-").isdigit() else float(t)
        else:
            epoch = self.tracker.clock.now()
            if epoch is None:
                raise ApiError(409, "no valid time; pass ?t=<unix seconds>")
        lat = float(query.get("lat", site["lat"]))
        lon = float(query.get("lon", site["lon"]))
        azimuth, elevation, apparent = sun_position(epoch, lat, lon)
        return {"t": epoch, "lat": lat, "lon": lon, "azimuth": azimuth,
                "elevation": elevation, "apparent_elevation": apparent}

    def get_config(self, query, payload):
        # Never hand the WiFi password back out.
        data = dict(self.store.data)
        data["wifi"] = dict(data["wifi"], psk="***" if data["wifi"]["psk"] else "")
        return data

    def get_log(self, query, payload):
        from util import log

        return {"lines": log.history()}

    # -- writes -----------------------------------------------------------------

    def post_config(self, query, payload):
        if "wifi" in payload:
            raise ApiError(400, "WiFi credentials are set over BLE provisioning only")
        self.store.update(payload)
        self.store.save()
        self._refresh_tracker_config()
        return {"ok": True}

    def post_time(self, query, payload):
        epoch = float(_require(payload, "epoch"))
        if epoch < 1.6e9:  # before September 2020: certainly not a real clock
            raise ApiError(400, f"epoch {epoch} is not a plausible Unix time")
        if self.timesync is not None:
            self.timesync.from_phone(epoch)
        else:
            self.tracker.clock.set_epoch(epoch)
        if "tz_offset_min" in payload:
            self.store.data["site"]["tz_offset_min"] = int(payload["tz_offset_min"])
        return {"ok": True, "time": self.tracker.clock.now(), "source": "phone"}

    def post_location(self, query, payload):
        site = {"lat": float(_require(payload, "lat")), "lon": float(_require(payload, "lon"))}
        if "elev_m" in payload:
            site["elev_m"] = float(payload["elev_m"])
        self.store.update({"site": site})
        self.store.save()
        self._refresh_tracker_config()
        return {"ok": True, "site": self.store["site"]}

    def post_target(self, query, payload):
        mode = payload.get("mode", "azel")
        if mode == "azel":
            target = {"mode": "azel", "az": float(_require(payload, "az")),
                      "el": float(_require(payload, "el"))}
        elif mode == "point":
            target = {"mode": "point", "x": float(_require(payload, "x")),
                      "y": float(_require(payload, "y")), "z": float(_require(payload, "z"))}
        else:
            raise ApiError(400, "target mode must be 'azel' or 'point'")
        self.store.update({"target": target})
        self.store.save()
        self._refresh_tracker_config()
        return {"ok": True, "target": self.store["target"]}

    def post_capture(self, query, payload):
        azimuth, elevation = self.tracker.capture_target()
        self.store.update({"target": {"mode": "azel", "az": azimuth, "el": elevation}})
        self.store.save()
        self._refresh_tracker_config()
        return {"ok": True, "target": self.store["target"]}

    def post_mode(self, query, payload):
        mode = _require(payload, "mode")
        if not modes.is_valid(mode):
            raise ApiError(400, f"unknown mode: {mode}")
        self.tracker.request_mode(mode)
        return {"ok": True, "mode": self.tracker.mode, "intent": self.tracker.intent}

    def post_jog(self, query, payload):
        axis = int(_require(payload, "axis"))
        if axis not in (0, 1):
            raise ApiError(400, "axis must be 0 (azimuth) or 1 (elevation)")
        if "abs_deg" in payload:
            return self._jog(axis, abs_deg=float(payload["abs_deg"]))
        return self._jog(axis, delta_deg=float(_require(payload, "delta_deg")))

    async def _jog(self, axis, **kwargs):
        target = await self.tracker.jog(axis, **kwargs)
        return {"ok": True, "axis": axis, "target_deg": target}

    def post_clear(self, query, payload):
        self.tracker.clear_fault()
        return {"ok": True, "mode": self.tracker.mode}

    def post_wifi_forget(self, query, payload):
        """Clear the stored network; the board reboots into BLE provisioning."""
        self.store.data["wifi"]["ssid"] = ""
        self.store.data["wifi"]["psk"] = ""
        self.store.save()
        if self.wifi is not None:
            self.wifi.schedule_reboot()
        return {"ok": True, "rebooting": self.wifi is not None}

    def post_imu_level(self, query, payload):
        """Adopt the base's current attitude as level. Do this once, after install."""
        imu = self.tracker.sensors.imu
        if imu is None or not hasattr(imu, "calibrate_level"):
            raise ApiError(409, "no IMU fitted")
        level = [round(v, 5) for v in imu.calibrate_level()]
        self.store.update({"imu": {"level": level}})
        self.store.save()
        self._refresh_tracker_config()
        return {"ok": True, "level": level}

    # -- internals ----------------------------------------------------------------

    def _refresh_tracker_config(self):
        # The tracker holds a reference to the config dict; store.update() swaps
        # in a new one, so point the tracker (and its supervisor) at it.
        self.tracker.cfg = self.store.data
        self.tracker.supervisor.cfg = self.store.data["safety"]


def _decode(body):
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except ValueError:
        raise ApiError(400, "body is not valid JSON")
    if not isinstance(payload, dict):
        raise ApiError(400, "body must be a JSON object")
    return payload


def _require(payload, key):
    if key not in payload:
        raise ApiError(400, f"missing field: {key}")
    return payload[key]
