# SPDX-License-Identifier: BSD-3-Clause
"""Trips, derived from streams built through the l0 verbs (ADR-0005).

The reference vehicle's cadence: odometer and position every 15 minutes,
SoC and fuel every 2, a trip counter every 2 — a standstill is 30 minutes
of nothing moving.
"""

import json
from datetime import timedelta
from itertools import pairwise

from vledger import clock, geo, series, trips, units
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
T0 = clock.parse("2026-10-09T10:00:00Z")

# Four fixes roughly 5 km apart, then the same place as the start.
HOME = (51.0000, 7.0000)
ROAD = [(51.0300, 7.0200), (51.0600, 7.0500), (51.0900, 7.0700)]
STOP = (51.1200, 7.1000)


def at(minutes: float) -> str:
    return clock.to_text(T0 + timedelta(minutes=minutes))


class Builder:
    """Builds a stream the way the integration would, verb by verb."""

    def __init__(self, base, capsys, thresholds=None):
        self.b = ["--base", str(base), "--vehicle", "a7c1"]
        self.capsys = capsys
        self.n = 0
        snapshot = [
            {"role": "odometer", "entity": "sensor.o", "state": "1000", "unit": "km", "since": at(-120)},
            {"role": "position", "entity": "device_tracker.v", "state": "home", "since": at(-120),
             "attrs": {"latitude": HOME[0], "longitude": HOME[1], "gps_accuracy": 0}},
            {"role": "soc", "entity": "sensor.s", "state": "80", "unit": "%", "since": at(-120)},
            {"role": "fuel_level", "entity": "sensor.f", "state": "20.5", "unit": "L", "since": at(-120)},
        ]
        self.run("start", *self.b, "--t", at(-60), "--homeassistant", "2026.9.4", "--snapshot", json.dumps(snapshot))
        cfg = {"name": "V", "roles": {"odometer": {"entity": "sensor.o"}, "position": {"entity": "device_tracker.v"},
                                      "ignition": {"entity": "binary_sensor.i", "map": {"on": ["on"]}}},
               "thresholds": dict({"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360}, **(thresholds or {}))}
        self.run("config", *self.b, "--t", at(-60), "--config", json.dumps(cfg))

    def run(self, *argv):
        assert main(["l0", *argv]) == 0, argv
        self.capsys.readouterr()
        return self

    def state(self, minutes, role, value, unit=None, **attrs):
        args = ["state", *self.b, "--t", at(minutes), "--role", role, "--entity", f"x.{role}", "--state", str(value)]
        if unit:
            args += ["--unit", unit]
        for k, v in attrs.items():
            args += ["--attr", f"{k}={v}"]
        return self.run(*args)

    def fix(self, minutes, lat, lon, zone="not_home", accuracy=0):
        return self.state(minutes, "position", zone, latitude=lat, longitude=lon, gps_accuracy=accuracy)

    def heartbeat(self, minutes):
        return self.run("heartbeat", *self.b, "--t", at(minutes), "--lines", "0")

    def drive(self, start_min, odo_start=1000.0):
        """A 45-minute drive with a 8-minute stop inside it, then home."""
        m = start_min
        self.state(m, "ignition", "on")
        self.state(m + 15, "odometer", odo_start + 7, "km").fix(m + 15, *ROAD[0])
        self.state(m + 30, "odometer", odo_start + 15, "km").fix(m + 30, *ROAD[1])
        # the 8-minute stop: no sample moves between m+30 and m+45 anyway
        self.state(m + 45, "odometer", odo_start + 22, "km").fix(m + 45, *ROAD[2])
        self.state(m + 47, "ignition", "off")
        return m + 45


def test_a_drive_between_two_standstills(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    end = b.drive(60)
    b.state(end + 3, "soc", 62, "%").state(end + 3, "fuel_level", 18.9, "L")
    b.heartbeat(end + 60)
    b.state(end + 200, "odometer", 1022, "km")   # same value: no movement

    found = trips.derive_from(tmp_path, V)
    assert len(found) == 1
    t = found[0]
    assert t.start == at(60) and t.refined_by_ignition          # ignition on refined the start
    assert t.end == at(107)                                       # ignition off refined the end (ADR-0008)
    assert t.distance_km == 22 and t.distance_quality == "measured" and t.distance_source == "odometer"
    assert t.start_zone == "home" and t.end_zone == "not_home"
    assert [w["latitude"] for w in t.waypoints] == [HOME[0], *[r[0] for r in ROAD]]
    assert "accuracy_m" not in t.waypoints[0]                     # 0 is unknown (ISSUE-0004)
    assert t.delta_soc_pct == -18 and t.delta_fuel_l == -1.6
    assert t.quality == "measured"
    assert series.load(tmp_path, V).unmapped == {"ignition": {"off"}}   # met, not listed, negative


def test_two_drives_split_by_a_standstill_but_not_by_a_short_stop(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    b.heartbeat(end + 60)
    b.drive(end + 40, odo_start=1022)   # 40 min later: beyond T_still, a new trip
    found = trips.derive_from(tmp_path, V)
    assert [(t.distance_km, t.distance_source) for t in found] == [(22, "odometer"), (22, "odometer")]
    b2 = Builder(tmp_path / "short", capsys)
    end = b2.drive(60)
    # Ignition on 10 min later, the first moving sample 25 min after the
    # last: within T_still, so one trip. (20 min later would be 35 between
    # samples — the sampling, not the stop, decides; FAH-01, FAH-06.)
    b2.drive(end + 10, odo_start=1022)
    found = trips.derive_from(tmp_path / "short", V)
    assert len(found) == 1 and found[0].distance_km == 44


def test_a_gap_inside_a_trip_makes_it_incomplete_and_splits_it(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(60, "odometer", 1007, "km").fix(60, *ROAD[0])
    # the capture dies here and comes back 20 minutes later: a crash gap
    b.run("start", *b.b, "--t", at(80), "--homeassistant", "2026.9.4")
    b.state(80, "odometer", 1015, "km").fix(80, *ROAD[1])
    b.state(95, "odometer", 1022, "km").fix(95, *ROAD[2])
    found = trips.derive_from(tmp_path, V)
    # 7 km before the gap, 7 after; the 8 km driven inside the gap belong to
    # no trip, because nothing is read across a gap (ABL-04).
    assert [(t.quality, t.distance_km) for t in found] == [("incomplete", 7), ("incomplete", 7)]


def test_distance_falls_back_to_the_trip_counter_then_to_straight_lines(tmp_path, capsys):
    # No odometer at all, a trip counter that resets mid-trip, and fixes.
    b = Builder(tmp_path, capsys)
    b.state(60, "trip_distance", 3.0, "km").fix(60, *ROAD[0])
    b.state(75, "trip_distance", 11.0, "km").fix(75, *ROAD[1])
    b.state(90, "trip_distance", 0.4, "km").fix(90, *ROAD[2])   # reset: counter not monotone
    found = trips.derive_from(tmp_path, V)
    assert len(found) == 1
    t = found[0]
    assert t.distance_source == "odometer"   # the snapshot's odometer never changed...
    # ...so the odometer difference is 0 and honest; drop the odometer to see the fallbacks.
    s = series.load(tmp_path, V)
    s.series.pop("odometer")
    t = trips.derive(s)[0]
    assert t.distance_source == "waypoints" and t.distance_quality == "estimated"
    straight = sum(geo.distance_km(*a, *b_) for a, b_ in pairwise([HOME, *ROAD]))
    assert abs(t.distance_km - straight) < 0.01
    s.series["trip_distance"] = [x for x in s.series["trip_distance"] if x.value != 0.4]
    t = trips.derive(s)[0]
    assert t.distance_source == "trip_counter" and t.distance_km == 8


def test_an_odometer_in_miles_comes_out_in_km(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.state(60, "odometer", 630, "mi").state(75, "odometer", 640, "mi")
    s = series.load(tmp_path, V)
    assert [round(x.value, 1) for x in s.series["odometer"]] == [1000.0, 1013.9, 1030.0]
    assert units.convert(10, "mi", "distance") == 16.09344


def test_jitter_at_rest_is_not_a_trip(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.fix(10, HOME[0] + 0.0001, HOME[1], "home")   # 11 metres
    b.fix(25, HOME[0], HOME[1] + 0.0001, "home")
    assert trips.derive_from(tmp_path, V) == []


def test_the_verbs(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.drive(60)
    code = main(["derive", "trips", *b.b])
    out, err = capsys.readouterr()
    assert code == 0 and err.strip() == "1 trip(s)"
    trip = json.loads(out.splitlines()[0])
    assert trip["kind"] == "trip" and trip["distance_km"] == 22
    assert main(["calc", "distance", "51.0", "7.0", "51.03", "7.02"]) == 0
    assert capsys.readouterr().out.strip().endswith("km")
    assert main(["calc", "convert", "5", "gal", "--quantity", "volume"]) == 0
    assert capsys.readouterr().out.strip() == "18.9271 L"
