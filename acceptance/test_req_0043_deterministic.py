# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0043, as a user meets it: the derivation is deterministic — the same
inputs yield the same results.

Derived from the requirement and the user documentation: README.md
("Derives, deterministically … computed from L0, receipts and configuration
— the same logic live in Home Assistant and in batch from the command line,
recomputable from scratch at any time"), and the user guide's *The
derivations: vledger derive* (each derivation prints what it found as JSON
Lines), *The derivation on disk: vledger l1* ("judged by the stream's last
line rather than the clock, so the same stream always yields the same
files"; ``l1 update`` is what the integration runs live), *Reports* and
*Exports*. The manifest's ``derived_at`` is the one key of L1 that says when
rather than from what (l1-format.md, *The manifest*), and is left out where
L1's files are compared.

The inputs are a day of a plug-in hybrid with a charge point at home — two
drives and a refuelling, a charge at home and one elsewhere — and receipts
for the fill and both charges, written with ``vledger l0`` and ``vledger
receipt`` as the user guide shows them. "The same inputs" is that data
directory, or a copy of it: a receipt's id is minted when it is entered, so
entering it again is a different input.

Every step is the ``vledger`` command run as a program; nothing of
``vledger`` is imported."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0043")]

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
        "fuel_price": {"entity": "sensor.fuel_price"},
        "outside_temperature": {"entity": "sensor.outside_temperature"},
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

KINDS = ("trip", "refuelling", "charging", "period")
DERIVE = {"trip": "trips", "refuelling": "refuellings", "charging": "charging", "period": "periods"}

# Environments a user may run the command in: another time zone, locale and
# home, another hash seed for the interpreter. None of them is an input.
ELSEWHERE = [
    {"TZ": "Pacific/Kiritimati", "LC_ALL": "C", "LANG": "C", "PYTHONHASHSEED": "1"},
    {"TZ": "America/Los_Angeles", "LC_ALL": "C.UTF-8", "LANG": "de_DE.UTF-8", "PYTHONHASHSEED": "4242"},
]


def at(hours: float) -> str:
    """A time of the scenario: hours after midnight UTC on 5 October 2026."""
    return (DAY + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def vledger(base: Path, *argv: str, env: dict | None = None, cwd: Path | None = None,
            expect: int = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ.update(env or {})
    # The tree's own src, as an absolute path, so that vledger is found from
    # whatever working directory the process runs in.
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=cwd or base)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def scenario() -> list[tuple]:
    """The day, as (hour, role, state, unit, attrs) in time order, ``role``
    None for a heartbeat of both streams every half hour:

    - 01:00 at home, 17 L in the tank, 50 % charged, 9 °C outside;
    - 03:00–04:00 a drive to a fuel station, the fill at 05:00 from 15 to 45 L;
    - 07:00–08:00 home again, charging there from 09:00 to 11:00, 40 → 70 %;
    - 13:00–14:00 a drive elsewhere, charging there from 15:00 to 16:00,
      50 → 90 %, at no configured charge point;
    - heartbeats until 20:00."""
    def fix(where):
        return {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}

    lines = [
        (1, "odometer", "1000", "km", {}), (1, "position", "home", None, fix(HOME)),
        (1, "fuel_level", "17.0", "L", {}), (1, "fuel_price", "1.859", "EUR/L", {}),
        (1, "outside_temperature", "9.0", "°C", {}),
        (1, "soc", "50", "%", {}), (1, "charging_state", "idle", None, {}),
        # To the fuel station.
        (3, "odometer", "1010", "km", {}), (3, "position", "not_home", None, fix((51.05, 7.04))),
        (3.5, "outside_temperature", "11.5", "°C", {}),
        (4, "odometer", "1020", "km", {}), (4, "position", "not_home", None, fix(STATION)),
        (4.1, "fuel_level", "15.0", "L", {}), (4.1, "soc", "40", "%", {}),
        (4.5, "fuel_price", "1.899", "EUR/L", {}),
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
        (12, "outside_temperature", "14.0", "°C", {}),
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
    """Log ``lines`` of the scenario as the integration would."""
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
            "--quantity-l", "41.37", "--total-price", "72.36", "--full", "--place", "motorway services")
    vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(10),
            "--energy-kwh", "8.1", "--total-price", "2.83")
    vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(15.5),
            "--energy-kwh", "11.8", "--total-price", "6.49", "--provider", "roaming card")


def derived(base: Path, env: dict | None = None, cwd: Path | None = None) -> dict[str, str]:
    """What each derivation prints, verbatim, and which receipt met which
    event."""
    out = {kind: vledger(base, "derive", DERIVE[kind], "--vehicle", VEHICLE, env=env, cwd=cwd).stdout
           for kind in KINDS}
    out["match"] = vledger(base, "derive", "match", "--vehicle", VEHICLE, env=env, cwd=cwd).stdout
    return out


def l1_files(base: Path) -> dict[str, bytes]:
    """The vehicle's L1 as it lies on disk, the manifest without the moment
    it was written."""
    d = base / f"vehicle-{VEHICLE}" / "l1"
    out = {}
    for p in sorted(d.iterdir()):
        if p.name == "manifest.json":
            m = json.loads(p.read_text(encoding="utf-8"))
            m.pop("derived_at", None)
            out[p.name] = json.dumps(m, sort_keys=True).encode()
        else:
            out[p.name] = p.read_bytes()
    return out


def l1_read(base: Path) -> dict[str, list[dict]]:
    """Every kind of event in L1, as ``l1 read`` gives it."""
    return {kind: jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind).stdout)
            for kind in KINDS}


def copy(base: Path, to: Path) -> Path:
    """The inputs alone — L0 and the receipts of every subject — in another
    directory, without any L1."""
    shutil.copytree(base, to, ignore=shutil.ignore_patterns("l1"))
    assert not list(to.rglob("l1"))
    return to


@pytest.fixture(scope="module")
def inputs(tmp_path_factory) -> Path:
    """The day's data directory: both streams and the three receipts."""
    base = tmp_path_factory.mktemp("day")
    begin(base)
    log(base, scenario())
    receipts(base)
    return base


@pytest.fixture
def base(inputs: Path, tmp_path: Path) -> Path:
    """A fresh copy of the day's inputs, for a test to derive in."""
    return copy(inputs, tmp_path / "vledger")


def test_the_day_derives_every_kind_of_event(base):
    """The inputs are not trivially empty: the day derives trips, the fill,
    both charges and the periods, and in L1 the receipts are met. (With the
    odometer sampled hourly and T_still at 30 minutes, how many trips a drive
    makes is the sampling's to say, and not what this requirement is about.)"""
    out = {kind: jsonl(derived(base)[kind]) for kind in KINDS}
    assert out["trip"], out
    assert len(out["refuelling"]) == 1, out["refuelling"]
    assert [c["chargepoint"] for c in out["charging"]] == [CHARGEPOINT, "foreign"], out["charging"]
    assert {p["period"] for p in out["period"]} >= {"month", "year", "lifetime"}, out["period"]

    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    events = l1_read(base)
    [fill] = events["refuelling"]
    assert fill["quantity_l"] == 41.37, fill
    assert [c["grid_kwh"] for c in events["charging"]] == [8.1, 11.8], events["charging"]


def test_each_derivation_prints_the_same_every_time_it_runs(base):
    """``vledger derive`` run three times on the same inputs prints the same
    bytes each time, for every kind of event."""
    first = derived(base)
    for _ in range(2):
        assert derived(base) == first


def test_the_same_inputs_derive_the_same_in_another_directory_and_environment(base, tmp_path):
    """A copy of the inputs, derived from another working directory under
    another time zone, locale, home and hash seed, prints what the original
    prints."""
    here = derived(base)
    for i, env in enumerate(ELSEWHERE):
        cwd = tmp_path / f"cwd-{i}"
        cwd.mkdir()
        other = copy(base, tmp_path / f"elsewhere-{i}" / "data")
        assert derived(other, env={**env, "HOME": str(cwd)}, cwd=cwd) == here, env


def test_l1_built_twice_from_scratch_is_the_same_files(base, tmp_path):
    """``derive all --write`` on the same inputs, once here and once in a
    copy under another environment, and again after ``l1 clean``, writes the
    same files byte for byte, the manifest's ``derived_at`` aside."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    first = l1_files(base)
    assert {"manifest.json", "trips.jsonl", "refuellings.jsonl", "charging-sessions.jsonl",
            "periods.jsonl"} <= set(first), sorted(first)

    vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write", env=ELSEWHERE[0])
    assert l1_files(base) == first

    other = copy(base, tmp_path / "elsewhere")
    vledger(other, "derive", "all", "--vehicle", VEHICLE, "--write", env=ELSEWHERE[1], cwd=tmp_path)
    assert l1_files(other) == first


def test_l1_kept_live_holds_what_a_rebuild_from_scratch_holds(inputs, tmp_path):
    """The same logic live and in batch: the day logged in five parts, with
    ``l1 update`` after each as the integration runs it, leaves in L1 the
    events ``derive all --write`` writes from the whole day at once."""
    live = tmp_path / "live"
    live.mkdir()
    begin(live)
    shutil.copy(inputs / f"vehicle-{VEHICLE}" / "receipts.jsonl",
                live / f"vehicle-{VEHICLE}" / "receipts.jsonl")
    lines = scenario()
    for hours in ((0, 4.6), (4.6, 8.6), (8.6, 12.5), (12.5, 16.2), (16.2, 99)):
        log(live, [x for x in lines if hours[0] <= x[0] < hours[1]])
        vledger(live, "l1", "update", "--vehicle", VEHICLE)
    assert vledger(live, "l1", "status", "--vehicle", VEHICLE).returncode == 0

    batch = copy(live, tmp_path / "batch")
    vledger(batch, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert l1_read(live) == l1_read(batch)


def test_a_report_and_an_export_of_the_same_l1_are_the_same(base, tmp_path):
    """What is rendered from the derivation — a report of the whole of
    capture and every kind as CSV — is the same for the same inputs, built
    and rendered in two places under two environments."""
    def rendered(where: Path, env: dict) -> dict[str, str]:
        vledger(where, "derive", "all", "--vehicle", VEHICLE, "--write", env=env)
        out = {"report": vledger(where, "report", "metrics", "--vehicle", VEHICLE, "--json",
                                 env=env).stdout}
        for kind in KINDS:
            out[kind] = vledger(where, "export", "csv", "--vehicle", VEHICLE, "--kind", kind,
                                env=env).stdout
        return out

    first = rendered(base, ELSEWHERE[0])
    assert json.loads(first["report"])["report"], first["report"]
    assert all(first[kind].strip() for kind in KINDS), first
    other = copy(base, tmp_path / "elsewhere")
    assert rendered(other, ELSEWHERE[1]) == first
