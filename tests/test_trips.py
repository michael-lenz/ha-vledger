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

    def __init__(self, base, capsys, thresholds=None, roles=None, parameters=None):
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
                                      "ignition": {"entity": "binary_sensor.i", "map": {"on": ["on"]}},
                                      "plug_state": {"entity": "binary_sensor.p", "map": {"plugged": ["on"]}},
                                      "charging_state": {"entity": "sensor.c", "map": {"charging": ["Charging"]}}},
               "thresholds": dict({"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360}, **(thresholds or {}))}
        cfg["roles"].update(roles or {})
        if parameters:
            cfg["parameters"] = parameters
        self.run("config", *self.b, "--t", at(-60), "--config", json.dumps(cfg))

    def run(self, *argv):
        assert main(["l0", *argv]) == 0, argv
        self.capsys.readouterr()
        return self

    def state(self, minutes, role, value, unit=None, before=None, **attrs):
        """``before``: the minute the replaced value was last reported (ADR-0011)."""
        args = ["state", *self.b, "--t", at(minutes), "--role", role, "--entity", f"x.{role}", "--state", str(value)]
        if unit:
            args += ["--unit", unit]
        if before is not None:
            args += ["--reported-before", at(before)]
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
    assert t.start == at(60) and t.end == at(107)                 # ignition on and off refined both
    assert t.refined_by == {"start": "ignition", "end": "ignition"}
    assert t.movements_while_plugged == 0
    assert t.distance_km == 22 and t.distance_quality == "measured" and t.distance_source == "odometer"
    assert t.start_zone == "home" and t.end_zone == "not_home"
    assert [w["latitude"] for w in t.waypoints] == [HOME[0], *[r[0] for r in ROAD]]
    assert "accuracy_m" not in t.waypoints[0]                     # 0 is unknown (ISSUE-0004)
    assert t.delta_soc_pct == -18 and t.delta_fuel_l == -1.6
    assert t.quality == "measured"
    assert series.load(tmp_path, V).unmapped == {"ignition": {"off"}}   # met, not listed, negative


def plain_drive(b, m, odo_start=1000.0):
    """The drive of :meth:`Builder.drive` without an ignition: the vehicles
    that have a plug state and no ignition entity."""
    b.state(m + 15, "odometer", odo_start + 7, "km").fix(m + 15, *ROAD[0])
    b.state(m + 30, "odometer", odo_start + 15, "km").fix(m + 30, *ROAD[1])
    b.state(m + 45, "odometer", odo_start + 22, "km").fix(m + 45, *ROAD[2])
    return m + 45


def test_plug_state_refines_both_ends_without_an_ignition(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(50, "plug_state", "on")                 # plugged: no marker before any drive matters
    b.state(65, "plug_state", "off")                # unplugged 10 min before the first movement
    end = plain_drive(b, 60)
    b.state(end + 4, "plug_state", "on")            # plugged in 4 min after the last
    b.heartbeat(end + 60)
    t, = trips.derive_from(tmp_path, V)
    assert (t.start, t.end) == (at(65), at(109))
    assert t.refined_by == {"start": "plug_state", "end": "plug_state"}
    assert t.distance_km == 22 and t.movements_while_plugged == 0


def test_the_earliest_end_marker_and_the_latest_start_marker_win(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(55, "plug_state", "off")                # unplugged, then the ignition: ignition is later
    end = b.drive(60)                               # ignition on at 60, off at 107
    b.state(end + 1, "plug_state", "on")            # plugged in at 106, before the ignition went off
    b.state(end + 3, "charging_state", "Charging")
    b.heartbeat(end + 60)
    t, = trips.derive_from(tmp_path, V)
    assert (t.start, t.end) == (at(60), at(106))
    assert t.refined_by == {"start": "ignition", "end": "plug_state"}


def test_charging_ends_a_trip_but_its_end_starts_none(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(10, "charging_state", "Charging").state(55, "charging_state", "Done")
    end = plain_drive(b, 60)
    b.state(end + 6, "charging_state", "Charging")
    b.heartbeat(end + 60)
    t, = trips.derive_from(tmp_path, V)
    assert t.start == at(75)                        # the full battery says nothing of leaving
    assert t.end == at(111) and t.refined_by == {"start": None, "end": "charging_state"}


def test_markers_beyond_t_still_or_through_an_outage_refine_nothing(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(30, "plug_state", "off")                # 45 min before the first movement: too early
    end = plain_drive(b, 60)
    b.state(end + 2, "plug_state", "unavailable")
    b.state(end + 3, "plug_state", "off")           # back from the outage, still unplugged: no change
    b.state(end + 40, "plug_state", "on")           # beyond T_still
    b.heartbeat(end + 60)
    t, = trips.derive_from(tmp_path, V)
    assert (t.start, t.end) == (at(75), at(105))
    assert t.refined_by == {"start": None, "end": None}


def test_a_movement_while_plugged_counts_and_is_reported(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0).heartbeat(60)
    b.state(70, "odometer", 1007, "km").fix(70, *ROAD[0])
    b.state(84, "plug_state", "on")
    # Measured before the plug-in, reported after it: sample timing.
    b.state(85, "odometer", 1015, "km").fix(85, *ROAD[1])
    b.heartbeat(150)
    t, = trips.derive_from(tmp_path, V)
    assert t.distance_km == 15 and t.end == at(85)  # nothing cut
    assert t.movements_while_plugged == 2           # the odometer and the fix
    assert t.refined_by["end"] is None              # the plug-in came before the last movement


def test_a_start_marker_never_reaches_back_across_the_previous_trip(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    end = plain_drive(b, 0)                         # last movement at 45
    b.state(end + 20, "plug_state", "on")           # ends the first trip at 65
    b.state(end + 25, "plug_state", "off")
    b.heartbeat(end + 30)
    plain_drive(b, end + 15)                        # first movement at 75
    first, second = trips.derive_from(tmp_path, V)
    assert first.end == at(65)
    assert second.start == at(70) and second.refined_by["start"] == "plug_state"
    b2 = Builder(tmp_path / "crossed", capsys)
    b2.heartbeat(0)
    end = plain_drive(b2, 0)
    b2.state(end + 10, "plug_state", "off")         # an unplug at 55, no plug-in seen before it
    b2.state(end + 25, "plug_state", "on")          # ends the first trip at 70
    b2.heartbeat(end + 30)
    plain_drive(b2, end + 15)                       # first movement at 75
    first, second = trips.derive_from(tmp_path / "crossed", V)
    assert first.end == at(70) and first.refined_by["end"] == "plug_state"
    # The unplug at 55 is within T_still of 75, but behind the first trip's end.
    assert second.start == at(75) and second.refined_by["start"] is None


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


def test_a_gap_after_the_standstill_leaves_the_trip_before_it_complete(tmp_path, capsys):
    """ISSUE-0014: a completed trip stays what it was when it completed."""
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    b.heartbeat(end + 40)                              # T_still elapsed: complete
    before = trips.derive_from(tmp_path, V, completed_only=True)
    assert [t.quality for t in before] == ["measured"]
    b.run("stop", *b.b, "--t", at(end + 50), "--reason", "reload")
    # The restart's snapshot repeats the last values, at the time they were set.
    snapshot = [
        {"role": "odometer", "entity": "x.odometer", "state": "1022.0", "unit": "km", "since": at(end)},
        {"role": "position", "entity": "x.position", "state": "not_home", "since": at(end),
         "attrs": {"latitude": ROAD[2][0], "longitude": ROAD[2][1], "gps_accuracy": 0}},
    ]
    b.run("start", *b.b, "--t", at(end + 50), "--homeassistant", "2026.9.4", "--snapshot", json.dumps(snapshot))
    b.state(end + 75, "odometer", 1029, "km")          # moved across the reload
    found = trips.derive_from(tmp_path, V)
    assert found[0] == before[0]
    assert [t.quality for t in found] == ["measured", "incomplete"]


def test_a_gap_inside_the_standstill_is_counted_when_the_trip_completes(tmp_path, capsys):
    """ISSUE-0014: the same answer before and after the next trip begins."""
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    b.run("start", *b.b, "--t", at(end + 10), "--homeassistant", "2026.9.4")   # a crash inside it
    b.heartbeat(end + 40)
    before = trips.derive_from(tmp_path, V, completed_only=True)
    assert [t.quality for t in before] == ["incomplete"]
    b.state(end + 75, "odometer", 1029, "km")
    assert trips.derive_from(tmp_path, V)[0] == before[0]


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


# --- vehicles that report their movement once per driving cycle (ADR-0021) --

CYCLE_ROLES = {
    "trip_distance": {"entity": "sensor.trip"},
    "in_use": {"entity": "sensor.connection", "map": {"in_use": ["car_in_use"]}},
    "lock": {"entity": "lock.v", "map": {"locked": ["locked"]}},
    "engine": {"entity": "binary_sensor.engine", "map": {"running": ["on"]}},
}


def test_in_use_is_movement_and_no_standstill_lies_inside_it(tmp_path, capsys):
    """Odometer and trip counter arrive only at the stop, 50 minutes after
    the departure; in use was reported at 10 and still at 40."""
    b = Builder(tmp_path, capsys, roles=CYCLE_ROLES)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4)
    b.state(10, "in_use", "car_in_use", before=-20)
    b.state(52, "trip_distance", 30.0, "km", before=50).state(52, "odometer", 1030, "km", before=50)
    b.heartbeat(55)
    b.state(70, "in_use", "available", before=40)
    b.heartbeat(110)
    [t] = trips.derive_from(tmp_path, V)
    assert (t.start, t.end) == (at(5), at(52))          # from the unlock to the cycle's upload
    assert t.refined_by == {"start": "lock", "end": None}
    assert t.distance_km == 30 and t.distance_source == "odometer"


def test_a_trip_in_use_is_not_complete(tmp_path, capsys):
    b = Builder(tmp_path, capsys, roles=CYCLE_ROLES)
    b.heartbeat(0)
    b.state(10, "in_use", "car_in_use", before=-20)
    b.heartbeat(60)                      # 50 minutes, nothing moved, still in use
    assert trips.derive_from(tmp_path, V, completed_only=True) == []
    b.state(70, "in_use", "available", before=40)
    b.heartbeat(120)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.end) == (at(10), at(40))


def test_without_in_use_the_same_drive_starts_at_its_upload(tmp_path, capsys):
    b = Builder(tmp_path, capsys, roles=CYCLE_ROLES)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4)
    b.state(52, "trip_distance", 30.0, "km", before=50).state(52, "odometer", 1030, "km", before=50)
    b.heartbeat(55)
    b.heartbeat(110)
    [t] = trips.derive_from(tmp_path, V)
    assert t.start == at(52)        # the unlock is 47 minutes before: beyond T_still


def test_lock_and_engine_only_ever_start_a_trip(tmp_path, capsys):
    b = Builder(tmp_path, capsys, roles=CYCLE_ROLES)
    b.heartbeat(0)
    b.state(55, "lock", "unlocked").state(57, "engine", "on").state(58, "lock", "locked")
    end = plain_drive(b, 60)                         # moves at 75, 90, 105
    b.state(70, "engine", "off")                     # electric from here on: not an end
    b.state(end + 2, "lock", "locked")               # nor is locking
    b.state(end + 3, "engine", "off")
    b.heartbeat(end + 60)
    [t] = trips.derive_from(tmp_path, V)
    assert t.start == at(57)                          # the latest start marker: the engine
    assert t.end == at(105) and t.refined_by == {"start": "engine", "end": None}


def test_a_slower_roles_late_report_does_not_extend_the_trip(tmp_path, capsys):
    """The trip counter is polled every 2 minutes, the odometer every 15:
    the counter's last rise at 44 ends the trip, the odometer's change seen
    at 56 — its old value last heard at 44 — is a late report of it."""
    b = Builder(tmp_path, capsys, roles={"trip_distance": {"entity": "sensor.trip"}})
    b.heartbeat(0)
    b.state(11, "odometer", 1004, "km", before=-4)
    b.state(14, "trip_distance", 4.0, "km", before=12).state(26, "odometer", 1012, "km", before=11)
    b.state(30, "trip_distance", 12.0, "km", before=28)
    b.state(44, "trip_distance", 20.0, "km", before=42)
    b.state(56, "odometer", 1020, "km", before=44)
    b.heartbeat(60).heartbeat(120)
    [t] = trips.derive_from(tmp_path, V)
    assert t.end == at(44)
    assert t.distance_km == 20 and t.distance_source == "odometer"   # the late value still counts


def test_a_slower_role_reporting_later_than_its_interval_still_ends_the_trip(tmp_path, capsys):
    b = Builder(tmp_path, capsys, roles={"trip_distance": {"entity": "sensor.trip"}})
    b.heartbeat(0)
    b.state(11, "odometer", 1004, "km", before=-4)
    b.state(14, "trip_distance", 4.0, "km", before=12).state(26, "odometer", 1012, "km", before=11)
    b.state(30, "trip_distance", 12.0, "km", before=28)
    # Its old value was still heard at 33, after the counter's last rise at 30:
    # the change follows that rise by more than its own interval.
    b.state(41, "odometer", 1020, "km", before=33)
    b.heartbeat(60).heartbeat(120)
    [t] = trips.derive_from(tmp_path, V)
    assert t.end == at(41)


def test_a_version_2_line_cannot_carry_a_role_of_version_3(tmp_path, capsys):
    path = tmp_path / "vehicle-a7c1/l0/2026-10.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"v":2,"t":"2026-10-01T06:00:00.000Z","kind":"state","subject":"a7c1",'
                    '"role":"lock","entity":"lock.v","state":"locked"}\n')
    assert main(["l0", "validate", "--base", str(tmp_path), "--vehicle", "a7c1"]) == 1
    assert "role 'lock' in a version 2 line" in capsys.readouterr().out


def test_per_cycle_trips_line_by_line_equal_one_rebuild(tmp_path, capsys):
    """In-use spans and the late-report rule read the stream around a trip,
    never beyond the previous trip's end: the live path yields what one
    rebuild does (ADR-0009, point 6)."""
    from test_fixtures import replay
    roles = dict(CYCLE_ROLES, fuel_level={"entity": "sensor.f"})
    b = Builder(tmp_path / "stream", capsys, roles=roles)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "trip_distance", 0.0, "km", before=4)
    b.state(10, "in_use", "car_in_use", before=-20)
    b.heartbeat(45)        # in use since 10 and no line since: not a completed trip
    b.state(52, "trip_distance", 30.0, "km", before=50).state(52, "fuel_level", 18.0, "L", before=50)
    b.heartbeat(55)
    b.state(63, "odometer", 1030, "km", before=52)      # the slow poll, late
    b.state(70, "in_use", "available", before=40)
    b.heartbeat(110)
    b.state(150, "engine", "on", before=148)
    b.state(165, "trip_distance", 40.0, "km", before=163).heartbeat(170)
    b.state(178, "odometer", 1040, "km", before=165)
    b.heartbeat(260)
    found = trips.derive_from(tmp_path / "stream", V)
    assert [(t.start, t.end) for t in found] == [(at(5), at(52)), (at(150), at(165))]
    replay(tmp_path / "stream", tmp_path / "replay")
