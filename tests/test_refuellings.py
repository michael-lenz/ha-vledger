# SPDX-License-Identifier: BSD-3-Clause
"""Refuelling candidates, derived from streams built through the l0 verbs
(ADR-0005): a fuel rise of at least the threshold at unchanged odometer,
its level after read once T_settle (6 min here) has elapsed."""

import json
import shutil

from test_trips import ROAD, Builder, at
from vledger import l1, refuellings, series
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
STATION = (51.1000, 7.0800)
FUEL = {"fuel_level": {"entity": "sensor.f"}}


def builder(tmp_path, capsys, roles=None, parameters=None):
    return Builder(tmp_path, capsys, roles=dict(FUEL, **(roles or {})), parameters=parameters)


def to_the_station(b):
    """Drive 22 km and stop at a fuel station: the odometer stands from +45."""
    b.state(15, "odometer", 1007, "km").fix(15, *ROAD[0])
    b.state(30, "odometer", 1015, "km").fix(30, *ROAD[1])
    b.state(45, "odometer", 1022, "km").fix(45, *STATION, zone="fuel_station")
    b.state(46, "fuel_level", 18.9, "L")


def test_a_refuelling_at_a_station(tmp_path, capsys):
    b = builder(tmp_path, capsys, roles={"fuel_price": {"entity": "sensor.price"}})
    b.state(40, "fuel_price", "1.799", "EUR/L")
    to_the_station(b)
    # The sensor sees the pump in two steps, then settles a little higher.
    b.state(52, "fuel_level", 40.0, "L").state(54, "fuel_level", 60.0, "L")
    b.state(58, "fuel_level", 60.4, "L")
    b.heartbeat(59)
    assert refuellings.derive_from(tmp_path, V, completed_only=True) == []   # settles at 54 + 6 = 60
    b.heartbeat(70)
    [r] = refuellings.derive_from(tmp_path, V, completed_only=True)
    assert (r.start, r.end) == (at(52), at(54))                  # two steps, one refuelling
    assert r.quality == "measured"
    assert r.level_before_l == 18.9 and r.level_after_l == 60.4 and r.settled_at == at(58)
    assert r.sensor_delta_l == 41.5 and r.sensor_delta_quality == "estimated"
    assert r.zone == "fuel_station" and r.position["latitude"] == STATION[0]
    assert r.price_suggestion == 1.799


def test_a_partial_fill_counts_and_a_rise_below_the_threshold_does_not(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    to_the_station(b)
    b.state(50, "fuel_level", 20.9, "L")               # +2 L: below 3 L, sensor noise
    b.state(60, "fuel_level", 30.9, "L")               # +10 L: a partial fill
    b.heartbeat(80)
    [r] = refuellings.derive_from(tmp_path, V)
    assert r.start == at(60) and r.level_before_l == 20.9 and r.sensor_delta_l == 10
    assert r.price_suggestion is None                   # no fuel_price role


def test_a_rise_while_driving_is_not_a_candidate(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    b.state(10, "fuel_level", 20.0, "L")
    b.state(15, "odometer", 1007, "km").state(16, "fuel_level", 26.0, "L")   # sloshing, moving
    b.heartbeat(60)
    assert refuellings.derive_from(tmp_path, V) == []


def test_without_an_odometer_any_other_movement_counts(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    b.state(10, "fuel_level", 20.0, "L")
    b.fix(16, *ROAD[0]).state(16, "fuel_level", 26.0, "L")
    b.heartbeat(60)
    s = series.load(tmp_path, V)
    assert len(refuellings.derive(s)) == 1               # the odometer stood: TNK-01 is met
    s.series.pop("odometer")
    assert refuellings.derive(s) == []                   # without one, the fix that moved says driving


def test_fuel_in_percent_is_litres_of_the_tank_capacity(tmp_path, capsys):
    b = builder(tmp_path, capsys, parameters={"tank_capacity_l": 50})
    b.state(10, "fuel_level", 30, "%").state(20, "fuel_level", 90, "%")
    b.heartbeat(60)
    [r] = refuellings.derive_from(tmp_path, V)
    assert (r.level_before_l, r.level_after_l, r.sensor_delta_l) == (15, 45, 30)


def test_fuel_in_percent_without_a_tank_capacity_detects_nothing_and_says_why(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    b.state(10, "fuel_level", 30, "%").state(20, "fuel_level", 90, "%")
    b.heartbeat(60)
    s = series.load(tmp_path, V)
    assert "fuel_level" not in s.series or all(x.t == at(-120) for x in s.series["fuel_level"])
    assert refuellings.derive(s) == []
    assert main(["derive", "refuellings", *b.b]) == 0
    err = capsys.readouterr().err
    assert "no refuelling detection: fuel_level reports % and tank_capacity_l is not set" in err


def test_a_dropout_during_the_settle_time(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    to_the_station(b)
    b.state(50, "fuel_level", 60.0, "L")
    b.state(52, "fuel_level", "unavailable")            # the sensor drops out while settling...
    b.heartbeat(57)                                     # ...past the due time, 56
    [r] = refuellings.derive_from(tmp_path, V)
    assert r.level_after_l is None                      # nothing to read yet: a dropout says nothing
    assert refuellings.derive_from(tmp_path, V, completed_only=True) == []
    b.state(59, "fuel_level", 60.3, "L")                # back: the first value after it is the reading
    [r] = refuellings.derive_from(tmp_path, V, completed_only=True)
    assert r.level_after_l == 60.3 and r.settled_at == at(59) and r.quality == "measured"


def test_a_capture_gap_during_the_settle_time_leaves_it_incomplete(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    to_the_station(b)
    b.state(50, "fuel_level", 60.0, "L")
    b.run("start", *b.b, "--t", at(70), "--homeassistant", "2026.9.4")   # a crash in between
    b.state(71, "fuel_level", 60.4, "L")
    [r] = refuellings.derive_from(tmp_path, V, completed_only=True)
    assert r.quality == "incomplete" and r.level_after_l is None and r.sensor_delta_l is None


def test_l1_holds_settled_candidates_and_line_by_line_equals_one_rebuild(tmp_path, capsys):
    live = tmp_path / "live"
    b = builder(live, capsys)
    to_the_station(b)
    b.state(50, "fuel_level", 60.0, "L").state(53, "fuel_level", 60.2, "L")
    b.heartbeat(60)
    b.state(120, "fuel_level", 50.0, "L").state(130, "fuel_level", 65.0, "L")   # a second, later top-up
    b.heartbeat(140)

    replay = tmp_path / "replay"
    src = live / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst = replay / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst.parent.mkdir(parents=True)
    with open(dst, "a") as f:
        for line in src.read_text().splitlines(keepends=True):
            f.write(line)
            f.flush()
            l1.incremental(replay, V)

    manifest = l1.rebuild(live, V)
    batch = (l1.l1_dir(live, V) / "refuellings.jsonl").read_bytes()
    assert batch == (l1.l1_dir(replay, V) / "refuellings.jsonl").read_bytes()
    events = list(l1.read(live, V, "refuelling"))
    assert [(e["start"], e["level_after_l"]) for e in events] == [(at(50), 60.2), (at(130), 65.0)]
    assert manifest["through"]["refuelling"] == at(130)


def test_the_verbs(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    to_the_station(b)
    b.state(50, "fuel_level", 60.0, "L")
    code = main(["derive", "refuellings", *b.b])
    out, err = capsys.readouterr()
    assert code == 0 and err.strip() == "1 refuelling candidate(s)"
    assert json.loads(out)["level_after_l"] is None     # still settling: printed, not written
    assert main(["derive", "refuellings", *b.b, "--write"]) == 0
    assert "0 completed refuelling(s)" in capsys.readouterr().out
    b.heartbeat(60)
    assert main(["derive", "refuellings", *b.b, "--write"]) == 0
    assert "1 completed refuelling(s)" in capsys.readouterr().out
    assert main(["derive", "refuellings", *b.b, "--write", "--since", at(0)]) == 2
    assert main(["l1", "read", *b.b, "--kind", "refuelling"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "refuelling"
    shutil.rmtree(tmp_path / "vehicle-a7c1")


def test_a_receipt_from_the_candidate_meets_it_in_l1(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    to_the_station(b)
    b.state(52, "fuel_level", 40.0, "L").state(54, "fuel_level", 60.0, "L")
    b.state(58, "fuel_level", 60.4, "L")
    b.heartbeat(70)
    vb = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    assert main(["receipt", "add", "refuelling", *vb, "--from-candidate", at(52), "--quantity-l", "41.8",
                 "--total-price", "75.20", "--full", "--t", at(600)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert main(["derive", "all", *vb, "--write"]) == 0
    capsys.readouterr()
    [e] = l1.read(tmp_path, V, "refuelling")
    assert e["receipt"] == receipt["id"] and e["confirmation"] == "receipt"
    assert e["quantity_l"] == 41.8 and e["sensor_delta_l"] == 41.5 and e["implausible"] is False
