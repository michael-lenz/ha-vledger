# SPDX-License-Identifier: BSD-3-Clause
"""``vledger l0 anonymise``: a data directory copied with positions shifted
and names dropped (QUA-01), so that it can become a fixture — and the
fixture harness of test_fixtures run on the copy."""

import json
from itertools import pairwise

import pytest

from test_charging import HOME, HOME_TARIFFS, at, charge, chargepoint, vehicle
from test_fixtures import check, replay
from vledger import geo, l0, l1, layout, trips
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
CP = Subject("chargepoint", "home")
ROAD = [(51.0300, 7.0200), (51.0600, 7.0500)]


@pytest.fixture
def real(tmp_path, capsys):
    """A day of the reference vehicle under names a person would give:
    a drive to a named zone, a charge at home metered, a receipt with a
    place. Built through the verbs, like everything here."""
    base = tmp_path / "real"
    home = chargepoint(base, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(base, capsys)
    v.state(0, "odometer", 1007, "km").state(0, "position", "Smith Ltd", latitude=ROAD[0][0],
                                            longitude=ROAD[0][1], gps_accuracy=12)
    v.state(15, "odometer", 1015, "km").state(15, "position", "Smith Ltd", latitude=ROAD[1][0],
                                             longitude=ROAD[1][1], gps_accuracy=12)
    v.state(30, "odometer", 1022, "km").state(30, "position", "home", latitude=HOME[0],
                                             longitude=HOME[1], gps_accuracy=12)
    end = charge(v, 90, 40, 80, meter=home, reading=5000.0, kwh=6.5)
    v.heartbeat(end + 60)
    home.heartbeat(end + 60)
    assert main(["receipt", "add", "charging", *v.b, "--anchor", at(90), "--energy-kwh", "6.6",
                 "--total-price", "1.98", "--place", "Garage, 12 Elm Street",
                 "--provider", "Smith Energy", "--t", at(400)]) == 0
    capsys.readouterr()
    return base


def anonymise(capsys, base, to, shift="0,-3.5", *extra):
    code = main(["l0", "anonymise", "--base", str(base), f"--shift={shift}", "--to", str(to), *extra])
    out, err = capsys.readouterr()
    assert code == 0, err
    return out


def everything(base) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted(base.rglob("*.jsonl")))


def test_names_are_dropped(real, tmp_path, capsys):
    out = anonymise(capsys, real, tmp_path / "anon")
    assert sorted(out.splitlines()) == sorted(
        f"{s.dirname}: {n} line(s)" for s, n in
        ((CP, len(list(l0.read(real, CP)))), (V, len(list(l0.read(real, V))) + 1)))
    text = everything(tmp_path / "anon")
    for name in ("Smith", "Elm Street", 'sensor.o"', "sensor.meter", '"Home"'):
        assert name not in text, name
    assert "zone_1" in text and '"state":"home"' in text
    cfg = [r.line["config"] for r in l0.read(tmp_path / "anon", V, kind="config")][-1]
    assert cfg["name"] == "vehicle"
    assert cfg["roles"]["odometer"]["entity"] == "sensor.odometer"
    # The builders log under x.<role> and configure sensor.o: two entities, one role.
    assert {r.line["entity"] for r in l0.read(tmp_path / "anon", V, role="odometer")} == {"x.odometer"}
    receipt = json.loads((layout.receipts_file(tmp_path / "anon", V)).read_text())
    assert receipt["place"] is None and receipt["provider"] is None and receipt["energy_kwh"] == 6.6


def test_positions_move_and_distances_stay(real, tmp_path, capsys):
    anonymise(capsys, real, tmp_path / "anon", "0,-3.5")
    fixes = {b: [(r.line["attrs"]["latitude"], r.line["attrs"]["longitude"])
                 for r in l0.read(b, V, role="position")] for b in (real, tmp_path / "anon")}
    assert [(a, round(o + 3.5, 7)) for a, o in fixes[tmp_path / "anon"]] == fixes[real]
    for a, b in zip(pairwise(fixes[real]), pairwise(fixes[tmp_path / "anon"]), strict=True):
        assert geo.distance_km(*a[0], *a[1]) == pytest.approx(geo.distance_km(*b[0], *b[1]), abs=1e-6)
    cp = [r.line["config"] for r in l0.read(tmp_path / "anon", CP, kind="config")][-1]
    assert (cp["latitude"], cp["longitude"]) == (HOME[0], HOME[1] - 3.5)
    assert cp["name"] == "chargepoint" and cp["meter"]["entity"] == "sensor.energy_meter"


def test_the_derivations_see_the_same_day(real, tmp_path, capsys):
    """A latitude shift scales east–west distances a little; everything a
    derivation reads from a sensor stays as it was."""
    anon = tmp_path / "anon"
    anonymise(capsys, real, anon, "0.4,-3.5")
    for b in (real, anon):
        assert main(["derive", "all", "--base", str(b), "--vehicle", "a7c1", "--write"]) == 0
    capsys.readouterr()
    keep = ("start", "end", "quality", "distance_km", "distance_source")
    assert [{k: t[k] for k in keep} for t in l1.read(anon, V, "trip")] == \
           [{k: t[k] for k in keep} for t in l1.read(real, V, "trip")]
    keep = ("start", "end", "chargepoint", "grid_kwh", "cost_eur", "receipt", "confirmation")
    sessions = {b: [{k: e[k] for k in keep} for e in l1.read(b, V, "charging")] for b in (real, anon)}
    assert sessions[anon] == sessions[real] and sessions[real][0]["chargepoint"] == "home"
    assert [t.end_zone for t in trips.derive_from(anon, V)] == ["home"]


def test_the_copy_becomes_a_fixture(real, tmp_path, capsys):
    """The harness of test_fixtures on a fixture made the documented way."""
    fixture = tmp_path / "fixture"
    anonymise(capsys, real, fixture)
    assert main(["derive", "all", "--base", str(fixture), "--vehicle", "a7c1", "--write"]) == 0
    capsys.readouterr()
    check(fixture, tmp_path / "work")
    replay(fixture, tmp_path / "replay")
    # and a fixture whose expected L1 is wrong fails
    p = l1.path_of(fixture, V, "trip")
    p.write_text(p.read_text().replace('"quality":"measured"', '"quality":"estimated"', 1))
    with pytest.raises(AssertionError):
        check(fixture, tmp_path / "work2")


def test_one_subject_and_the_refusals(real, tmp_path, capsys):
    out = anonymise(capsys, real, tmp_path / "one", "0,1", "--chargepoint", "home")
    assert out.startswith("chargepoint-home:") and layout.subjects(tmp_path / "one") == [CP]
    b = ["l0", "anonymise", "--base", str(real), "--to", str(tmp_path / "one")]
    assert main([*b, "--shift", "0,1", "--chargepoint", "home"]) == 2     # never into a copy
    assert "exists" in capsys.readouterr().err
    assert main([*b, "--shift", "north"]) == 2
    assert main([*b[:-1], str(tmp_path / "x"), "--shift=50,0"]) == 2       # off the globe
    assert "leaves the globe" in capsys.readouterr().err
