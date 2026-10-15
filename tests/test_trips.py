# SPDX-License-Identifier: BSD-3-Clause
"""Trips, derived from streams built through the l0 verbs (ADR-0005).

The reference vehicle's cadence: odometer and position every 15 minutes,
SoC and fuel every 2, a trip counter every 2 — a standstill is 30 minutes
of nothing moving.
"""

import json
from datetime import timedelta
from itertools import pairwise

from vledger import clock, export, geo, series, trips, units
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


def test_a_marker_in_the_same_poll_as_a_boundary_movement_still_bounds_the_trip(tmp_path, capsys):
    """ISSUE-0031: the ignition went off and the odometer rose in one poll,
    the ignition's line milliseconds before the odometer's — the lines of
    one poll land in an order Home Assistant chooses. The marker names
    the end; the trip's time stays the movement's, the outer line."""
    ms = 0.005 / 60                                 # five milliseconds, in minutes
    b = Builder(tmp_path, capsys)
    b.heartbeat(0).heartbeat(60)
    b.state(75, "odometer", 1007, "km").fix(75, *ROAD[0])
    b.state(75 + ms, "ignition", "on")              # the same poll as the first moving sample
    b.state(90, "odometer", 1015, "km").fix(90, *ROAD[1])
    b.state(105 - ms, "ignition", "off")            # the same poll as the last, written first
    b.state(105, "odometer", 1022, "km").fix(105, *ROAD[2])
    b.heartbeat(165)
    t, = trips.derive_from(tmp_path, V)
    assert (t.start, t.end) == (at(75), at(105))
    assert t.refined_by == {"start": "ignition", "end": "ignition"}
    # Two seconds before the last movement is inside the trip, not at its end.
    b2 = Builder(tmp_path / "inside", capsys)
    b2.heartbeat(0).heartbeat(60)
    end = plain_drive(b2, 60)
    b2.state(end - 2 / 60, "ignition", "off")
    b2.heartbeat(end + 60)
    t, = trips.derive_from(tmp_path / "inside", V)
    assert t.end == at(end) and t.refined_by["end"] is None


def test_the_minimum_move_of_a_fix_is_the_vehicles_threshold(tmp_path, capsys):
    """ADR-0030 (ISSUE-0025): a tracker that jitters by some 65 m at rest
    makes a trip at the default of 50 m, and none once the vehicle's
    threshold says 100."""
    def jitter(b):
        b.heartbeat(0).heartbeat(60)
        for i in range(1, 7):
            b.fix(60 + 5 * i, HOME[0] + (0.0006 if i % 2 else 0), HOME[1], zone="home")
        b.heartbeat(150)

    jitter(Builder(tmp_path / "default", capsys))
    assert len(trips.derive_from(tmp_path / "default", V)) == 1
    jitter(Builder(tmp_path / "tuned", capsys, thresholds={"min_move_m": 100}))
    assert trips.derive_from(tmp_path / "tuned", V) == []


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
    b.state(end + 75, "odometer", 1029, "km")          # moved after the reload
    found = trips.derive_from(tmp_path, V)
    assert found[0] == before[0]
    # The reload's snapshot held the odometer through it, so the gap is
    # bridged and the trip after it is complete too (ADR-0037).
    assert [t.quality for t in found] == ["measured", "measured"]


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


def restart(b, stop_min, start_min, odo, odo_since, where, where_since, reason="reload"):
    """Capture stopped and started again, the start's snapshot carrying the
    odometer and the position with the since of each."""
    b.run("stop", *b.b, "--t", at(stop_min), "--reason", reason)
    snapshot = [
        {"role": "odometer", "entity": "x.odometer", "state": str(odo), "unit": "km", "since": at(odo_since)},
        {"role": "position", "entity": "x.position", "state": "not_home", "since": at(where_since),
         "attrs": {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}},
    ]
    b.run("start", *b.b, "--t", at(start_min), "--homeassistant", "2026.9.4", "--snapshot", json.dumps(snapshot))


def test_a_reload_inside_the_standstill_that_the_odometer_held_through_leaves_the_trip_complete(tmp_path, capsys):
    """ADR-0037 (ISSUE-0051): the snapshot repeats the odometer with a since
    before the stop, so the gap hid no movement and the standstill runs
    through it; the trip completes measured, and stays so."""
    from test_fixtures import replay
    b = Builder(tmp_path / "stream", capsys)
    b.heartbeat(0)
    end = b.drive(60)                                   # last movement at 105
    restart(b, end + 10, end + 10, 1022.0, end, ROAD[2], end)
    b.heartbeat(end + 40)
    s = series.load(tmp_path / "stream", V)
    assert [g.reason for g in s.gaps] == ["stopped"] and trips.bridged(s) == s.gaps
    before = trips.derive_from(tmp_path / "stream", V, completed_only=True)
    assert [(t.quality, t.distance_km, t.end) for t in before] == [("measured", 22, at(107))]
    b.state(end + 75, "odometer", 1029, "km")
    found = trips.derive_from(tmp_path / "stream", V)
    assert found[0] == before[0] and found[1].quality == "measured"
    replay(tmp_path / "stream", tmp_path / "replay")


def test_a_reload_inside_a_drive_that_the_odometer_held_through_is_no_gap(tmp_path, capsys):
    """ADR-0037: a four-minute reload at a red light, the odometer unchanged
    across it, and the drive is one measured trip."""
    from test_fixtures import replay
    b = Builder(tmp_path / "stream", capsys)
    b.heartbeat(0)
    b.state(60, "ignition", "on")
    b.state(75, "odometer", 1007, "km").fix(75, *ROAD[0])
    restart(b, 80, 84, 1007.0, 75, ROAD[0], 75)
    b.state(90, "odometer", 1015, "km").fix(90, *ROAD[1])
    b.state(105, "odometer", 1022, "km").fix(105, *ROAD[2])
    b.state(107, "ignition", "off")
    b.heartbeat(150)
    [t] = trips.derive_from(tmp_path / "stream", V)
    assert (t.quality, t.distance_km, t.start, t.end) == ("measured", 22, at(60), at(107))
    replay(tmp_path / "stream", tmp_path / "replay")


def test_a_fix_that_moved_inside_the_gap_keeps_it_a_gap(tmp_path, capsys):
    """ADR-0037, point 1: the odometer held through the reload, but the
    tracker's coordinates changed inside it — a movement placed there by
    ADR-0028 — and the gap splits and marks as before."""
    b = Builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(75, "odometer", 1007, "km").fix(75, *ROAD[0])
    restart(b, 80, 84, 1007.0, 75, ROAD[1], 82)
    b.state(90, "odometer", 1015, "km").fix(90, *ROAD[1])
    b.state(105, "odometer", 1022, "km").fix(105, *ROAD[2])
    b.heartbeat(150)
    s = series.load(tmp_path, V)
    assert trips.bridged(s) == []
    found = trips.derive_from(tmp_path, V)
    assert found and all(t.quality == "incomplete" for t in found)


def test_a_per_cycle_vehicle_reads_a_bridged_gap_as_a_gap(tmp_path, capsys):
    """ADR-0037, point 4: a per-cycle vehicle's values move once per cycle,
    so a reload in a short stop still ends the trip (ADR-0024)."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 25.0, 1025)
    b.state(41, "lock", "unlocked", before=40).state(42, "lock", "locked", before=41)
    b.run("stop", *b.b, "--t", at(44), "--reason", "reload")
    snapshot = [{"role": "trip_distance", "entity": "x.trip_distance", "state": "25.0", "unit": "km", "since": at(40)},
                {"role": "odometer", "entity": "x.odometer", "state": "1025", "unit": "km", "since": at(40)},
                {"role": "lock", "entity": "x.lock", "state": "locked", "since": at(42)}]
    b.run("start", *b.b, "--t", at(44), "--homeassistant", "2026.9.4", "--snapshot", json.dumps(snapshot))
    b.state(47, "lock", "unlocked", before=46).state(48, "lock", "locked", before=47)
    b.heartbeat(55)
    upload(b, 70, 40.0, 1040)
    b.heartbeat(120)
    found = trips.derive_from(tmp_path, V, completed_only=True)
    assert [(t.start, t.end) for t in found] == [(at(10), at(40)), (at(47), at(70))]


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


# --- legs: a vehicle that reports once per driving cycle (ADR-0023) ---------

PER_CYCLE = {"movement_reporting": "per_cycle"}


def cycle_builder(path, capsys):
    return Builder(path, capsys, roles=CYCLE_ROLES, parameters=PER_CYCLE)


def upload(b, minute, km, odo):
    """The stop's upload: trip counter and odometer, polled together."""
    b.state(minute, "trip_distance", km, "km", before=minute - 2)
    b.state(minute, "odometer", odo, "km", before=minute - 2)


def test_a_drive_under_way_has_begun_at_its_departure_once_the_vehicle_runs(tmp_path, capsys):
    """ADR-0048, point 1: a departure with the engine running or the car in
    use is a drive under way; an unlock alone is a door."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4)
    b.heartbeat(8)
    assert trips.begun(series.load(tmp_path, V)) == []                 # unlocked, not running
    b.state(9, "engine", "on", before=7)
    b.heartbeat(20)
    assert trips.begun(series.load(tmp_path, V)) == [at(5)]            # running: begun at the unlock
    b.state(52, "engine", "off", before=50)
    upload(b, 52, 30.0, 1030)
    b.heartbeat(55)
    assert trips.begun(series.load(tmp_path, V)) == [at(5)]            # now a leg, not yet complete
    assert trips.derive_from(tmp_path, V, completed_only=True) == []
    b.heartbeat(90)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.end) == (at(5), at(52))
    assert trips.begun(series.load(tmp_path, V)) == [at(5)]
    # The next departure, in use this time, and nothing yet arrived.
    b.state(100, "in_use", "car_in_use", before=70)
    b.heartbeat(110)
    assert trips.begun(series.load(tmp_path, V)) == [at(5), at(100)]
    # Read from the cursor, as the live path reads it: the departure alone.
    assert trips.begun(series.load(tmp_path, V, since=at(52))) == [at(100)]


def test_being_in_use_ends_no_earlier_trip(tmp_path, capsys):
    """ADR-0048, point 3: a use that began long after the arrival is the
    next trip's, and the trip before it stays complete in a rebuild."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4)
    b.state(10, "in_use", "car_in_use", before=-20)
    upload(b, 52, 30.0, 1030)
    b.state(70, "in_use", "available", before=40)
    b.heartbeat(120)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    b.state(900, "in_use", "car_in_use", before=870)                  # 14 hours later
    b.heartbeat(910)
    assert [x.end for x in trips.derive_from(tmp_path, V, completed_only=True)] == [t.end]
    # A use that began within T_still of the arrival keeps the trip open.
    b2 = cycle_builder(tmp_path / "soon", capsys)
    b2.heartbeat(0)
    b2.state(5, "lock", "unlocked", before=4)
    upload(b2, 52, 30.0, 1030)
    b2.state(60, "in_use", "car_in_use", before=30)
    b2.heartbeat(120)
    assert trips.derive_from(tmp_path / "soon", V, completed_only=True) == []


def test_being_in_use_ends_no_earlier_sampled_trip_either(tmp_path, capsys):
    b = Builder(tmp_path, capsys, roles=CYCLE_ROLES)
    b.heartbeat(0)
    end = plain_drive(b, 0)
    b.heartbeat(end + 60)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    b.state(end + 600, "in_use", "car_in_use", before=end + 570)
    b.heartbeat(end + 610)
    assert [x.end for x in trips.derive_from(tmp_path, V, completed_only=True)] == [t.end]


def test_an_unlock_without_a_sign_of_driving_was_a_door(tmp_path, capsys):
    """ADR-0050: unlocked six minutes after the arrival to plug the car in —
    outside the exit window, inside T_still — and nothing drives: the trip
    completes T_still after that unlock, and the next day's leg starts at
    its own unlock, not at this one."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4)
    b.state(7, "lock", "locked", before=5)                   # locks itself on driving off
    b.state(40, "engine", "off", before=38)
    upload(b, 40, 25.0, 1025)
    b.state(46, "lock", "unlocked", before=45)              # 6 min after: a door, we will learn
    b.state(47, "plug_state", "on", before=45).state(47, "lock", "locked", before=46)
    b.heartbeat(70)
    assert trips.derive_from(tmp_path, V, completed_only=True) == []   # not T_still past the unlock yet
    b.heartbeat(77)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.end) == (at(5), at(40))
    b.state(600, "plug_state", "off", before=598)
    b.state(601, "lock", "unlocked", before=599).state(602, "engine", "on", before=600)
    b.state(603, "lock", "locked", before=601)
    upload(b, 640, 50.0, 1050)
    b.heartbeat(700)
    _first, second = trips.derive_from(tmp_path, V, completed_only=True)
    assert (second.start, second.refined_by["start"]) == (at(601), "lock")


def test_an_unlock_the_engine_follows_is_a_departure(tmp_path, capsys):
    """The same unlock with the engine running after it keeps the stop short:
    the legs are one trip, as ADR-0024 reads them."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4)
    b.state(7, "lock", "locked", before=5)
    b.state(40, "engine", "off", before=38)
    upload(b, 40, 25.0, 1025)
    b.state(46, "lock", "unlocked", before=45).state(48, "engine", "on", before=46)
    b.state(90, "engine", "off", before=88)
    upload(b, 90, 40.0, 1040)
    b.heartbeat(150)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.end, t.distance_km) == (at(5), at(90), 40)


def test_the_end_reading_stops_at_the_charge_that_began_at_the_arrival(tmp_path, capsys):
    """ADR-0051: the SoC after the trip is the sample before the charge, not
    the one T_settle past the odometer's late report."""
    b = Builder(tmp_path, capsys, roles=dict(CYCLE_ROLES, soc={"entity": "sensor.s"}), parameters=PER_CYCLE)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b.state(6, "trip_distance", 0.0, "km", before=4)
    b.state(-10, "soc", 90, "%", before=-12).state(20, "soc", 80, "%", before=18).state(30, "soc", 70, "%", before=28)
    b.state(40, "engine", "off", before=38).state(40, "charging_state", "Charging", before=38)
    b.state(40, "trip_distance", 20.0, "km", before=38)
    b.state(42, "soc", 72, "%", before=40).state(44, "soc", 75, "%", before=42)
    b.state(49, "odometer", 1020, "km", before=40)          # the slow poll, late
    b.state(52, "soc", 80, "%", before=50)
    b.heartbeat(100)
    [t] = trips.derive_from(tmp_path, V)
    assert (t.end, t.distance_km) == (at(40), 20)
    assert t.delta_soc_pct == -20                             # 90 → 70, not 90 → 80 at 52
    # Without a charging state the rise itself is the bound.
    b2 = Builder(tmp_path / "soc", capsys, roles=dict(CYCLE_ROLES, soc={"entity": "sensor.s"}), parameters=PER_CYCLE)
    b2.heartbeat(0)
    b2.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b2.state(6, "trip_distance", 0.0, "km", before=4)
    b2.state(-10, "soc", 90, "%", before=-12).state(30, "soc", 70, "%", before=28)
    b2.state(40, "engine", "off", before=38).state(40, "trip_distance", 20.0, "km", before=38)
    b2.state(42, "soc", 72, "%", before=40).state(44, "soc", 75, "%", before=42)
    b2.heartbeat(100)
    [t] = trips.derive_from(tmp_path / "soc", V)
    assert t.delta_soc_pct == -20


def test_the_fuel_reading_stops_at_the_fill_after_the_arrival(tmp_path, capsys):
    """ADR-0051 for the level: a pump right after the arrival is not the trip's."""
    b = Builder(tmp_path, capsys, roles=dict(CYCLE_ROLES, fuel_level={"entity": "sensor.f"}),
                parameters=dict(PER_CYCLE, fuel="petrol", fuel_level_resolution_l=1.0))
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b.state(6, "trip_distance", 0.0, "km", before=4)
    b.state(40, "engine", "off", before=38).state(40, "fuel_level", 18.0, "L", before=38)
    b.state(40, "trip_distance", 20.0, "km", before=38)
    b.state(44, "fuel_level", 30.0, "L", before=40).state(46, "fuel_level", 40.0, "L", before=44)
    b.heartbeat(100)
    [t] = trips.derive_from(tmp_path, V)
    assert t.delta_fuel_l == -2.5                             # 20.5 → 18, not 20.5 → 40


def test_a_dropout_return_measures_no_sampling_interval(tmp_path, capsys):
    """ADR-0052: the odometer that comes back from three dropouts two minutes
    later is not the fastest role; the trip counter is, and the trip ends
    at its upload, not at the odometer's late report."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    for m in (10, 20, 30):
        b.state(m, "odometer", "unavailable", before=m - 8).state(m + 2, "odometer", 1000, "km", before=m)
    b.state(16, "trip_distance", 0.0, "km", before=12).state(20, "trip_distance", 0.0, "km", before=16)
    b.state(100, "lock", "unlocked", before=99).state(101, "engine", "on", before=99).state(102, "lock", "locked", before=100)
    b.state(140, "engine", "off", before=138).state(140, "trip_distance", 25.0, "km", before=136)
    b.state(150, "odometer", 1025, "km", before=140)
    b.heartbeat(200)
    [t] = trips.derive_from(tmp_path, V)
    assert (t.start, t.end, t.distance_km) == (at(100), at(140), 25)


def test_a_fill_inside_a_trip_is_added_back_to_the_level_delta(tmp_path, capsys):
    """ADR-0053: the level rose 38.9 L over a trip that stopped at a pump for
    41.5 L; the trip burnt 2.6 L, with the level's error twice."""
    from test_refuellings import STATION, to_the_station
    b = Builder(tmp_path, capsys, roles={"fuel_level": {"entity": "sensor.f"}},
                parameters={"fuel": "petrol", "fuel_level_resolution_l": 1.0})
    b.heartbeat(0)
    to_the_station(b)                                         # odometer 1022 at 45, 18.9 L at 46
    b.state(52, "fuel_level", 40.0, "L").state(54, "fuel_level", 60.0, "L").state(58, "fuel_level", 60.4, "L")
    b.state(70, "odometer", 1030, "km").fix(70, STATION[0] + 0.05, STATION[1])
    b.state(71, "fuel_level", 59.4, "L")
    b.heartbeat(100).heartbeat(130)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert t.distance_km == 30 and t.delta_fuel_l == 38.9
    assert (t.fuel_consumed_l, t.fuel_refuelled_l, t.fuel_consumed_error_l) == (2.6, 41.5, 4.0)
    assert (t.fuel_consumed_source, t.fuel_consumed_quality) == ("fuel_level", "estimated")


def test_a_short_gap_inside_a_per_cycle_trip_leaves_it_measured(tmp_path, capsys):
    """ADR-0054: a reload of a second inside a leg hides no standstill; one
    of T_still hides one and leaves the trip incomplete. Both are counted."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b.run("stop", *b.b, "--t", at(20), "--reason", "reload")
    b.run("start", *b.b, "--t", at(20.01), "--homeassistant", "2026.9.4", "--snapshot", "[]")
    b.state(40, "engine", "off", before=38)
    upload(b, 40, 25.0, 1025)
    b.heartbeat(100)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.quality, t.gaps, t.distance_km) == ("measured", 1, 25)
    b2 = cycle_builder(tmp_path / "long", capsys)
    b2.heartbeat(0)
    b2.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b2.run("stop", *b2.b, "--t", at(20), "--reason", "reload")
    b2.run("start", *b2.b, "--t", at(55), "--homeassistant", "2026.9.4", "--snapshot", "[]")
    b2.state(60, "engine", "off", before=58)
    upload(b2, 60, 25.0, 1025)
    b2.heartbeat(120)
    [t] = trips.derive_from(tmp_path / "long", V, completed_only=True)
    assert (t.quality, t.gaps) == ("incomplete", 1)


def test_a_short_move_takes_the_counter_within_the_odometers_step(tmp_path, capsys):
    """ADR-0055: 100 m moved is 0.1 km by the counter, where the odometer's
    step is 1 km; a counter the odometer does not bound yields to it."""
    b = cycle_builder(tmp_path, capsys)
    b.state(-30, "trip_distance", 13.8, "km", before=-32)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b.state(9, "engine", "off", before=7)
    upload(b, 9, 13.9, 1001)                                  # 0.1 km by the counter, 1 by the odometer
    b.heartbeat(60)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.distance_km, t.distance_source) == (0.1, "trip_counter")
    b.state(100, "lock", "unlocked", before=99).state(101, "engine", "on", before=99).state(102, "lock", "locked", before=100)
    b.state(130, "engine", "off", before=128)
    upload(b, 130, 20.0, 1002)                                # 6.1 km by the counter, 1 by the odometer
    b.heartbeat(200)
    _first, second = trips.derive_from(tmp_path, V, completed_only=True)
    assert (second.distance_km, second.distance_source) == (1.0, "odometer")


def test_a_short_first_leg_keeps_its_unlock_and_a_plug_pulled_hours_before_departs_nothing(tmp_path, capsys):
    """ADR-0056: unlocked four minutes before a two-minute first leg's
    arrival with the engine started between — a departure, not an exit;
    and the plug pulled in the morning, which nothing followed, is no
    start for the afternoon's trip."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "plug_state", "off", before=4)                 # unplugged, then nothing for hours
    b.heartbeat(60).heartbeat(120).heartbeat(180).heartbeat(240).heartbeat(300)
    b.state(345, "trip_distance", 0.0, "km", before=343)
    b.state(346, "lock", "unlocked", before=345).state(348, "engine", "on", before=346).state(348, "lock", "locked", before=346)
    b.state(350, "engine", "off", before=348).state(350, "trip_distance", 0.9, "km", before=348)
    b.state(352, "odometer", 1001, "km", before=346)          # the six-minute poll
    b.state(354, "engine", "on", before=352)
    b.state(370, "engine", "off", before=368).state(370, "trip_distance", 17.3, "km", before=368)
    b.state(372, "odometer", 1017, "km", before=366)
    b.heartbeat(430)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.refined_by["start"], t.end, t.distance_km) == (at(346), "lock", at(370), 17.3)


def test_an_ignition_nothing_follows_for_an_hour_is_still_the_departure(tmp_path, capsys):
    """ADR-0057, point 1: an ignition that came on after an eight-minute
    stop, with no sign for 56 minutes but the arrival, departed; the legs
    are one trip of 76 km, not a 21 km trip and a leg lost to the next."""
    b = Builder(tmp_path, capsys, roles={"trip_distance": {"entity": "sensor.trip"}}, parameters=PER_CYCLE)
    b.heartbeat(0)
    b.state(5, "ignition", "on", before=3).state(5, "trip_distance", 0.0, "km", before=3)
    b.state(45, "ignition", "off", before=43).state(45, "trip_distance", 21.0, "km", before=43)
    b.state(46, "odometer", 1021, "km", before=40)
    b.state(53, "ignition", "on", before=51)
    b.state(109, "ignition", "off", before=107).state(109, "trip_distance", 75.7, "km", before=107)
    b.state(111, "ignition", "on", before=109)
    b.state(119, "odometer", 1076, "km", before=113)
    b.state(133, "ignition", "off", before=131).state(133, "trip_distance", 96.7, "km", before=131)
    b.state(135, "odometer", 1097, "km", before=129)
    b.heartbeat(200)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.end, t.distance_km) == (at(5), at(133), 96.7)


def test_the_arrival_upload_is_no_movement_while_plugged(tmp_path, capsys):
    """ISSUE-0073: plug and charging lines precede the upload's movement
    lines by milliseconds; that is the stop, not a drive while plugged."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b.state(40, "engine", "off", before=38).state(40, "plug_state", "on", before=38)
    b.state(40, "charging_state", "Charging", before=38)
    upload(b, 40, 25.0, 1025)
    b.state(49, "odometer", 1025, "km", before=40)
    b.heartbeat(100)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert t.movements_while_plugged == 0
    b2 = Builder(tmp_path / "sampled", capsys)
    b2.heartbeat(0)
    end = plain_drive(b2, 0)
    b2.state(end, "plug_state", "on")                         # the same minute as the last sample
    b2.heartbeat(end + 60)
    [t] = trips.derive_from(tmp_path / "sampled", V, completed_only=True)
    assert t.movements_while_plugged == 0


def test_legs_with_a_short_stop_are_one_trip_and_a_long_stop_splits(tmp_path, capsys):
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 25.0, 1025)
    b.state(41, "lock", "unlocked", before=40).state(42, "lock", "locked", before=41)   # getting out
    b.state(47, "lock", "unlocked", before=46).state(48, "lock", "locked", before=47)   # 7 min stop
    b.heartbeat(55)
    upload(b, 70, 40.0, 1040)
    b.heartbeat(120)
    b.state(160, "lock", "unlocked", before=159)                                        # 90 min later
    upload(b, 180, 50.0, 1050)
    b.heartbeat(230)
    first, second = trips.derive_from(tmp_path, V, completed_only=True)
    assert (first.start, first.end) == (at(10), at(70)) and first.distance_km == 40
    assert first.refined_by == {"start": "lock", "end": None}
    assert (second.start, second.end) == (at(160), at(180)) and second.distance_km == 10


def test_an_unlock_a_minute_before_an_arrival_no_fast_sample_opened_is_still_an_exit(tmp_path, capsys):
    """ISSUE-0077, the owner's morning of 2026-10-15: the previous stop's
    odometer came as a late report 46 s after its own poll, so the
    odometer ranks fastest at the next arrival's trip counter sample, and
    the counter's sample at the arrival is of the slower role. The exit
    window is still measured from that arrival: the unlock 60 s before it
    is the driver getting out, and the trip departs at the morning's
    unlock, not there."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4).state(7, "lock", "locked", before=5)
    b.state(30, "engine", "off", before=28).state(30, "trip_distance", 16.5, "km", before=28)
    b.state(30, "odometer", 1015, "km", before=20)
    b.state(32, "odometer", 1016, "km", before=31.2)          # a late report, 48 s after its poll
    b.heartbeat(60).heartbeat(120).heartbeat(180)
    b.state(200, "engine", "on", before=198).state(200, "trip_distance", 0.0, "km", before=198)
    b.state(200.5, "lock", "unlocked", before=199).state(201.5, "lock", "locked", before=200.5)
    b.state(219, "lock", "unlocked", before=218)              # the arrival's unlock, a minute early
    b.state(220, "engine", "off", before=218).state(220, "trip_distance", 15.5, "km", before=218)
    b.state(229, "odometer", 1031, "km", before=220)
    b.heartbeat(280)
    _first, t = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.refined_by["start"], t.end, t.distance_km) == (at(200.5), "lock", at(220), 15.5)


def test_the_latest_unlock_departs_not_one_to_fetch_something(tmp_path, capsys):
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(20, "lock", "unlocked", before=19).state(22, "lock", "locked", before=21)
    b.heartbeat(60)
    b.state(70, "lock", "unlocked", before=69).state(72, "lock", "locked", before=71)
    upload(b, 95, 20.0, 1020)
    b.heartbeat(150)
    [t] = trips.derive_from(tmp_path, V)
    assert t.start == at(70)


def test_without_an_unlock_the_earliest_other_marker_departs(tmp_path, capsys):
    """A reset of the trip counter comes with the departure; the engine
    starts minutes into it; in use is reported later still."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "trip_distance", 12.0, "km", before=3)
    b.heartbeat(50)
    b.state(60, "trip_distance", 0.0, "km", before=58)
    b.state(64, "engine", "on", before=62)
    b.state(70, "in_use", "car_in_use", before=40)
    upload(b, 85, 15.0, 1015)
    b.state(100, "in_use", "available", before=70)
    b.heartbeat(150)
    [t] = trips.derive_from(tmp_path, V)
    assert t.start == at(60) and t.refined_by["start"] == "trip_distance"


def test_a_slower_roles_report_before_the_next_departure_joins_the_arrival(tmp_path, capsys):
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "trip_distance", 0.0, "km", before=3)
    b.state(10, "lock", "unlocked", before=9)
    b.state(40, "trip_distance", 25.0, "km", before=38)
    b.state(52, "odometer", 1025, "km", before=40)          # the 15-minute poll, late
    b.heartbeat(100)
    [t] = trips.derive_from(tmp_path, V)
    assert (t.start, t.end) == (at(10), at(40))
    assert t.distance_km == 25 and t.distance_source == "trip_counter"      # agrees with the odometer: the finer one (ADR-0055)


def test_a_trip_is_complete_only_when_no_departure_follows_within_t_still(tmp_path, capsys):
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 25.0, 1025)
    b.heartbeat(60)
    b.state(65, "lock", "unlocked", before=64)              # off again after 25 minutes
    b.heartbeat(80)
    assert trips.derive_from(tmp_path, V, completed_only=True) == []
    upload(b, 90, 35.0, 1035)
    b.heartbeat(130)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.end) == (at(10), at(90))


def test_legs_line_by_line_equal_one_rebuild(tmp_path, capsys):
    from test_fixtures import replay
    b = cycle_builder(tmp_path / "stream", capsys)
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 25.0, 1025)
    b.state(41, "lock", "unlocked", before=40).state(42, "lock", "locked", before=41)
    b.state(47, "lock", "unlocked", before=46).state(48, "lock", "locked", before=47)
    b.heartbeat(55)
    upload(b, 70, 40.0, 1040)
    b.heartbeat(120)
    b.state(160, "engine", "on", before=158)
    upload(b, 180, 50.0, 1050)
    b.heartbeat(230)
    assert len(trips.derive_from(tmp_path / "stream", V)) == 2
    replay(tmp_path / "stream", tmp_path / "replay")


def test_a_slower_report_after_a_quick_departure_joins_the_next_arrival(tmp_path, capsys):
    """The pump (ISSUE-0036): the counter's upload at 40, the engine off
    again at 42, the odometer's slow poll bringing the pump's value at 50,
    home at 64. The odometer at 50 is no arrival of its own (ADR-0024)."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "trip_distance", 0.0, "km", before=3)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    b.state(39, "odometer", 1010, "km", before=24)                 # a slow poll before the stop
    b.state(40, "trip_distance", 25.0, "km", before=38)            # the pump
    b.state(42, "engine", "on", before=40)
    b.state(50, "odometer", 1025, "km", before=35)                 # the pump's value, late
    b.heartbeat(55)
    b.state(64, "trip_distance", 40.0, "km", before=62)            # home
    b.state(65, "odometer", 1040, "km", before=50)
    b.heartbeat(120)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.start, t.end) == (at(10), at(64))
    assert t.distance_km == 40


def test_a_slower_report_just_before_the_faster_one_joins_its_arrival(tmp_path, capsys):
    """One upload, the odometer polled 30 s before the counter: it belongs
    to the coming arrival, not to the one before the departure."""
    b = cycle_builder(tmp_path, capsys)
    b.heartbeat(0)
    b.state(5, "trip_distance", 0.0, "km", before=3)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    b.state(20, "trip_distance", 10.0, "km", before=18).state(20, "odometer", 1010, "km", before=5)
    b.heartbeat(50)
    b.state(70, "lock", "unlocked", before=69).state(72, "lock", "locked", before=71)
    # The counter's reset at the departure is measured in this trip's window,
    # so it ranks fastest before the stop's upload comes (ADR-0024, point 2).
    b.state(73, "trip_distance", 0.0, "km", before=71)
    b.state(89.5, "odometer", 1030, "km", before=74.5)
    b.state(90, "trip_distance", 20.0, "km", before=88)
    b.heartbeat(150)
    first, second = trips.derive_from(tmp_path, V, completed_only=True)
    assert (first.start, first.end, first.distance_km) == (at(10), at(20), 10)
    assert (second.start, second.end, second.distance_km) == (at(70), at(90), 20)


def test_a_late_report_after_a_written_trip_line_by_line_equals_one_rebuild(tmp_path, capsys):
    """The odometer's late report of a trip's last upload comes after the
    trip's end, which is the live path's cursor: it belongs to that trip,
    and opens nothing after it (ADR-0024)."""
    from test_fixtures import replay
    b = cycle_builder(tmp_path / "stream", capsys)
    b.heartbeat(0)
    for start in (10, 200):
        b.state(start, "lock", "unlocked", before=start - 1)
        b.state(start + 1, "trip_distance", 0.0, "km", before=start - 1)
        b.state(start + 2, "lock", "locked", before=start + 1)
        b.heartbeat(start + 20)
        b.state(start + 30, "trip_distance", 20.0, "km", before=start + 28)
        b.state(start + 42, "odometer", 1020 if start == 10 else 1040, "km", before=start + 27)
        b.heartbeat(start + 80).heartbeat(start + 140)
    found = trips.derive_from(tmp_path / "stream", V)
    assert [(t.start, t.end, t.distance_km) for t in found] == [(at(10), at(40), 20), (at(200), at(230), 20)]
    replay(tmp_path / "stream", tmp_path / "replay")


# --- a trip's consumption (ADR-0025) -----------------------------------------

def _consumed(t):
    return {k: v for k, v in trips.to_dict(t).items()
            if k.startswith(("fuel_consumed", "fuel_l_per", "battery_consumed", "battery_kwh_per"))}


def test_from_the_level_a_rate_is_shown_only_when_the_fuel_exceeds_the_sensors_error(tmp_path, capsys):
    """The drive of test_a_drive_between_two_standstills: 22 km, 1.6 L and
    18 % of a 10 kWh battery, read with the level good to 0.2 L."""
    b = Builder(tmp_path, capsys, parameters={"fuel": "petrol", "fuel_level_resolution_l": 0.2,
                                               "battery_net_kwh": 10})
    b.heartbeat(0)
    end = b.drive(60)
    b.state(end + 3, "soc", 62, "%").state(end + 3, "fuel_level", 18.9, "L")
    b.heartbeat(end + 60).state(end + 200, "odometer", 1022, "km")
    [t] = trips.derive_from(tmp_path, V)
    assert _consumed(t) == {
        "fuel_consumed_l": 1.6, "fuel_consumed_quality": "estimated",
        "fuel_consumed_source": "fuel_level", "fuel_consumed_error_l": 0.4,
        "fuel_l_per_100km": 7.273, "fuel_l_per_100km_quality": "estimated",
        "fuel_l_per_100km_error_pct": 25.0,
        "battery_consumed_kwh": 1.8, "battery_consumed_quality": "estimated",
        "battery_consumed_error_kwh": 0.2,
        "battery_kwh_per_100km": 8.182, "battery_kwh_per_100km_quality": "estimated",
        "battery_kwh_per_100km_error_pct": 11.1,
    }
    # The keys ride on the trip event, so the verb and the CSV carry them.
    assert set(_consumed(t)) <= set(export.columns("trip", []))


def test_a_level_whose_error_swallows_the_fuel_gives_the_quantity_and_no_rate(tmp_path, capsys):
    b = Builder(tmp_path, capsys, parameters={"fuel": "petrol", "fuel_level_resolution_l": 1.0})
    b.heartbeat(0)
    end = b.drive(60)
    b.state(end + 3, "fuel_level", 18.9, "L")
    b.heartbeat(end + 60).state(end + 200, "odometer", 1022, "km")
    [t] = trips.derive_from(tmp_path, V)
    c = _consumed(t)
    assert (c["fuel_consumed_l"], c["fuel_consumed_error_l"]) == (1.6, 2.0)   # 1.6 L, give or take 2
    assert c["fuel_l_per_100km"] is None and c["fuel_l_per_100km_error_pct"] is None
    assert c["fuel_l_per_100km_quality"] is None
    assert c["battery_consumed_kwh"] is None                                    # no capacity


def test_without_the_levels_resolution_the_level_gives_no_fuel_figure(tmp_path, capsys):
    b = Builder(tmp_path, capsys, parameters={"fuel": "petrol"})
    b.heartbeat(0)
    end = b.drive(60)
    b.state(end + 3, "fuel_level", 18.9, "L")
    b.heartbeat(end + 60).state(end + 200, "odometer", 1022, "km")
    [t] = trips.derive_from(tmp_path, V)
    assert t.delta_fuel_l == -1.6                                              # FAH-05 stays
    assert t.fuel_consumed_l is None and t.fuel_consumed_source is None


def test_the_trip_computer_measures_the_fuel_and_a_reset_zeroes_the_start(tmp_path, capsys):
    """A per-cycle vehicle whose trip counter reports its average since the
    reset: 100 km at 6.0 then 125 km at 6.2 is 1.75 L for a 25 km leg; after
    the counter's reset at a departure, 10 km at 8.0 is the whole 0.8 L."""
    roles = dict(CYCLE_ROLES, trip_consumption={"entity": "sensor.avg"})
    b = Builder(tmp_path, capsys, roles=roles, parameters=dict(
        PER_CYCLE, fuel="petrol", fuel_level_resolution_l=1.0))
    b.state(-30, "trip_distance", 100.0, "km").state(-30, "trip_consumption", 6.0, "L/100 km")
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 125.0, 1025)
    b.state(40, "trip_consumption", 6.2, "L/100 km", before=38)
    b.state(43, "fuel_level", 17.0, "L", before=38)      # the level says 3.5 L: the computer wins
    b.heartbeat(100)
    b.state(160, "lock", "unlocked", before=159)
    upload(b, 180, 10.0, 1035)                            # the counter went down: reset at departure
    b.state(180, "trip_consumption", 8.0, "L/100 km", before=178)
    b.heartbeat(230)
    first, second = trips.derive_from(tmp_path, V, completed_only=True)
    assert first.distance_km == 25 and first.delta_fuel_l == -3.5
    assert _consumed(first) == {
        "fuel_consumed_l": 1.75, "fuel_consumed_quality": "measured",
        "fuel_consumed_source": "trip_computer", "fuel_consumed_error_l": 0.113,
        "fuel_l_per_100km": 7.0, "fuel_l_per_100km_quality": "measured",
        "fuel_l_per_100km_error_pct": 6.4,
        "battery_consumed_kwh": None, "battery_consumed_quality": None,
        "battery_consumed_error_kwh": None, "battery_kwh_per_100km": None,
        "battery_kwh_per_100km_quality": None, "battery_kwh_per_100km_error_pct": None,
    }
    assert (second.fuel_consumed_l, second.fuel_consumed_error_l) == (0.8, 0.005)
    assert (second.fuel_l_per_100km, second.fuel_l_per_100km_error_pct) == (8.0, 0.6)


def test_an_unchanged_average_is_a_reading(tmp_path, capsys):
    """ADR-0049, point 3: a 100 m move at an average that did not change
    writes no average line (ADR-0011); the figure is still the computer's."""
    roles = dict(CYCLE_ROLES, trip_consumption={"entity": "sensor.avg"})
    b = Builder(tmp_path, capsys, roles=roles, parameters=dict(
        PER_CYCLE, fuel="petrol", fuel_level_resolution_l=1.0))
    b.state(-30, "trip_distance", 13.8, "km").state(-30, "trip_consumption", 0.0, "L/100 km")
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 14, 13.9, 1001)                              # the counter moved, the average did not
    b.state(16, "fuel_level", 19.5, "L", before=12)        # the level says 1 L
    b.heartbeat(60)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert t.delta_fuel_l == -1.0
    assert (t.fuel_consumed_l, t.fuel_consumed_source, t.fuel_consumed_quality) == (0.0, "trip_computer", "measured")


def test_an_electric_leg_from_the_trip_computer_is_zero_not_unknown(tmp_path, capsys):
    """ISSUE-0043's leg: after the counter's reset at the departure, 10 km at
    an average of 0.0 is no fuel, give or take 0.005 L. That error over the
    distance is half a step, so the rate is shown, as zero (ADR-0026,
    point 1); there is no share of nothing."""
    roles = dict(CYCLE_ROLES, trip_consumption={"entity": "sensor.avg"})
    b = Builder(tmp_path, capsys, roles=roles, parameters=dict(PER_CYCLE, fuel="petrol"))
    b.state(-30, "trip_distance", 54.7, "km").state(-30, "trip_consumption", 7.6, "L/100 km")
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 10.0, 1010)                             # the counter went down: reset at departure
    b.state(40, "trip_consumption", 0.0, "L/100 km", before=38)
    b.heartbeat(100)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert t.distance_km == 10
    assert {k: v for k, v in _consumed(t).items() if k.startswith("fuel")} == {
        "fuel_consumed_l": 0.0, "fuel_consumed_quality": "measured",
        "fuel_consumed_source": "trip_computer", "fuel_consumed_error_l": 0.005,
        "fuel_l_per_100km": 0.0, "fuel_l_per_100km_quality": "measured",
        "fuel_l_per_100km_error_pct": None,
    }


def test_the_same_leg_from_the_level_alone_stays_unknown(tmp_path, capsys):
    """Without the trip computer the leg is 0.1 L, give or take 2: over 10 km
    that error is 20 L/100 km, two hundred steps, and the sign is not
    certain either (ADR-0026, What it costs)."""
    b = Builder(tmp_path, capsys, roles=CYCLE_ROLES, parameters=dict(
        PER_CYCLE, fuel="petrol", fuel_level_resolution_l=1.0))
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 10.0, 1010)
    b.state(40, "fuel_level", 20.4, "L", before=38)
    b.heartbeat(100)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert (t.fuel_consumed_l, t.fuel_consumed_error_l, t.fuel_l_per_100km) == (0.1, 2.0, None)


def test_a_charge_held_is_zero_battery_energy_only_when_read_finely_enough(tmp_path, capsys):
    """22 km on the engine with the charge held at 80 %. Read to a tenth of a
    percent, 0 kWh give or take 0.02 is within one step over the distance:
    zero, and a positive one. Read in whole percent, give or take 0.2, it is
    ten steps and stays unknown (ADR-0026, point 1)."""
    for resolution, rate in ((0.1, 0.0), (1, None)):
        path = tmp_path / str(resolution)
        b = Builder(path, capsys, parameters={"battery_net_kwh": 10, "soc_resolution_pct": resolution})
        b.heartbeat(0)
        end = b.drive(60)
        b.state(end + 3, "soc", 80, "%")
        b.heartbeat(end + 60).state(end + 200, "odometer", 1022, "km")
        [t] = trips.derive_from(path, V)
        assert (t.battery_consumed_kwh, t.battery_kwh_per_100km) == (0.0, rate)
        assert str(t.battery_consumed_kwh) == "0.0"                      # not -0.0
        assert rate is None or str(t.battery_kwh_per_100km) == "0.0"


def test_a_trip_across_a_gap_is_incomplete_in_its_consumption_too(tmp_path, capsys):
    b = Builder(tmp_path, capsys, parameters={"fuel": "petrol", "fuel_level_resolution_l": 0.2})
    b.heartbeat(0)
    m = 60
    b.state(m + 15, "odometer", 1007, "km").fix(m + 15, *ROAD[0])
    b.run("stop", *b.b, "--t", at(m + 20), "--reason", "shutdown")
    b.run("start", *b.b, "--t", at(m + 25), "--homeassistant", "2026.9.4")
    b.state(m + 45, "odometer", 1022, "km").fix(m + 45, *ROAD[2])
    b.state(m + 48, "fuel_level", 18.9, "L")
    b.heartbeat(m + 120).state(m + 200, "odometer", 1022, "km")
    found = trips.derive_from(tmp_path, V)
    after = [t for t in found if t.start == at(m + 45)]
    assert after and after[0].quality == "incomplete"
    assert after[0].fuel_consumed_quality == "incomplete"
    assert after[0].fuel_l_per_100km_quality in (None, "incomplete")


# --- a trip's cost, at read time (ADR-0043) ----------------------------------

PHEV = {"fuel": "petrol", "battery_net_kwh": 18.8, "charging_loss_factor": 1.1}


def _trip_line(**kw) -> dict:
    line = {"start": "2026-10-09T10:00:00.000Z",
            "fuel_consumed_l": 2.0, "fuel_consumed_quality": "measured", "fuel_l_per_100km": 5.0,
            "battery_consumed_kwh": 4.0, "battery_consumed_quality": "estimated",
            "battery_kwh_per_100km": 10.0}
    line.update(kw)
    return line


def _receipted(start: str, unit: float) -> dict:
    return {"start": start, "confirmation": "receipt", "unit_price": unit}


def _session(start: str, cost: float, kwh: float, quality: str = "receipt") -> dict:
    return {"start": start, "cost_eur": cost, "cost_quality": quality, "grid_kwh": kwh}


def test_a_trip_is_priced_at_the_last_prices_known_at_its_start():
    refuellings = [_receipted("2026-10-01T08:00:00.000Z", 1.80),
                   _receipted("2026-10-08T08:00:00.000Z", 1.90),
                   _receipted("2026-10-09T12:00:00.000Z", 2.50),           # after the start
                   {"start": "2026-10-09T09:00:00.000Z", "price_suggestion": 1.0}]  # no receipt
    sessions = [_session("2026-10-05T20:00:00.000Z", 3.0, 10.0),
                _session("2026-10-07T20:00:00.000Z", 0.0, 0.0),            # no energy: not a price
                _session("2026-10-10T20:00:00.000Z", 9.0, 10.0)]
    c = trips.cost(_trip_line(), refuellings, sessions, PHEV)
    assert c["fuel_cost_eur"] == 3.8 and c["fuel_cost_eur_quality"] == "measured"
    # 4 kWh from the battery are 4.4 kWh from the grid, at 0.30 per kWh.
    assert c["electricity_cost_eur"] == 1.32 and c["electricity_cost_eur_quality"] == "estimated"
    assert c["cost_eur"] == 5.12 and c["cost_eur_quality"] == "estimated"


def test_a_late_receipt_changes_the_cost_when_it_is_next_read():
    line = _trip_line(battery_consumed_kwh=0.0, battery_kwh_per_100km=0.0)
    assert trips.cost(line, [], [], PHEV)["cost_eur"] is None
    later = trips.cost(line, [_receipted("2026-10-08T08:00:00.000Z", 1.90)], [], PHEV)
    assert later["cost_eur"] == 3.8 and later["electricity_cost_eur"] == 0.0


def test_an_unshown_consumption_or_a_missing_price_leaves_the_cost_unknown():
    prices = [_receipted("2026-10-08T08:00:00.000Z", 1.90)], [_session("2026-10-05T20:00:00.000Z", 3.0, 10.0)]
    c = trips.cost(_trip_line(fuel_l_per_100km=None), *prices, PHEV)
    assert c["fuel_cost_eur"] is None and c["fuel_cost_eur_quality"] == "incomplete"
    assert c["electricity_cost_eur"] == 1.32
    assert c["cost_eur"] is None and c["cost_eur_quality"] == "incomplete"
    c = trips.cost(_trip_line(), prices[0], [], PHEV)
    assert c["electricity_cost_eur"] is None and c["cost_eur"] is None


def test_a_quantity_within_its_error_of_zero_costs_zero_at_any_price():
    """ADR-0059, the owner's 4.3 km trip: -2 % SoC is exactly the battery's
    error and the trip computer's 0.000 L has an error of 0.019 L over too
    short a distance for a rate; each costs 0.00, estimated, and so does
    the sum, with no price known at all. A fuel figure beyond its error
    with no rate — no distance — stays unknown."""
    line = _trip_line(fuel_consumed_l=0.0, fuel_consumed_error_l=0.019, fuel_l_per_100km=None,
                      battery_consumed_kwh=0.376, battery_consumed_error_kwh=0.376,
                      battery_kwh_per_100km=None)
    c = trips.cost(line, [], [], PHEV)
    assert (c["fuel_cost_eur"], c["fuel_cost_eur_quality"]) == (0.0, "measured")
    assert (c["electricity_cost_eur"], c["electricity_cost_eur_quality"]) == (0.0, "estimated")
    assert (c["cost_eur"], c["cost_eur_quality"]) == (0.0, "estimated")
    c = trips.cost(_trip_line(fuel_consumed_l=-0.01, fuel_consumed_error_l=0.019, fuel_l_per_100km=None),
                   [], [_session("2026-10-05T20:00:00.000Z", 3.0, 10.0)], PHEV)
    assert (c["fuel_cost_eur"], c["fuel_cost_eur_quality"]) == (0.0, "estimated")
    assert c["cost_eur"] == 1.32
    c = trips.cost(_trip_line(fuel_consumed_error_l=0.019, fuel_l_per_100km=None), [], [], PHEV)
    assert c["fuel_cost_eur"] is None and c["cost_eur"] is None


def test_an_energy_the_vehicle_does_not_use_stays_out_of_the_sum():
    line = _trip_line(battery_consumed_kwh=None, battery_kwh_per_100km=None)
    c = trips.cost(line, [_receipted("2026-10-08T08:00:00.000Z", 1.90)], [], {"fuel": "diesel"})
    assert c["electricity_cost_eur"] is None and c["electricity_cost_eur_quality"] is None
    assert c["cost_eur"] == 3.8 and c["cost_eur_quality"] == "measured"
    c = trips.cost(line, [], [], {})
    assert c == dict.fromkeys(("fuel_cost_eur", "fuel_cost_eur_quality", "electricity_cost_eur",
                               "electricity_cost_eur_quality", "cost_eur", "cost_eur_quality"))


def test_a_battery_that_gained_is_a_negative_electricity_cost():
    line = _trip_line(battery_consumed_kwh=-2.0, battery_kwh_per_100km=-5.0)
    c = trips.cost(line, [_receipted("2026-10-08T08:00:00.000Z", 1.90)],
                   [_session("2026-10-05T20:00:00.000Z", 3.0, 10.0)], PHEV)
    assert c["electricity_cost_eur"] == -0.66 and c["cost_eur"] == 3.14
