# SPDX-License-Identifier: BSD-3-Clause
"""The metrics report (CLI-04, VER-09): a span's metrics from L1 and the
stream, on the day test_periods works out by hand — with the outside
temperature reported, so an interval has a mean to carry."""

import json

import pytest

from test_charging import HOME, HOME_TARIFFS, at, charge, chargepoint
from test_periods import receipt, trip, vehicle
from vledger import l1, periods, report
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")


@pytest.fixture
def ledger(tmp_path, capsys):
    """test_periods' day, plus the outside temperature: 8 °C before the
    first trip, 12 °C during the charge, 16 °C after the second refuelling —
    and L1 derived. Returns the verbs' --base and --vehicle."""
    v = vehicle(tmp_path, capsys)
    home = chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS)
    v.state(5, "outside_temperature", 8, "°C")
    trip(v, 0, 1000, 30, fuel=17, soc=40)
    v.state(80, "fuel_level", 37, "L")
    v.state(90, "outside_temperature", 12, "°C")
    charge(v, 100, 40, 60, minutes=60, meter=home, reading=5000.0, kwh=2.5)
    home.heartbeat(400)
    trip(v, 200, 1030, 30, fuel=34, soc=50)
    v.state(280, "fuel_level", 37, "L")
    v.state(300, "outside_temperature", 16, "°C")
    v.heartbeat(400)
    receipt(tmp_path, capsys, 80, 20, 35.00)
    receipt(tmp_path, capsys, 280, 3, 5.40)
    vb = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    assert main(["derive", "all", *vb, "--write"]) == 0
    capsys.readouterr()
    return vb


def test_the_whole_stream_is_the_lifetime_line(ledger, tmp_path):
    """Over the stream's whole span the report says what the lifetime line
    says — computed from the events, not read from periods.jsonl."""
    r = report.from_l1(tmp_path, V)
    life = next(x for x in l1.read(tmp_path, V, "period") if x["period"] == "lifetime")
    assert r["report"]["period"] == "report"
    # The counters are the span's own: the starting values from before
    # capture are the lifetime line's alone (VER-11).
    own = {"period", "charge_cycles", "tank_fills"}
    assert {k: v for k, v in r["report"].items() if k not in own} == \
        {k: v for k, v in life.items() if k not in own}
    assert (r["report"]["charge_cycles"], r["report"]["tank_fills"]) == (0.2, 0.575)
    assert (life["charge_cycles"], life["tank_fills"]) == (100.2, 10.575)
    assert set(periods.METRICS) < set(r["report"]) and set(periods.CONSUMPTION) < set(r["report"])
    # The lifetime line names the interval; the report lists it, with its
    # temperature: the one sample between the two refuellings, 12 °C.
    x, = r["intervals"]
    assert (x["from"], x["to"], x["receipts"], x["l_per_100km"], x["quality"]) == (
        at(80), at(280), 2, 10, "receipt")
    assert x["error_pct"] == 0 and x["mean_outside_temperature_c"] == 12.0


def test_a_span_of_its_own(ledger, tmp_path):
    """From after the first trip to after the second refuelling: the second
    trip, the charge, both refuellings; the first trip is out by its start."""
    r = report.from_l1(tmp_path, V, since=at(50), until=at(300))
    m = r["report"]
    assert (m["start"], m["end"], m["open"]) == (at(50), at(300), False)
    assert m["distance_km"] == 30 and m["distance_km_quality"] == "measured"
    assert m["fuel_purchased_l"] == 23 and m["fuel_cost_eur"] == 40.40
    # 23 bought + (level at +50, 17 L, minus level at +300, 37 L).
    assert m["fuel_consumed_l"] == 3 and m["fuel_level_corrected"] is True
    assert m["grid_kwh"] == 2.5 and m["charge_cycles"] == 0.2
    # SoC 40 % at +50 (the first trip's end), 50 % at +300: 1 kWh of stock
    # went into the span's distance on top of the 2 kWh charged.
    assert m["battery_kwh"] == 1 and m["soc_corrected"] is True
    assert m["consumption_l_per_100km"] == 10 and m["consumption_receipts"] == 2
    assert len(r["intervals"]) == 1


def test_a_span_without_the_interval_has_no_consumption(ledger, tmp_path):
    """The interval's first receipt lies before the span: no interval, no
    consumption — and the metrics of what is in it all the same."""
    r = report.from_l1(tmp_path, V, since=at(150), until=at(400))
    m = r["report"]
    assert m["distance_km"] == 30 and m["fuel_purchased_l"] == 3 and m["grid_kwh"] == 0
    assert m["fuel_consumed_l"] == 3            # 37 L at both bounds
    assert m["grid_kwh_per_100km"] == 0 and m["electricity_cost_eur"] == 0
    assert r["intervals"] == [] and m["consumption_l_per_100km"] is None
    assert m["consumption_quality"] is None
    # The end is inclusive, as --until is everywhere: a span ending at a
    # refuelling's start holds it.
    assert report.from_l1(tmp_path, V, since=at(150), until=at(280))["report"]["fuel_purchased_l"] == 3
    # Past the stream's last line the span is still running.
    assert report.from_l1(tmp_path, V, since=at(150), until=at(500))["report"]["open"] is True


def test_a_span_with_nothing_in_it(ledger, tmp_path):
    m = report.from_l1(tmp_path, V, since=at(300), until=at(400))["report"]
    assert m["distance_km"] == 0 and m["eur_per_100km"] is None and m["quality"] == "estimated"
    with pytest.raises(ValueError, match="lies before"):
        report.from_l1(tmp_path, V, since=at(400), until=at(300))


def test_the_verb(ledger, tmp_path, capsys):
    assert main(["report", "metrics", *ledger]) == 0
    out, err = capsys.readouterr()
    assert out.startswith(f"a7c1: {at(-60)} to {at(400)}, 0 capture gap(s), quality estimated\n")
    assert "distance_km" in out and "        60  measured" in out
    assert "consumption: 10 L/100 km (receipt)" in out and "12" in out.splitlines()[-1]
    assert err.strip() == "1 tank-to-tank interval(s)"

    assert main(["report", "metrics", *ledger, "--json", "--since", at(150), "--until", at(400)]) == 0
    out, err = capsys.readouterr()
    r = json.loads(out)
    assert r["report"]["distance_km"] == 30 and r["intervals"] == []
    assert err.strip() == "0 tank-to-tank interval(s)"
    assert main(["report", "metrics", *ledger]) == 0
    assert "consumption: no tank-to-tank interval" not in capsys.readouterr().out

    target = tmp_path / "report.json"
    assert main(["report", "metrics", *ledger, "--json", "--out", str(target)]) == 0
    assert json.loads(target.read_text())["report"]["period"] == "report"
    assert capsys.readouterr().out.strip() == f"{target}: 1 tank-to-tank interval(s)"

    assert main(["report", "metrics", "--base", str(tmp_path), "--chargepoint", "home"]) == 2
    assert "a vehicle's" in capsys.readouterr().err
    assert main(["report", "metrics", "--base", str(tmp_path / "none"), "--vehicle", "a7c1"]) == 2
    assert "derive all --write first" in capsys.readouterr().err


def test_a_report_says_when_l1_is_stale(ledger, tmp_path, capsys):
    assert main(["receipt", "add", "charging", *ledger, "--anchor", at(100), "--energy-kwh", "2.6",
                 "--total-price", "0.80", "--t", at(600)]) == 0
    capsys.readouterr()
    assert main(["report", "metrics", *ledger]) == 0
    assert "not current (the receipts changed)" in capsys.readouterr().err
