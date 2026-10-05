"""Persistent configuration and calibration.

One `config.json` in flash, deep-merged over DEFAULTS on load. A firmware update
that adds a field therefore never loses an operator's stored values: defaults
fill in whatever the saved file predates, and the saved file wins wherever it
has an opinion.

NEVER WRITTEN FROM THE CONTROL LOOP. Flash survives roughly 10^5 write cycles;
a 1 Hz write would exhaust it in about a day, and the failure is unrecoverable.
Saves happen only on an explicit API call, a completed homing run, or a
provisioning commit -- `dirty` tracks whether one is needed, and nothing saves
automatically.
"""

from util.jsonio import atomic_write_json, deep_merge, read_json

CONFIG_PATH = "config.json"
SCHEMA_VERSION = 1

# Encoder span per family, mirrored from control/kinematics.FAMILY_DEGREES.
ENCODER_DEGREES = {"STS": 360.0, "SCS": 300.0}

DEFAULTS = {
    "schema": SCHEMA_VERSION,
    # With sim on, hal/board.py builds simulated servos, switches and sensors.
    "sim": False,
    # The ESP32 joins the phone's own network; BLE only hands over the credentials.
    "wifi": {"ssid": "", "psk": "", "hostname": "heliostat"},
    "ble": {"name": "my_heliostat"},
    # Firmware updates over WiFi need this token (set over USB by tools/deploy.py).
    # Empty: OTA is off.
    "ota": {"token": ""},
    # Normally-closed inputs: with nothing wired they read as OPEN, which would
    # fault the machine instantly. Enable each only once it is actually wired.
    "hardware": {"limit_switches": False, "estop": False},
    # Gravity vector of the installed, level base; set by POST /api/imu/level.
    "imu": {"level": None},
    "site": {"lat": 0.0, "lon": 0.0, "elev_m": 0.0, "tz_offset_min": 0},
    "mount": {"type": "azel"},
    "axes": [
        {
            "name": "az",
            "servo_id": 1,
            "family": "STS",
            "offset_deg": 0.0,
            "direction": 1,
            "gear_ratio": 1.0,
            # Dead zone at North: a northern-hemisphere mirror works the southern
            # sky all day. See test_the_dead_zone_must_be_sited_away_from_the_working_arc.
            "min_deg": 10.0,
            "max_deg": 350.0,
            "backlash_deg": 0.3,
            "max_speed_dps": 60.0,
            "home_mech_deg": 10.0,
            "home_dir": -1,
        },
        {
            "name": "el",
            "servo_id": 2,
            # A second ST3020: the SC09 was too weak to tilt a mirror. (An SC09
            # still works here: family "SCS", offset 150 for its 0..300 encoder.)
            "family": "STS",
            # Centres mechanical -90..+90 on the ST3020's 0..360 encoder (90..270).
            # With 0 here, half the travel would map to angles the encoder lacks.
            "offset_deg": 180.0,
            "direction": 1,
            "gear_ratio": 1.0,
            "min_deg": -90.0,
            "max_deg": 90.0,
            "backlash_deg": 0.3,
            "max_speed_dps": 30.0,
            "home_mech_deg": -90.0,
            "home_dir": -1,
        },
    ],
    "target": {"mode": "azel", "az": 180.0, "el": 10.0, "x": 0.0, "y": 0.0, "z": 0.0},
    "safety": {
        "min_sun_elev_deg": 5.0,
        # Mirror face-down: sheds rain, keeps dust off the reflective surface,
        # and cannot send a beam anywhere.
        "stow": {"az_mech_deg": 180.0, "el_mech_deg": -90.0},
        "defocus_offset_deg": 8.0,
        "time_stale_s": 86400,
        "comms_timeout_s": 900,
        "tilt_limit_deg": 5.0,
        "shock_g": 1.5,
        "pressure_drop_hpa_per_hr": 2.0,
        "wind_stow_kph": 40.0,
        "servo": {
            "max_temp_c": 65,
            # Supply window per servo family, as reported by each servo. Set to
            # match the driver board in use (bench: ST3020 12.3 V; SC09 9.9 V);
            # the point is to catch a sagging or failing rail.
            "volt_limits": {"STS": [6.0, 13.0], "SCS": [6.0, 13.0]},
            "max_load": 800,
            "max_missed_replies": 3,
        },
        "keepout": [],
    },
    "tracker": {"period_s": 1.0, "deadband_steps": 1.0},
    "log": {"level": "info"},
}


class ConfigError(ValueError):
    pass


class ConfigStore:
    """Holds the live configuration and knows when it needs saving."""

    def __init__(self, path=CONFIG_PATH, data=None):
        self.path = path
        self.data = deep_merge(DEFAULTS, data or {})
        self.dirty = False

    @classmethod
    def load(cls, path=CONFIG_PATH):
        """Load from flash, falling back to defaults if missing or corrupt.

        A corrupt file must not brick the machine. Defaults leave it reachable
        over the network, where it can be reconfigured.
        """
        saved = read_json(path, default={})
        if not isinstance(saved, dict):
            saved = {}
        store = cls(path, saved)
        validate(store.data)
        return store

    def save(self):
        """Persist to flash. Explicit only -- nothing calls this on a timer."""
        validate(self.data)
        atomic_write_json(self.path, self.data)
        self.dirty = False

    def update(self, overlay):
        """Deep-merge a partial config, validate it, and mark dirty.

        Validates BEFORE committing, so a bad request leaves the live config
        untouched rather than half-applied.
        """
        candidate = deep_merge(self.data, overlay)
        validate(candidate)
        self.data = candidate
        self.dirty = True

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)


def validate(data):
    """Reject configurations that would make the machine do something dangerous
    or nonsensical. Raises ConfigError naming the first problem found."""
    site = data["site"]
    if not -90.0 <= site["lat"] <= 90.0:
        raise ConfigError("site.lat must be within [-90, 90]")
    if not -180.0 <= site["lon"] <= 180.0:
        raise ConfigError("site.lon must be within [-180, 180]")

    axes = data["axes"]
    if len(axes) != 2:
        raise ConfigError("an az/el mount needs exactly two axes")
    ids = set()
    for axis in axes:
        if axis["family"] not in ("STS", "SCS"):
            raise ConfigError("axis {}: unknown family {}".format(axis["name"], axis["family"]))
        if axis["min_deg"] >= axis["max_deg"]:
            raise ConfigError("axis {}: min_deg must be below max_deg".format(axis["name"]))
        if axis["direction"] not in (1, -1):
            raise ConfigError("axis {}: direction must be 1 or -1".format(axis["name"]))
        if axis["gear_ratio"] <= 0:
            raise ConfigError("axis {}: gear_ratio must be positive".format(axis["name"]))
        # The whole mechanical travel must land on angles the encoder can report.
        # ST3020 reads 0..360, SC09 0..300; outside that the servo cannot go.
        span = ENCODER_DEGREES[axis["family"]]
        for mech in (axis["min_deg"], axis["max_deg"]):
            servo = axis["offset_deg"] + axis["direction"] * axis["gear_ratio"] * mech
            if not 0.0 <= servo <= span:
                raise ConfigError(
                    "axis {}: {} deg maps to servo {:.1f}, outside the encoder's "
                    "0-{:.0f}; adjust offset_deg".format(axis["name"], mech, servo, span)
                )
        if axis["servo_id"] in ids:
            # Two axes on one ID is exactly the bus collision we debugged on the
            # bench: both servos answer at once and every reply is corrupted.
            raise ConfigError("servo_id {} is used twice".format(axis["servo_id"]))
        ids.add(axis["servo_id"])

    target = data["target"]
    if target["mode"] not in ("azel", "point"):
        raise ConfigError("target.mode must be 'azel' or 'point'")
    if target["mode"] == "point" and target["x"] == target["y"] == target["z"] == 0.0:
        raise ConfigError("a point target cannot sit at the mirror's pivot")

    safety = data["safety"]
    # Stow is where the machine goes when anything is wrong, so it must be a pose
    # the frame can actually reach. (Found on the mounted structure: a face-down
    # stow the frame could not reach drove the tilt servo into it and stalled it.)
    for axis, key in zip(axes, ("az_mech_deg", "el_mech_deg")):
        stow = safety["stow"][key]
        if not axis["min_deg"] <= stow <= axis["max_deg"]:
            raise ConfigError(
                "safety.stow.{} = {} is outside axis {}'s limits [{}, {}]".format(
                    key, stow, axis["name"], axis["min_deg"], axis["max_deg"])
            )
    if safety["min_sun_elev_deg"] < 0.0:
        raise ConfigError("safety.min_sun_elev_deg must not be negative")
    if safety["defocus_offset_deg"] <= 0.0:
        raise ConfigError("safety.defocus_offset_deg must be positive")
    for zone in safety["keepout"]:
        for key in ("az_min", "az_max", "el_min", "el_max"):
            if key not in zone:
                raise ConfigError(f"keep-out zone is missing {key}")
        if zone["el_min"] > zone["el_max"]:
            raise ConfigError("keep-out zone has el_min above el_max")
