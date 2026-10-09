# SPDX-License-Identifier: BSD-3-Clause
"""Exports (CLI-03): CSV and JSON of one kind, GPX of the trips — renderings
of L1 on disk, from streams built through the verbs (ADR-0005)."""

import csv
import io
import json
from dataclasses import fields
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from test_refuellings import builder, to_the_station
from test_trips import STOP, at
from vledger import charging, clock, export, l1, refuellings, trips
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
G = "{" + export.GPX_NS + "}"


@pytest.fixture
def ledger(tmp_path, capsys):
    """A drive to the station, a refuelling receipted from its candidate, a
    second drive, a charging receipt that meets nothing — and L1 derived."""
    b = builder(tmp_path, capsys)
    b.heartbeat(0)
    to_the_station(b)
    b.state(52, "fuel_level", 40.0, "L").state(54, "fuel_level", 60.0, "L")
    b.state(58, "fuel_level", 60.4, "L")
    b.heartbeat(100)
    b.state(150, "odometer", 1030, "km").fix(150, *STOP)
    b.heartbeat(200)
    vb = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    assert main(["receipt", "add", "refuelling", *vb, "--from-candidate", at(52), "--quantity-l", "41.8",
                 "--total-price", "75.20", "--full", "--place", "Station, A3", "--t", at(600)]) == 0
    assert main(["receipt", "add", "charging", *vb, "--anchor", at(300), "--energy-kwh", "11.5",
                 "--total-price", "6.90", "--t", at(600)]) == 0
    assert main(["derive", "all", *vb, "--write"]) == 0
    capsys.readouterr()
    return vb


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    assert code == 0, err
    return out, err


def test_csv_a_row_per_event_in_the_declared_order(ledger, capsys):
    out, err = run(capsys, "export", "csv", *ledger, "--kind", "trip")
    rows = list(csv.DictReader(io.StringIO(out)))
    assert err.strip() == "2 event(s)"
    header = out.splitlines()[0].split(",")
    assert header[:6] == list(export.ENVELOPE)
    assert "start_position.latitude" in header and "refined_by.end" in header
    assert "waypoints" not in header                    # the GPX's
    assert [r["start"] for r in rows] == [e["start"] for e in l1.read(Path(ledger[1]), V, "trip")]
    first = rows[0]
    assert first["distance_km"] == "22.0" and first["distance_source"] == "odometer"
    assert first["end_zone"] == "fuel_station" and first["start_position.accuracy_m"] == ""


def test_csv_spells_values_as_json_does(ledger, capsys):
    out, _ = run(capsys, "export", "csv", *ledger, "--kind", "refuelling")
    r, = csv.DictReader(io.StringIO(out))
    assert r["confirmation"] == "receipt" and r["full"] == "true" and r["implausible"] == "false"
    assert r["quantity_l"] == "41.8" and r["sensor_delta_l"] == "41.5"
    assert r["place"] == "Station, A3"                 # quoted by the writer, read back whole
    assert r["contenders"] == ""                       # not ambiguous: no key, an empty cell


def test_the_column_order_does_not_depend_on_the_data(ledger, capsys):
    """An event of its own carries a handful of keys; its header is still
    the kind's whole declaration."""
    out, _ = run(capsys, "export", "csv", *ledger, "--kind", "charging")
    header = out.splitlines()[0]
    assert header == ",".join(export.columns("charging", []))
    r, = csv.DictReader(io.StringIO(out))
    assert r["quality"] == "receipt" and r["grid_kwh"] == "11.5" and r["soc_start_pct"] == ""


def test_every_key_l1_holds_is_declared(ledger, capsys):
    """An undeclared key would land, sorted, at the end of the header; the
    declarations are kept complete instead."""
    base = Path(ledger[1])
    for kind in l1.FILES:
        events = list(l1.read(base, V, kind))
        assert events, kind
        assert export.columns(kind, events) == export.columns(kind, []), kind
    for kind, cls in (("trip", trips.Trip), ("refuelling", refuellings.Refuelling),
                      ("charging", charging.Session)):
        assert {f.name for f in fields(cls)} - export.NOT_IN_CSV <= set(export.COLUMNS[kind])


def test_json_is_the_events_as_l1_holds_them(ledger, capsys):
    out, _ = run(capsys, "export", "json", *ledger, "--kind", "trip")
    assert json.loads(out) == list(l1.read(Path(ledger[1]), V, "trip"))
    out, err = run(capsys, "export", "json", *ledger, "--kind", "trip", "--since", at(100))
    assert len(json.loads(out)) == 1 and err.strip() == "1 event(s)"


def test_gpx_a_track_per_trip_in_the_schemas_shape(ledger, capsys, tmp_path):
    target = tmp_path / "trips.gpx"
    out, _ = run(capsys, "export", "gpx", *ledger, "--out", str(target))
    assert out.strip() == f"{target}: 2 trip(s)"
    root = ET.parse(target).getroot()
    assert root.tag == G + "gpx" and root.get("version") == "1.1" and root.get("creator")
    tracks = list(root)
    assert [t.tag for t in tracks] == [G + "trk"] * 2
    # gpx.xsd's trkType is a sequence: name, cmt, desc, src, link, number, type, extensions, trkseg.
    order = ["name", "cmt", "desc", "src", "link", "number", "type", "extensions", "trkseg"]
    events = list(l1.read(Path(ledger[1]), V, "trip"))
    for trk, trip in zip(tracks, events, strict=True):
        tags = [c.tag.removeprefix(G) for c in trk]
        assert tags == sorted(tags, key=order.index)
        assert trk.find(G + "name").text == trip["start"]
        assert int(trk.find(G + "number").text) >= 1
        points = trk.find(G + "trkseg").findall(G + "trkpt")
        assert len(points) == len(trip["waypoints"])
        for pt, w in zip(points, trip["waypoints"], strict=True):
            assert set(pt.attrib) == {"lat", "lon"}
            assert -90 <= float(pt.get("lat")) <= 90 and -180 <= float(pt.get("lon")) < 180
            assert float(pt.get("lat")) == w["latitude"]
            assert [c.tag for c in pt] == [G + "time"] and pt[0].text == w["t"]
        times = [clock.parse(pt[0].text) for pt in points]
        assert times == sorted(times)


def test_an_export_needs_l1_and_says_when_it_is_stale(tmp_path, capsys, ledger):
    assert main(["export", "csv", "--base", str(tmp_path / "none"), "--vehicle", "a7c1",
                 "--kind", "trip"]) == 2
    assert "derive all --write" in capsys.readouterr().err
    assert main(["receipt", "cancel", *ledger, "--t", at(700),
                 json.loads(run(capsys, "receipt", "list", *ledger, "--kind", "charging")[0])["id"]]) == 0
    capsys.readouterr()
    _, err = run(capsys, "export", "gpx", *ledger)
    assert "not current (the receipts changed)" in err


def test_an_empty_kind_is_a_header_and_nothing_else():
    assert export.to_csv("trip", []).count("\n") == 1
    assert export.to_json([]) == "[]\n"
    assert ET.fromstring(export.to_gpx([])).tag == G + "gpx"
