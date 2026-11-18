# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0047, as a user of the ``vledger`` command meets it: L1 is stored as
files next to L0 and can be deleted and regenerated at any time. JSON Lines
is the L1 of record, written alike by the live and the batch derivation; CSV
is an export produced only by the CLI and the Home Assistant action.

Derived from the requirement and the user documentation:

- l0-format.md's *Files*: ``<base>/vehicle-<subject>/`` holds ``l0/<month>.jsonl``,
  ``receipts.jsonl`` and ``l1/…``, "the derivation, regenerable";
- l1-format.md's *Files* (``manifest.json``, ``trips.jsonl``,
  ``charging-sessions.jsonl``, ``refuellings.jsonl``, ``periods.jsonl``; one
  JSON object per line), *Determinism* ("A stream replayed line by line with
  the incremental derivation after every line yields byte-identical event
  files and ``periods.jsonl`` to one rebuild of the whole stream") and
  *Exports* ("JSON Lines is the L1 of record; CSV, JSON and GPX are
  renderings of it, made by ``vledger export`` and by the Home Assistant
  action ``vledger.export`` from the files as they are and never by the live
  path"; a CSV is "a header, then a row per event in file order", the
  envelope first);
- the user guide's *The derivation on disk: vledger l1* (``derive all
  --write`` rebuilds ``l1/`` from scratch; ``l1 update`` is "what the
  integration runs live"; ``l1 clean`` "delete l1/ — it is regenerable";
  ``l1 status`` exits 1 when a rebuild is due, among others with no
  manifest) and *Exports: vledger export* (``--out`` writes a file, without
  it the CSV goes to stdout; an export derives nothing). The manifest's
  ``derived_at`` says when rather than from what (l1-format.md, *The
  manifest*) and is left out where two L1s are compared.

The "live" derivation is ``l1 update`` run as the stream grows; "batch" is
``derive all --write`` on the whole stream. Home Assistant's own live
derivation and its action are the other module, test_req_0047_l1_files_ha.py.

The data is a day of a plug-in hybrid with a charge point at home — two
drives, a refuelling, a charge at home and one elsewhere — and receipts for
the fill and both charges, written with ``vledger l0`` and ``vledger
receipt`` as the user guide shows them. Every step is the ``vledger``
command run as a program; nothing of ``vledger`` is imported."""

import csv
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0047")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
CHARGEPOINT = "cp01"
DAY = datetime(2026, 10, 5, tzinfo=UTC)

HOME = (51.0000, 7.0000)        # the charge point
STATION = (51.1000, 7.0800)     # a fuel station
AWAY = (51.3000, 7.4000)        # a charge at no configured charge point

VEHICLE_CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "position": {"entity": "device_tracker.car"},
        "fuel_level": {"entity": "sensor.fuel_level"},
        "soc": {"entity": "sensor.soc"},
        "charging_state": {"entity": "sensor.charging_state", "map": {"charging": ["charging"]}},
    },
    "parameters": {"fuel": "petrol", "battery_net_kwh": 10.0, "tank_capacity_l": 50},
    "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
    "time_zone": "Europe/Berlin",
}

CHARGEPOINT_CONFIG = {
    "name": "Home",
    "latitude": HOME[0],
    "longitude": HOME[1],
    "radius_m": 100,
    "meter": None,
    "power_kw": 3.7,
    "tariffs": [{"from": "2026-01-01", "eur_per_kwh": 0.30}],
}

# Each kind of event, the file l1-format.md names for it.
FILES = {"trip": "trips.jsonl", "refuelling": "refuellings.jsonl",
         "charging": "charging-sessions.jsonl", "period": "periods.jsonl"}


def at(hours: float) -> str:
    """A time of the day: hours after midnight UTC on 5 October 2026."""
    return (DAY + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def vledger(base: Path, *argv: str, expect: int | None = 0,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env, cwd=cwd or base)
    if expect is not None:
        assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def scenario() -> list[tuple]:
    """The day, as (hour, role, state, unit, attrs) in time order, ``role``
    None for a heartbeat of both streams every half hour:

    - 01:00 at home, 17 L in the tank, 50 % charged;
    - 03:00–04:00 a drive to a fuel station, the fill at 05:00 from 15 to 45 L;
    - 07:00–08:00 home again, charging there from 09:00 to 11:00, 40 → 70 %;
    - 13:00–14:00 a drive elsewhere, charging there from 15:00 to 16:00,
      50 → 90 %, at no configured charge point;
    - heartbeats until 20:00."""
    def fix(where):
        return {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}

    lines = [
        (1, "odometer", "1000", "km", {}), (1, "position", "home", None, fix(HOME)),
        (1, "fuel_level", "17.0", "L", {}),
        (1, "soc", "50", "%", {}), (1, "charging_state", "idle", None, {}),
        # To the fuel station.
        (3, "odometer", "1010", "km", {}), (3, "position", "not_home", None, fix((51.05, 7.04))),
        (4, "odometer", "1020", "km", {}), (4, "position", "not_home", None, fix(STATION)),
        (4.1, "fuel_level", "15.0", "L", {}), (4.1, "soc", "40", "%", {}),
        (5, "fuel_level", "45.0", "L", {}),
        (5.5, "fuel_level", "45.0", "L", {}),
        # Home.
        (7, "odometer", "1030", "km", {}), (7, "position", "not_home", None, fix((51.05, 7.04))),
        (8, "odometer", "1040", "km", {}), (8, "position", "home", None, fix(HOME)),
        (8.1, "fuel_level", "44.0", "L", {}),
        (9, "charging_state", "charging", None, {}), (9.1, "position", "home", None, fix(HOME)),
        (10, "soc", "55", "%", {}),
        (11, "soc", "70", "%", {}), (11, "charging_state", "idle", None, {}),
        # Elsewhere.
        (13, "odometer", "1060", "km", {}), (13, "position", "not_home", None, fix((51.2, 7.2))),
        (14, "odometer", "1085", "km", {}), (14, "position", "not_home", None, fix(AWAY)),
        (14.1, "soc", "50", "%", {}), (14.1, "fuel_level", "42.0", "L", {}),
        (15, "charging_state", "charging", None, {}), (15.1, "position", "not_home", None, fix(AWAY)),
        (15.5, "soc", "70", "%", {}),
        (16, "soc", "90", "%", {}), (16, "charging_state", "idle", None, {}),
    ]
    lines += [(h / 2, None, None, None, None) for h in range(1, 41)]
    return sorted(lines, key=lambda x: (x[0], x[1] is None))


def begin(base: Path) -> None:
    """Both streams' start and configuration, as the integration writes them."""
    for flag, subject, config in (("--chargepoint", CHARGEPOINT, CHARGEPOINT_CONFIG),
                                  ("--vehicle", VEHICLE, VEHICLE_CONFIG)):
        vledger(base, "l0", "start", flag, subject, "--t", at(0), "--homeassistant", "2026.9.4",
                "--snapshot", "[]")
        vledger(base, "l0", "config", flag, subject, "--t", at(0), "--config", json.dumps(config))


def log(base: Path, lines: list[tuple]) -> None:
    """Log ``lines`` of the day as the integration would."""
    for hour, role, value, unit, attrs in lines:
        if role is None:
            vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(hour), "--lines", "0")
            vledger(base, "l0", "heartbeat", "--chargepoint", CHARGEPOINT, "--t", at(hour),
                    "--lines", "0")
            continue
        argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(hour), "--role", role,
                "--entity", VEHICLE_CONFIG["roles"][role]["entity"], "--state", value]
        if unit:
            argv += ["--unit", unit]
        for k, v in attrs.items():
            argv += ["--attr", f"{k}={v}"]
        vledger(base, *argv)


def receipts(base: Path) -> None:
    """The pump's receipt for the fill, and a bill for each charge."""
    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(5.2),
            "--quantity-l", "41.37", "--total-price", "72.36", "--full")
    vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(10),
            "--energy-kwh", "8.1", "--total-price", "2.83")
    vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(15.5),
            "--energy-kwh", "11.8", "--total-price", "6.49")


def stream_dir(base: Path) -> Path:
    return base / f"vehicle-{VEHICLE}"


def l1_dir(base: Path) -> Path:
    return stream_dir(base) / "l1"


def l1_files(base: Path) -> dict[str, bytes]:
    """The vehicle's L1 as it lies on disk, the manifest without the moment
    it was written."""
    out = {}
    for p in sorted(l1_dir(base).iterdir()):
        if p.name == "manifest.json":
            m = json.loads(p.read_text(encoding="utf-8"))
            m.pop("derived_at", None)
            out[p.name] = json.dumps(m, sort_keys=True).encode()
        else:
            out[p.name] = p.read_bytes()
    return out


def inputs_on_disk(base: Path) -> dict[str, bytes]:
    """Every file under the data directory that is not L1: the streams and
    the receipts, byte for byte."""
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*"))
            if p.is_file() and "l1" not in p.relative_to(base).parts}


def csv_files(where: Path) -> list[str]:
    return sorted(str(p.relative_to(where)) for p in where.rglob("*")
                  if p.is_file() and p.suffix.lower() == ".csv")


@pytest.fixture(scope="module")
def inputs(tmp_path_factory) -> Path:
    """The day's data directory: both streams and the three receipts, and no
    L1."""
    base = tmp_path_factory.mktemp("day")
    begin(base)
    log(base, scenario())
    receipts(base)
    return base


@pytest.fixture
def base(inputs: Path, tmp_path: Path) -> Path:
    """A fresh copy of the day's data directory, for a test to derive in."""
    to = tmp_path / "vledger"
    shutil.copytree(inputs, to, ignore=shutil.ignore_patterns("l1"))
    return to


def test_l1_is_json_lines_files_in_the_stream_s_directory_beside_l0(base):
    """``derive all --write`` puts L1 in ``l1/`` of the vehicle's directory,
    beside ``l0/`` and ``receipts.jsonl``: a manifest and a JSON Lines file
    per kind of event, every line one JSON object of that kind, the lines
    ``l1 read`` gives back. Nothing in L1 is CSV."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")

    beside = sorted(p.name for p in stream_dir(base).iterdir())
    assert {"l0", "receipts.jsonl", "l1"} <= set(beside), beside
    assert sorted(stream_dir(base).glob("l0/*.jsonl")), beside

    names = sorted(p.name for p in l1_dir(base).iterdir())
    assert names == sorted(["manifest.json", *FILES.values()]), names
    for kind, name in FILES.items():
        text = (l1_dir(base) / name).read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines, name
        assert text.endswith("\n"), name
        events = [json.loads(line) for line in lines]
        assert all(isinstance(e, dict) and e["kind"] == kind for e in events), (name, events)
        read = jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind).stdout)
        assert read == events, kind
    assert csv_files(base) == []


def test_l1_deleted_is_regenerated_the_same_and_l0_is_untouched(base):
    """L1 deleted with ``l1 clean``, or by hand, is gone while the stream and
    the receipts stay byte for byte; a rebuild is then due, and ``derive all
    --write`` — or ``l1 update``, which rebuilds when one is due — writes the
    same files again."""
    kept = inputs_on_disk(base)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    first = l1_files(base)
    assert inputs_on_disk(base) == kept

    vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    assert not l1_dir(base).exists()
    assert inputs_on_disk(base) == kept
    vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=1)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert l1_files(base) == first
    vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=0)

    shutil.rmtree(l1_dir(base))
    assert inputs_on_disk(base) == kept
    vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=1)
    vledger(base, "l1", "update", "--vehicle", VEHICLE)
    assert l1_files(base) == first
    assert inputs_on_disk(base) == kept


def test_the_live_derivation_writes_the_files_the_batch_derivation_writes(inputs, tmp_path):
    """The day logged in parts, with ``l1 update`` — what the integration
    runs live — after each, and the receipts entered along the way, leaves
    event files and ``periods.jsonl`` byte for byte the same as one
    ``derive all --write`` of the whole day."""
    live = tmp_path / "live"
    live.mkdir()
    begin(live)
    lines = scenario()
    cuts = (0, 2.2, 3.6, 4.6, 5.6, 7.6, 9.5, 11.2, 13.6, 15.3, 16.2, 18, 99)
    for lo, hi in pairwise(cuts):
        log(live, [x for x in lines if lo <= x[0] < hi])
        if lo == 16.2:
            receipts(live)
        vledger(live, "l1", "update", "--vehicle", VEHICLE)
    vledger(live, "l1", "status", "--vehicle", VEHICLE, expect=0)
    assert csv_files(live) == []

    batch = tmp_path / "batch"
    shutil.copytree(live, batch, ignore=shutil.ignore_patterns("l1"))
    vledger(batch, "derive", "all", "--vehicle", VEHICLE, "--write")
    for name in FILES.values():
        assert (l1_dir(live) / name).read_bytes() == (l1_dir(batch) / name).read_bytes(), name
    assert jsonl((l1_dir(live) / FILES["trip"]).read_text(encoding="utf-8"))


def test_csv_is_an_export_of_the_json_lines_written_only_where_asked(base, tmp_path):
    """``vledger export csv`` renders a kind's JSON Lines as a header and a
    row per event in file order, to stdout or to the file ``--out`` names,
    and nowhere else: L1 holds no CSV before or after, and its files are
    unchanged by the export."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    before = l1_files(base)

    out = tmp_path / "exports"
    out.mkdir()
    for kind, name in FILES.items():
        events = jsonl((l1_dir(base) / name).read_text(encoding="utf-8"))
        printed = vledger(base, "export", "csv", "--vehicle", VEHICLE, "--kind", kind).stdout
        rows = list(csv.DictReader(io.StringIO(printed)))
        assert len(rows) == len(events), (kind, printed)
        assert [r["kind"] for r in rows] == [kind] * len(events), kind
        assert [r["start"] for r in rows] == [e["start"] for e in events], kind

        target = out / f"{kind}.csv"
        vledger(base, "export", "csv", "--vehicle", VEHICLE, "--kind", kind, "--out", str(target))
        assert target.read_text(encoding="utf-8") == printed, kind

    assert csv_files(base) == []
    assert csv_files(out) == sorted(f"{kind}.csv" for kind in FILES)
    assert l1_files(base) == before


def test_the_csv_renders_l1_as_the_files_hold_it(base):
    """JSON Lines is the record and CSV follows it: a trip cut out of
    ``trips.jsonl`` is missing from the next export too, since an export
    reads L1 as it is on disk and derives nothing; once L1 is rebuilt, the
    export holds the trip again."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")

    def exported() -> list[str]:
        printed = vledger(base, "export", "csv", "--vehicle", VEHICLE, "--kind", "trip").stdout
        return [r["start"] for r in csv.DictReader(io.StringIO(printed))]

    whole = exported()
    assert len(whole) >= 2, whole
    path = l1_dir(base) / FILES["trip"]
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[1:]), encoding="utf-8")
    assert exported() == whole[1:]

    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert exported() == whole
