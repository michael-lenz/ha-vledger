# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0049, as a user of the ``vledger`` command meets it: the version of
the derivation logic is recorded in L1.

Derived from the requirement and the user documentation:

- README's *What it is built of*: ``src/vledger/`` is the library holding
  "all derivation logic", published to PyPI as ``vledger``, and one version
  number serves both packages — so the derivation logic's version is the
  installed ``vledger`` distribution's version;
- l1-format.md's *The manifest* (``{"vledger": "0.6.0", …}``, what this L1
  was derived from "and by what") and *Files* ("Every event carries ``kind``
  … ``quality`` … and ``version``");
- l1-format.md's *When L1 is rebuilt* and the user guide's *The derivation
  on disk* ("``l1 status`` exits 1 when a rebuild is due — no manifest, a
  different library version, …"; ``l1 update`` "rebuilds if one is due");
- developing.md's *Real streams as fixtures*: an event's ``version`` key is
  the one "which a release changes".

The data is a day of a plug-in hybrid with a charge point at home — a drive
to a fuel station and a fill there, a drive home and a charge — written with
``vledger l0`` and ``vledger receipt`` as the user guide shows them. Every
step is the ``vledger`` command run as a program; nothing of ``vledger`` is
imported. The installed version is read as a user reads it, from the
distribution's metadata (what ``pip show vledger`` prints)."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0049")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
CHARGEPOINT = "cp01"
DAY = datetime(2026, 10, 5, tzinfo=UTC)

HOME = (51.0000, 7.0000)        # the charge point
STATION = (51.1000, 7.0800)     # a fuel station

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


def vledger(base: Path, *argv: str, expect: int | None = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env, cwd=base)
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
    - heartbeats until 16:00."""
    def fix(where):
        return {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}

    lines = [
        (1, "odometer", "1000", "km", {}), (1, "position", "home", None, fix(HOME)),
        (1, "fuel_level", "17.0", "L", {}),
        (1, "soc", "50", "%", {}), (1, "charging_state", "idle", None, {}),
        (3, "odometer", "1010", "km", {}), (3, "position", "not_home", None, fix((51.05, 7.04))),
        (4, "odometer", "1020", "km", {}), (4, "position", "not_home", None, fix(STATION)),
        (4.1, "fuel_level", "15.0", "L", {}), (4.1, "soc", "40", "%", {}),
        (5, "fuel_level", "45.0", "L", {}),
        (5.5, "fuel_level", "45.0", "L", {}),
        (7, "odometer", "1030", "km", {}), (7, "position", "not_home", None, fix((51.05, 7.04))),
        (8, "odometer", "1040", "km", {}), (8, "position", "home", None, fix(HOME)),
        (8.1, "fuel_level", "44.0", "L", {}),
        (9, "charging_state", "charging", None, {}), (9.1, "position", "home", None, fix(HOME)),
        (10, "soc", "55", "%", {}),
        (11, "soc", "70", "%", {}), (11, "charging_state", "idle", None, {}),
    ]
    lines += [(h / 2, None, None, None, None) for h in range(1, 33)]
    return sorted(lines, key=lambda x: (x[0], x[1] is None))


def write_day(base: Path) -> None:
    """Both streams as the integration writes them, and the receipts."""
    for flag, subject, config in (("--chargepoint", CHARGEPOINT, CHARGEPOINT_CONFIG),
                                  ("--vehicle", VEHICLE, VEHICLE_CONFIG)):
        vledger(base, "l0", "start", flag, subject, "--t", at(0), "--homeassistant", "2026.9.4",
                "--snapshot", "[]")
        vledger(base, "l0", "config", flag, subject, "--t", at(0), "--config", json.dumps(config))
    for hour, role, value, unit, attrs in scenario():
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
    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(5.2),
            "--quantity-l", "41.37", "--total-price", "72.36", "--full")
    vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(10),
            "--energy-kwh", "8.1", "--total-price", "2.83")


def l1_dir(base: Path, subject: str = f"vehicle-{VEHICLE}") -> Path:
    return base / subject / "l1"


def manifest(base: Path, subject: str = f"vehicle-{VEHICLE}") -> dict:
    return json.loads((l1_dir(base, subject) / "manifest.json").read_text(encoding="utf-8"))


def events(base: Path) -> dict[str, list[dict]]:
    """Every event file of the vehicle's L1, by kind, as the lines lie on disk."""
    return {kind: jsonl((l1_dir(base) / name).read_text(encoding="utf-8"))
            for kind, name in FILES.items()}


@pytest.fixture(scope="module")
def inputs(tmp_path_factory) -> Path:
    """The day's data directory: both streams and the receipts, and no L1."""
    base = tmp_path_factory.mktemp("day")
    write_day(base)
    return base


@pytest.fixture
def base(inputs: Path, tmp_path: Path) -> Path:
    """A fresh copy of the day's data directory, for a test to derive in."""
    to = tmp_path / "vledger"
    shutil.copytree(inputs, to, ignore=shutil.ignore_patterns("l1"))
    return to


@pytest.fixture(scope="module")
def library() -> str:
    """The version of the derivation logic installed: the ``vledger``
    distribution's."""
    return installed_version("vledger")


@pytest.mark.parametrize("how", [("derive", "all", "--vehicle", VEHICLE, "--write"),
                                 ("l1", "update", "--vehicle", VEHICLE)],
                         ids=["rebuild", "live"])
def test_the_manifest_names_the_library_version_that_derived_l1(base, library, how):
    """Whether L1 is rebuilt with ``derive all --write`` or written by the
    run the integration makes live, ``l1 update``, the manifest records the
    version of the library that derived it, under ``vledger``."""
    vledger(base, *how)
    assert manifest(base).get("vledger") == library, manifest(base)


@pytest.mark.parametrize("how", [("derive", "all", "--vehicle", VEHICLE, "--write"),
                                 ("l1", "update", "--vehicle", VEHICLE)],
                         ids=["rebuild", "live"])
def test_every_event_in_l1_carries_the_version_that_derived_it(base, library, how):
    """Every line of every event file — trips, refuellings, charging
    sessions and periods — carries ``version``, and it is the version the
    manifest records: the library's that wrote it."""
    vledger(base, *how)
    by_kind = events(base)
    for kind in ("trip", "refuelling", "charging", "period"):
        assert by_kind[kind], (kind, "the day yields no such event")
    for kind, lines in by_kind.items():
        for event in lines:
            assert event.get("kind") == kind, event
            assert "version" in event, event
            assert event["version"] == library, (kind, event["version"], library)


def test_l1_read_gives_back_the_version_each_event_was_derived_by(base, library):
    """What ``l1 read`` prints of an event is the event as L1 holds it, its
    ``version`` with it."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    for kind in FILES:
        read = jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind).stdout)
        assert read, kind
        assert {e.get("version") for e in read} == {library}, (kind, read)


def test_a_charge_point_s_manifest_names_the_library_version_too(base, library):
    """A charge point's ``l1/`` holds a manifest and nothing else
    (l1-format.md, *Files*); written by ``l1 update`` on the charge point's
    stream — every verb names its stream with ``--vehicle`` or
    ``--chargepoint`` (the user guide's *Where the data is*) — it too records
    the library version that wrote it."""
    vledger(base, "l1", "update", "--chargepoint", CHARGEPOINT)
    path = l1_dir(base, f"chargepoint-{CHARGEPOINT}") / "manifest.json"
    assert path.is_file(), sorted(str(p.relative_to(base)) for p in base.rglob("*"))
    assert manifest(base, f"chargepoint-{CHARGEPOINT}").get("vledger") == library


def test_l1_recorded_by_another_version_is_due_for_a_rebuild(base, library):
    """The recorded version is the one checked: an L1 whose manifest names
    a library version other than the installed one is due for a rebuild —
    ``l1 status`` exits 1 — and is not when it names the installed one."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=0)

    path = l1_dir(base) / "manifest.json"
    m = manifest(base)
    m["vledger"] = "0.0.1"
    path.write_text(json.dumps(m), encoding="utf-8")
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=1)
    assert status.stdout or status.stderr


def test_l1_update_rebuilds_l1_of_another_version_under_its_own(base, library):
    """An L1 derived by another version — its manifest and its events
    saying so — is rebuilt by the next ``l1 update``, after which the
    manifest and every event record the installed version, and no rebuild
    is due."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    for name in FILES.values():
        path = l1_dir(base) / name
        lines = jsonl(path.read_text(encoding="utf-8"))
        path.write_text("".join(json.dumps({**e, "version": "0.0.1"}) + "\n" for e in lines),
                        encoding="utf-8")
    m = manifest(base)
    m["vledger"] = "0.0.1"
    (l1_dir(base) / "manifest.json").write_text(json.dumps(m), encoding="utf-8")

    vledger(base, "l1", "update", "--vehicle", VEHICLE)

    assert manifest(base).get("vledger") == library
    for kind, lines in events(base).items():
        assert lines, kind
        assert {e.get("version") for e in lines} == {library}, (kind, lines)
    vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=0)
