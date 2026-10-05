"""The HTTP API: handlers directly, then over a real socket through httpd.

The second half is the important one -- it runs the firmware's own server on
CPython and talks to it over TCP, which is exactly what the Android app will do.
"""

import asyncio
import json

import pytest
from config import ConfigStore
from control import modes
from control.clock import Clock
from control.tracker import Tracker
from hal import board
from net.api import Api
from net.httpd import HttpServer, parse_target
from solar.julian import unix_from_civil

NOON = unix_from_civil(2026, 6, 21, 12, 0, 0)


class FakeMonotonic:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _api(tmp_path, epoch=NOON):
    store = ConfigStore(str(tmp_path / "config.json"),
                        {"sim": True, "site": {"lat": 51.4779, "lon": -0.0015}})
    mono = FakeMonotonic()
    clock = Clock(mono, epoch=epoch)
    az, el = board.make_axes(store.data, mono)
    sensors = board.make_sensors(store.data, mono, (az, el))
    tracker = Tracker(store.data, az, el, clock, mono, sensors)
    return Api(tracker, store), tracker, store


def call(api, method, path, body=None, query=None):
    raw = json.dumps(body) if body is not None else ""
    return asyncio.run(api.handle(method, path, query or {}, raw))


# --- handlers -------------------------------------------------------------------


def test_status_reports_device_and_mode(tmp_path):
    api, _, _ = _api(tmp_path)
    status, body = call(api, "GET", "/api/status")
    assert status == 200
    assert body["mode"] == modes.IDLE
    assert body["device"]["name"] == "my_heliostat"
    assert "allowed_modes" in body


def test_unknown_route_is_404_and_wrong_method_is_405(tmp_path):
    api, _, _ = _api(tmp_path)
    assert call(api, "GET", "/api/nope")[0] == 404
    assert call(api, "GET", "/api/mode")[0] == 405


def test_time_then_track(tmp_path):
    api, tracker, _ = _api(tmp_path, epoch=None)
    status, body = call(api, "POST", "/api/mode", {"mode": "track"})
    assert status == 409 and "no valid time" in body["error"]

    assert call(api, "POST", "/api/time", {"epoch": NOON})[0] == 200
    status, body = call(api, "POST", "/api/mode", {"mode": "track"})
    assert status == 200
    assert body["mode"] == modes.TRACK


def test_implausible_time_is_rejected(tmp_path):
    api, _, _ = _api(tmp_path)
    status, body = call(api, "POST", "/api/time", {"epoch": 12345})
    assert status == 400


def test_location_persists_to_flash(tmp_path):
    api, tracker, store = _api(tmp_path)
    status, body = call(api, "POST", "/api/location", {"lat": 38.72, "lon": -9.14})
    assert status == 200
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["site"]["lat"] == 38.72
    assert tracker.cfg["site"]["lat"] == 38.72  # the running tracker sees it too


def test_bad_location_is_rejected_and_nothing_changes(tmp_path):
    api, _, store = _api(tmp_path)
    status, _ = call(api, "POST", "/api/location", {"lat": 123.0, "lon": 0.0})
    assert status == 400
    assert store["site"]["lat"] == 51.4779


def test_target_update(tmp_path):
    api, tracker, _ = _api(tmp_path)
    status, body = call(api, "POST", "/api/target", {"az": 200.0, "el": 5.0})
    assert status == 200
    assert tracker.cfg["target"]["az"] == 200.0


def test_missing_field_names_itself(tmp_path):
    api, _, _ = _api(tmp_path)
    status, body = call(api, "POST", "/api/target", {"az": 200.0})
    assert status == 400 and "el" in body["error"]


def test_malformed_json_is_400(tmp_path):
    api, _, _ = _api(tmp_path)
    status, body = asyncio.run(api.handle("POST", "/api/mode", {}, "{not json"))
    assert status == 400


def test_jog_goes_through_manual_mode(tmp_path):
    api, tracker, _ = _api(tmp_path)
    assert call(api, "POST", "/api/jog", {"axis": 0, "delta_deg": 5})[0] == 409
    call(api, "POST", "/api/mode", {"mode": "manual"})
    status, body = call(api, "POST", "/api/jog", {"axis": 0, "delta_deg": 5})
    assert status == 200
    assert body["target_deg"] == pytest.approx(185.0, abs=0.3)  # from a noisy first reading


def test_config_never_leaks_the_wifi_password(tmp_path):
    api, _, store = _api(tmp_path)
    store.data["wifi"]["psk"] = "hunter2"
    status, body = call(api, "GET", "/api/config")
    assert body["wifi"]["psk"] == "***"
    assert "hunter2" not in json.dumps(body)


def test_a_stow_pose_outside_the_axis_limits_is_refused(tmp_path):
    """Stow is the safe place; one the frame cannot reach would stall a servo."""
    api, tracker, _ = _api(tmp_path)
    status, body = call(api, "POST", "/api/config", {"safety": {"stow": {"el_mech_deg": -120.0}}})
    assert status == 400 and "stow" in body["error"]
    assert tracker.cfg["safety"]["stow"]["el_mech_deg"] != -120.0


def test_an_axis_offset_change_applies_without_a_restart(tmp_path):
    api, tracker, store = _api(tmp_path)
    axes = [dict(a) for a in store["axes"]]
    before = tracker.az.axis.offset_deg
    axes[0]["offset_deg"] = before + 4.0
    status, body = call(api, "POST", "/api/config", {"axes": axes})
    assert status == 200 and body["restart_required"] is False
    assert tracker.az.axis.offset_deg == before + 4.0


def test_a_servo_id_change_asks_for_a_restart(tmp_path):
    api, tracker, store = _api(tmp_path)
    axes = [dict(a) for a in store["axes"]]
    axes[0]["servo_id"], axes[1]["servo_id"] = axes[1]["servo_id"], axes[0]["servo_id"]
    status, body = call(api, "POST", "/api/config", {"axes": axes})
    assert status == 200 and body["restart_required"] is True


def test_imu_readings_are_averaged_and_report_a_missing_magnetometer(tmp_path):
    api, tracker, _ = _api(tmp_path)
    status, body = call(api, "GET", "/api/imu", query={"n": "3"})
    assert status == 200
    assert body["samples"] == 3
    assert body["accel_g"][2] == pytest.approx(1.0)
    assert body["mag_ut"] is None  # the simulated machine has no magnetometer

    tracker.sensors.imu = None
    status, body = call(api, "GET", "/api/imu")
    assert status == 409


def test_wifi_cannot_be_set_over_http(tmp_path):
    api, _, _ = _api(tmp_path)
    status, body = call(api, "POST", "/api/config", {"wifi": {"ssid": "x"}})
    assert status == 400


def test_sun_endpoint_matches_the_solar_module(tmp_path):
    api, _, _ = _api(tmp_path)
    status, body = call(api, "GET", "/api/sun", query={"t": str(NOON)})
    assert status == 200
    assert body["azimuth"] == pytest.approx(179.1, abs=0.5)
    assert body["elevation"] == pytest.approx(62.0, abs=0.5)


def test_software_estop_latches_and_clears(tmp_path):
    api, tracker, _ = _api(tmp_path)
    assert call(api, "POST", "/api/mode", {"mode": "estop"})[0] == 200
    assert tracker.mode == modes.ESTOP
    assert call(api, "POST", "/api/mode", {"mode": "track"})[0] == 409
    assert call(api, "POST", "/api/clear")[0] == 200
    assert tracker.mode == modes.IDLE


def test_any_request_counts_as_contact(tmp_path):
    api, tracker, _ = _api(tmp_path)
    tracker._last_contact = -10_000.0
    call(api, "GET", "/api/telemetry")
    assert tracker._last_contact == 0.0


def test_parse_target():
    assert parse_target("/api/sun?t=5&lat=1.5") == ("/api/sun", {"t": "5", "lat": "1.5"})
    assert parse_target("/api/status") == ("/api/status", {})


# --- over a real socket -----------------------------------------------------------


async def _roundtrip(server_api, raw_request):
    server = HttpServer(server_api, host="127.0.0.1", port=0)
    srv = await server.start()
    port = srv.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw_request)
    await writer.drain()
    response = await reader.read()
    writer.close()
    srv.close()
    head, _, body = response.partition(b"\r\n\r\n")
    return head.decode(), json.loads(body)


def _request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n"
    return head.encode() + data


def test_http_get_over_a_socket(tmp_path):
    api, _, _ = _api(tmp_path)
    head, body = asyncio.run(_roundtrip(api, _request("GET", "/api/status")))
    assert head.startswith("HTTP/1.1 200")
    assert "application/json" in head
    assert body["mode"] == "idle"


def test_http_post_over_a_socket(tmp_path):
    api, tracker, _ = _api(tmp_path)
    head, body = asyncio.run(_roundtrip(api, _request("POST", "/api/mode", {"mode": "track"})))
    assert head.startswith("HTTP/1.1 200")
    assert tracker.mode == modes.TRACK


def test_http_errors_are_json_with_the_right_status(tmp_path):
    api, _, _ = _api(tmp_path)
    head, body = asyncio.run(_roundtrip(api, _request("GET", "/api/missing")))
    assert head.startswith("HTTP/1.1 404")
    assert "error" in body


def test_http_rejects_an_oversized_body(tmp_path):
    api, _, _ = _api(tmp_path)
    head, body = asyncio.run(
        _roundtrip(api, _request("POST", "/api/config", {"pad": "x" * 5000}))
    )
    assert head.startswith("HTTP/1.1 413")
