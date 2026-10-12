# SPDX-License-Identifier: BSD-3-Clause
"""Fuel at 15 °C (VER-08, ADR-0040): the atom, the low-pass filter over its
fixed window, and the normalised deltas on refuellings and trips — built
through the l0 verbs (ADR-0005). τ is 10 minutes here, so the window is 50."""

import math

import pytest

from test_trips import ROAD, Builder, at
from vledger import l1, refuellings, series, temperature, trips
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
TAU = 600
BETA = 9.5e-4      # petrol's, the default
TEMP = {"outside_temperature": {"entity": "sensor.t"}}
FUEL = {"fuel_level": {"entity": "sensor.f"}}
STATION = (51.1000, 7.0800)


def builder(tmp_path, capsys, roles=None, tau=TAU):
    return Builder(tmp_path, capsys, thresholds={"temperature_tau_s": tau}, roles=roles)


def filtered(base, minutes, tau=TAU):
    return temperature.filtered(series.load(base, V), at(minutes), tau)


def test_the_atom_for_each_fuel(capsys):
    assert main(["calc", "normalise-fuel", "50", "25"]) == 0
    assert capsys.readouterr().out == "49.529 L at 15 °C\n"
    assert main(["calc", "normalise-fuel", "50", "25", "--fuel", "diesel"]) == 0
    assert capsys.readouterr().out == "49.603 L at 15 °C\n"
    assert main(["calc", "normalise-fuel", "50", "-5", "--beta", "1e-3"]) == 0
    assert capsys.readouterr().out == "51.020 L at 15 °C\n"
    assert temperature.normalise(50, 15, BETA) == 50


def test_the_filter_follows_irregular_samples(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    b.state(0, "outside_temperature", 10, "°C").state(15, "outside_temperature", 30, "°C")
    b.state(16, "outside_temperature", 86, "°F")                        # 30 °C: units converted
    y15 = 10.0                                                          # started at its first sample
    y20 = 30 + (y15 - 30) * math.exp(-300 / TAU)
    assert filtered(tmp_path, 20) == pytest.approx(y20)
    assert filtered(tmp_path, 0) == 10.0
    assert filtered(tmp_path, -1) is None                               # nothing in the window yet


def test_the_window_starts_at_the_value_in_effect_and_forgets_what_came_before(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    b.state(-60, "outside_temperature", 100, "°C").state(-55, "outside_temperature", 0, "°C")
    b.state(-5, "outside_temperature", 10, "°C")
    # The window is [-50, 0]: it starts at 0 °C, and 100 °C never enters.
    assert filtered(tmp_path, 0) == pytest.approx(10 - 10 * math.exp(-300 / TAU))
    assert filtered(tmp_path, -5) == 0.0                                # held 50 min: a value in effect counts


def test_a_dropout_restarts_the_filter_and_one_after_the_last_sample_leaves_none(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    b.state(0, "outside_temperature", 10, "°C").state(5, "outside_temperature", "unavailable")
    assert filtered(tmp_path, 7) is None
    b.state(8, "outside_temperature", 20, "°C")
    assert filtered(tmp_path, 10) == 20.0                               # restarted, not pulled from 10


def test_a_dropout_before_the_window_clears_the_value_it_starts_from(tmp_path, capsys):
    """The value in effect holds through a dropout (ADR-0046); the filter
    alone treats one as a clearing (ADR-0040, 3) — also before the window."""
    b = builder(tmp_path, capsys)
    b.state(-100, "outside_temperature", 10, "°C").state(-60, "outside_temperature", "unavailable")
    b.heartbeat(0)
    assert series.in_effect(series.load(tmp_path, V), "outside_temperature", at(-50)).value == 10
    assert filtered(tmp_path, 0) is None                                # not started from the held 10
    b.state(1, "outside_temperature", 20, "°C")
    assert filtered(tmp_path, 2) == 20.0


def test_a_capture_gap_restarts_the_filter(tmp_path, capsys):
    b = builder(tmp_path, capsys)
    b.state(0, "outside_temperature", 10, "°C")
    b.run("start", *b.b, "--t", at(30), "--homeassistant", "2026.9.4")   # a crash in between
    b.state(31, "outside_temperature", 20, "°C")
    assert filtered(tmp_path, 30.5) is None                             # nothing is read across it
    assert filtered(tmp_path, 35) == 20.0


def test_the_filtered_temperature_verb(tmp_path, capsys):
    b = builder(tmp_path, capsys, roles=TEMP)
    b.state(0, "outside_temperature", 10, "°C").state(10, "outside_temperature", 20, "°C")
    assert main(["calc", "filtered-temperature", *b.b, "--at", at(20)]) == 0
    assert capsys.readouterr().out == f"{20 - 10 * math.exp(-1):.2f} °C\n"   # 16.32
    assert main(["calc", "filtered-temperature", *b.b, "--at", at(-1)]) == 1
    assert "no sample in the window" in capsys.readouterr().err

    other = builder(tmp_path / "other", capsys)
    assert main(["calc", "filtered-temperature", *other.b, "--at", at(20)]) == 1
    assert "no outside_temperature role" in capsys.readouterr().err


def to_the_station(b):
    b.state(15, "odometer", 1007, "km").fix(15, *ROAD[0])
    b.state(30, "odometer", 1015, "km").fix(30, *ROAD[1])
    b.state(45, "odometer", 1022, "km").fix(45, *STATION, zone="fuel_station")
    b.state(46, "fuel_level", 18.9, "L")
    b.state(52, "fuel_level", 60.0, "L").state(58, "fuel_level", 60.4, "L")
    b.heartbeat(70)


def test_a_refuelling_carries_its_delta_at_15_c(tmp_path, capsys):
    b = builder(tmp_path, capsys, roles=dict(FUEL, **TEMP))
    b.state(-30, "outside_temperature", 25, "°C")
    to_the_station(b)
    [r] = refuellings.derive_from(tmp_path, V, completed_only=True)
    assert r.sensor_delta_l == 41.5                                     # the raw delta is untouched
    expected = (60.4 - 18.9) / (1 + BETA * 10)
    assert r.sensor_delta_15c_l == round(expected, 3) and r.sensor_delta_15c_l < r.sensor_delta_l


def test_without_an_outside_temperature_role_there_is_no_15_c_figure(tmp_path, capsys):
    b = builder(tmp_path, capsys, roles=FUEL)
    b.state(-30, "outside_temperature", 25, "°C")                       # logged, but no role assigned
    to_the_station(b)
    [r] = refuellings.derive_from(tmp_path, V, completed_only=True)
    assert r.sensor_delta_l == 41.5 and r.sensor_delta_15c_l is None


def test_a_trip_carries_its_fuel_delta_at_15_c(tmp_path, capsys):
    b = builder(tmp_path, capsys, roles=TEMP)
    b.state(0, "outside_temperature", 5, "°C").state(50, "fuel_level", 20.5, "L")
    end = b.drive(60)
    b.state(end + 3, "fuel_level", 18.9, "L")
    b.heartbeat(end + 60)
    [t] = trips.derive_from(tmp_path, V, completed_only=True)
    assert t.delta_fuel_l == -1.6
    assert t.delta_fuel_15c_l == round((18.9 - 20.5) / (1 + BETA * -10), 3)

    plain = builder(tmp_path / "plain", capsys)
    plain.state(50, "fuel_level", 20.5, "L").drive(60)
    plain.state(end + 3, "fuel_level", 18.9, "L").heartbeat(end + 60)
    [t] = trips.derive_from(tmp_path / "plain", V, completed_only=True)
    assert t.delta_fuel_l == -1.6 and t.delta_fuel_15c_l is None


def replayed(live, tmp_path):
    """The live stream fed line by line to the incremental derivation."""
    replay = tmp_path / "replay"
    src = live / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst = replay / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst.parent.mkdir(parents=True)
    with open(dst, "a") as f:
        for line in src.read_text().splitlines(keepends=True):
            f.write(line)
            f.flush()
            l1.incremental(replay, V)
    l1.rebuild(live, V)
    return replay


def test_live_refuellings_equal_one_rebuild_with_the_temperature_across_the_cursor(tmp_path, capsys):
    # τ = 30 min: the window, 150 min, reaches back past the cursor the
    # first refuelling leaves, so the live run must load it from L0.
    live = tmp_path / "live"
    b = builder(live, capsys, roles=dict(FUEL, **TEMP), tau=1800)
    b.state(-40, "outside_temperature", 8, "°C").state(30, "outside_temperature", 12, "°C")
    to_the_station(b)
    b.state(100, "outside_temperature", 18, "°C")
    b.state(120, "fuel_level", 50.0, "L").state(125, "outside_temperature", 21, "°C")
    b.state(130, "fuel_level", 65.0, "L")
    b.heartbeat(140)
    replay = replayed(live, tmp_path)
    name = "refuellings.jsonl"
    assert (l1.l1_dir(live, V) / name).read_bytes() == (l1.l1_dir(replay, V) / name).read_bytes()
    events = list(l1.read(live, V, "refuelling"))
    assert len(events) == 2 and all(e["sensor_delta_15c_l"] is not None for e in events)
    s = series.load(live, V)
    fuel = s.series["fuel_level"]
    assert events[1]["sensor_delta_15c_l"] == temperature.Source(s).delta_15c(
        series.last_at_or_before(fuel, at(120)), series.last_at_or_before(fuel, at(130)))


def test_live_trips_equal_one_rebuild_with_the_temperature_across_the_cursor(tmp_path, capsys):
    live = tmp_path / "live"
    b = builder(live, capsys, roles=dict(FUEL, **TEMP), tau=1800)
    b.state(0, "outside_temperature", 6, "°C").state(50, "fuel_level", 40.0, "L")
    end = b.drive(60)
    b.state(end + 3, "fuel_level", 38.4, "L").state(end + 20, "outside_temperature", 14, "°C")
    b.heartbeat(end + 60)
    end2 = b.drive(end + 95, odo_start=1022)
    b.state(end2 + 3, "fuel_level", 36.9, "L").state(end2 + 10, "outside_temperature", 19, "°C")
    b.heartbeat(end2 + 61)
    replay = replayed(live, tmp_path)
    name = "trips.jsonl"
    assert (l1.l1_dir(live, V) / name).read_bytes() == (l1.l1_dir(replay, V) / name).read_bytes()
    events = list(l1.read(live, V, "trip"))
    assert len(events) == 2 and all(e["delta_fuel_15c_l"] is not None for e in events)
