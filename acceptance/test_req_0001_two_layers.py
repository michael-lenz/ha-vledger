# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0001, as a user meets it: two layers, the raw log L0 and the
derivation L1, and L1 computed exclusively from L0, receipts and
configuration.

Black-box: every step is the ``vledger`` CLI run as a separate process,
the way the user guide shows it — a stream written with ``vledger l0``, a
receipt entered with ``vledger receipt``, L1 built and read with
``vledger derive`` and ``vledger l1``. Nothing here imports the library.

What the user can see of "exclusively":

- the same L0, receipts and configuration give the same L1, wherever and
  whenever L1 is built, under whatever environment;
- L1 can be deleted and comes back the same, while L0 and the receipts are
  never written by a derivation;
- a change to the receipts or to the configuration — and only to one of
  the inputs — changes L1, and taking the change back restores it.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
HOME = ("51.0000", "7.0000")
ROAD = [("51.0300", "7.0200"), ("51.0600", "7.0500"), ("51.0900", "7.0700")]
STATION = ("51.1000", "7.0800")


def at(minutes: int) -> str:
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    h, m = divmod(6 * 60 + minutes, 60)
    d, h = divmod(h, 24)
    return f"2026-10-{5 + d:02d}T{h:02d}:{m:02d}:00.000Z"


def vledger(base: Path, *argv: str, env: dict | None = None, cwd: Path | None = None,
            expect: int = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ.update(env or {})
    # The process runs in another directory than the tests: a relative
    # PYTHONPATH such as the checks' ``src`` would no longer find vledger,
    # so the tree's own src goes first, as an absolute path.
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=cwd or base)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def state(base: Path, minutes: int, role: str, value: str, unit: str | None = None, **attrs: str) -> None:
    argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", role,
            "--entity", f"sensor.{role}", "--state", value]
    if unit:
        argv += ["--unit", unit]
    for k, v in attrs.items():
        argv += ["--attr", f"{k}={v}"]
    vledger(base, *argv)


def fix(base: Path, minutes: int, where: tuple[str, str], zone: str = "not_home") -> None:
    state(base, minutes, "position", zone, latitude=where[0], longitude=where[1], gps_accuracy="0")


CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "position": {"entity": "sensor.position"},
        "fuel_level": {"entity": "sensor.fuel_level"},
        "ignition": {"entity": "sensor.ignition", "map": {"on": ["on"]}},
    },
    "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
}


def write_stream(base: Path) -> None:
    """A morning as the integration would have logged it: a drive to a fuel
    station, a fill, 70 minutes without a move, the drive home, nearly three
    hours of quiet."""
    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": "1000", "unit": "km", "since": at(-120)},
        {"role": "position", "entity": "sensor.position", "state": "home", "since": at(-120),
         "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]), "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": "20.5", "unit": "L", "since": at(-120)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60), "--homeassistant", "2026.9.4",
            "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60), "--config", json.dumps(CONFIG))
    # To the station.
    state(base, 0, "ignition", "on")
    state(base, 15, "odometer", "1007", "km"), fix(base, 15, ROAD[0])
    state(base, 30, "odometer", "1015", "km"), fix(base, 30, ROAD[1])
    state(base, 45, "odometer", "1022", "km"), fix(base, 45, STATION, zone="fuel_station")
    state(base, 46, "fuel_level", "18.9", "L")
    state(base, 47, "ignition", "off")
    # The fill, seen in two steps, settling a little higher.
    state(base, 52, "fuel_level", "40.0", "L")
    state(base, 54, "fuel_level", "60.0", "L")
    state(base, 58, "fuel_level", "60.4", "L")
    # Home, after the stop.
    state(base, 100, "ignition", "on")
    state(base, 115, "odometer", "1037", "km"), fix(base, 115, ROAD[2])
    state(base, 130, "odometer", "1044", "km"), fix(base, 130, HOME, zone="home")
    state(base, 131, "fuel_level", "58.1", "L")
    state(base, 132, "ignition", "off")
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(300), "--lines", "0")


def add_receipt(base: Path) -> str:
    """The pump's receipt for the fill; returns its id."""
    p = vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(55),
                "--quantity-l", "41.37", "--total-price", "72.36", "--full", "--place", "motorway services")
    return json.loads(p.stdout)["id"]


def stream_dir(base: Path) -> Path:
    return base / f"vehicle-{VEHICLE}"


def l1_files(base: Path) -> dict[str, bytes]:
    """L1 as it lies on disk: every file under ``l1/``, the manifest without
    the moment it was written, which is when, not from what."""
    d = stream_dir(base) / "l1"
    out = {}
    for p in sorted(d.iterdir()):
        if p.name == "manifest.json":
            m = json.loads(p.read_text(encoding="utf-8"))
            m.pop("derived_at")
            out[p.name] = json.dumps(m, sort_keys=True).encode()
        else:
            out[p.name] = p.read_bytes()
    return out


def inputs(base: Path) -> dict[str, bytes]:
    """Everything in the data directory but L1: L0, the receipts, anything else."""
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*"))
            if p.is_file() and "l1" not in p.relative_to(base).parts}


def read(base: Path, kind: str) -> list[dict]:
    p = vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind)
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "home-a"
    b.mkdir()
    write_stream(b)
    add_receipt(b)
    return b


def test_l1_is_a_layer_of_its_own_beside_l0(base):
    """The two layers: deriving with --write puts L1 under l1/ beside the
    stream, and what it holds is the morning — two trips and the fill, the
    fill priced by its receipt."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert (stream_dir(base) / "l1" / "manifest.json").is_file()
    trips = read(base, "trip")
    assert [(t["start"] <= at(15), t["distance_km"]) for t in trips] == [(True, 22), (False, 22)]
    [fill] = read(base, "refuelling")
    assert fill["receipt"] and fill["quantity_l"] == 41.37
    assert vledger(base, "l1", "status", "--vehicle", VEHICLE).returncode == 0


def test_the_same_inputs_give_the_same_l1_wherever_and_whenever_it_is_built(base, tmp_path):
    """L0 and the receipts copied to another directory, with no L1, and
    derived there by another working directory under another time zone,
    locale and HOME, give the same L1 byte for byte."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    elsewhere = tmp_path / "somewhere" / "else"
    shutil.copytree(base, elsewhere, ignore=shutil.ignore_patterns("l1"))
    assert not (stream_dir(elsewhere) / "l1").exists()
    other_cwd = tmp_path / "cwd"
    other_cwd.mkdir()
    vledger(elsewhere, "derive", "all", "--vehicle", VEHICLE, "--write", cwd=other_cwd,
            env={"TZ": "Pacific/Kiritimati", "LC_ALL": "C", "LANG": "de_DE.UTF-8", "HOME": str(other_cwd)})
    assert l1_files(elsewhere) == l1_files(base)


def test_l1_deleted_comes_back_the_same_and_the_inputs_are_never_written(base):
    """L1 is regenerable: `l1 clean` deletes it, a rebuild is then due, and
    the rebuild yields the same files. No derivation touches L0 or the
    receipts, nor writes anything outside l1/."""
    before = inputs(base)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    first = l1_files(base)
    vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    assert not (stream_dir(base) / "l1").exists()
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=1)
    assert "no manifest" in status.stdout + status.stderr
    vledger(base, "l1", "update", "--vehicle", VEHICLE)   # what the integration runs: rebuilds, as one is due
    assert l1_files(base) == first
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert l1_files(base) == first
    assert inputs(base) == before


def test_l1_follows_the_receipts_and_nothing_but_them_changed(base):
    """A receipt is an input: cancelling it makes a rebuild due and the fill
    unpriced; entering it again prices it as before. L0 stays as it was, and
    the trips with it."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    with_receipt = l1_files(base)
    [uuid] = [json.loads(line)["id"] for line in
              vledger(base, "receipt", "list", "--vehicle", VEHICLE).stdout.splitlines() if line.strip()]
    l0_before = {k: v for k, v in inputs(base).items() if not k.endswith("receipts.jsonl")}

    vledger(base, "receipt", "cancel", uuid, "--vehicle", VEHICLE, "--note", "entered twice")
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=1)
    assert "receipts" in status.stdout + status.stderr
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    [fill] = read(base, "refuelling")
    assert not fill.get("receipt")
    assert l1_files(base)["refuellings.jsonl"] != with_receipt["refuellings.jsonl"]
    assert l1_files(base)["trips.jsonl"] == with_receipt["trips.jsonl"]

    add_receipt(base)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    [fill] = read(base, "refuelling")
    assert fill["receipt"] and fill["quantity_l"] == 41.37
    # The receipt entered again is a receipt of its own, with an id of its
    # own, which is all the fill now differs by.
    assert fill["receipt"] != uuid
    again = read(base, "refuelling")[0]
    assert again == dict(json.loads(with_receipt["refuellings.jsonl"]), receipt=again["receipt"])
    assert l1_files(base)["trips.jsonl"] == with_receipt["trips.jsonl"]
    assert {k: v for k, v in inputs(base).items() if not k.endswith("receipts.jsonl")} == l0_before


def test_l1_follows_the_configuration(base):
    """The configuration is an input: a later config line with a longer
    T_still makes a rebuild due and reads the stop at the station as no
    standstill, so the morning is one trip; the configuration set back
    restores the two trips."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert len(read(base, "trip")) == 2
    events_before = {k: v for k, v in l1_files(base).items() if k != "manifest.json"}

    longer = dict(CONFIG, thresholds=dict(CONFIG["thresholds"], t_still_s=5400))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(301), "--config", json.dumps(longer))
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=1)
    assert "configuration" in status.stdout + status.stderr
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    [trip] = read(base, "trip")
    assert trip["distance_km"] == 44

    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(302), "--config", json.dumps(CONFIG))
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert len(read(base, "trip")) == 2
    # The trips are as before; the periods read L0 up to its last line,
    # which the two config lines moved, so only they may differ.
    after = {k: v for k, v in l1_files(base).items() if k != "manifest.json"}
    assert after["trips.jsonl"] == events_before["trips.jsonl"]
    assert after["refuellings.jsonl"] == events_before["refuellings.jsonl"]
