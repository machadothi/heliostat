#!/usr/bin/env python3
"""Generate tools/reference/noaa_cases.json from pvlib. Run once; commit the result.

Freezing the reference into the repo means tests/test_sunpos.py needs neither a
network connection nor pvlib at run time, and the expected values cannot drift
underneath us when pvlib is upgraded.

    pip install -e '.[reference]'
    python3 tools/gen_reference.py

pvlib's `nrel_numpy` method is the NREL SPA, accurate to about 0.0003 degrees.
Our firmware implements the NOAA/Meeus algorithm at about 0.01 degrees, so any
disagreement beyond that is our bug, not a difference of opinion.

Cases are chosen to hit the places this kind of code breaks: the solstices and
equinoxes, both twilights, the horizon, a leap day, the extremes of latitude, both
sides of the prime meridian and the date line, and an azimuth crossing 0/360.
"""

import json
from pathlib import Path

import pandas as pd
import pvlib

OUT = Path(__file__).resolve().parent / "reference" / "noaa_cases.json"

# (label, ISO UTC timestamp, latitude, longitude, elevation in metres)
CASES = [
    # --- solstices and equinoxes, mid-latitude northern ---
    ("mar_equinox_noon", "2024-03-20T12:00:00", 51.4779, -0.0015, 47),
    ("jun_solstice_noon", "2024-06-20T12:00:00", 51.4779, -0.0015, 47),
    ("sep_equinox_noon", "2024-09-22T12:00:00", 51.4779, -0.0015, 47),
    ("dec_solstice_noon", "2024-12-21T12:00:00", 51.4779, -0.0015, 47),
    # --- times of day: dawn, morning, noon, afternoon, dusk ---
    ("summer_dawn", "2024-06-21T04:00:00", 51.4779, -0.0015, 47),
    ("summer_morning", "2024-06-21T08:00:00", 51.4779, -0.0015, 47),
    ("summer_afternoon", "2024-06-21T16:00:00", 51.4779, -0.0015, 47),
    ("summer_dusk", "2024-06-21T20:00:00", 51.4779, -0.0015, 47),
    # --- near the horizon, where refraction dominates ---
    ("near_horizon_rise", "2024-03-20T06:05:00", 51.4779, -0.0015, 47),
    ("near_horizon_set", "2024-03-20T18:10:00", 51.4779, -0.0015, 47),
    # --- leap day ---
    ("leap_day", "2024-02-29T12:00:00", 51.4779, -0.0015, 47),
    # --- latitude extremes ---
    ("equator_noon", "2024-06-21T12:00:00", 0.0, 0.0, 0),
    ("tropic_cancer", "2024-06-21T12:00:00", 23.44, 0.0, 0),
    ("tropic_capricorn", "2024-12-21T12:00:00", -23.44, 0.0, 0),
    ("high_north", "2024-06-21T12:00:00", 60.0, 10.0, 0),
    ("high_south", "2024-12-21T12:00:00", -60.0, 10.0, 0),
    ("arctic_midnight_sun", "2024-06-21T00:00:00", 70.0, 20.0, 0),
    # --- longitude: both sides of the prime meridian and the date line ---
    ("west_of_greenwich", "2024-06-21T18:00:00", 40.0, -105.0, 1600),
    ("east_of_greenwich", "2024-06-21T04:00:00", 35.0, 139.0, 40),
    ("near_dateline_east", "2024-06-21T00:00:00", -17.0, 179.0, 0),
    ("near_dateline_west", "2024-06-21T00:00:00", -17.0, -179.0, 0),
    # --- southern hemisphere, where azimuth runs through North ---
    ("southern_summer_noon", "2024-12-21T15:00:00", -23.55, -46.63, 760),
    ("southern_morning", "2024-12-21T10:00:00", -23.55, -46.63, 760),
    # --- azimuth crossing the 0/360 seam ---
    ("azimuth_seam_north", "2024-06-21T12:00:00", -35.0, 0.0, 0),
    # --- a year of noons, to catch any seasonal term ---
    *[
        (f"monthly_{month:02d}", f"2026-{month:02d}-15T12:00:00", 51.4779, -0.0015, 47)
        for month in range(1, 13)
    ],
]


def main() -> int:
    records = []
    for label, iso, lat, lon, elev in CASES:
        times = pd.DatetimeIndex([pd.Timestamp(iso, tz="UTC")])
        sp = pvlib.solarposition.get_solarposition(
            times, lat, lon, altitude=elev, method="nrel_numpy"
        )
        row = sp.iloc[0]
        records.append(
            {
                "label": label,
                "utc": iso,
                "unix": int(times[0].timestamp()),
                "lat": lat,
                "lon": lon,
                "elev_m": elev,
                "azimuth": round(float(row["azimuth"]), 6),
                "elevation": round(float(row["elevation"]), 6),
                "apparent_elevation": round(float(row["apparent_elevation"]), 6),
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "source": f"pvlib {pvlib.__version__}, method=nrel_numpy (NREL SPA)",
                "note": "Reference for tests/test_sunpos.py. "
                        "Regenerate with tools/gen_reference.py.",
                "cases": records,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {len(records)} cases to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
