# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0048, as a user of the ``vledger`` command meets it: the live
derivation applies the batch derivation's logic incrementally and writes the
same L1 files; after a restart it resumes from the last completed event in L1
and re-reads L0 from there, so what L1 held before stays as it was; the
derivation keeps no state outside L0, receipts, configuration and L1, and L1
can be regenerated from the other three at any time (the sentence as
ADR-0067 amended it).

Derived from the requirement, its note and ADR-0067, and the user
documentation:

- the user guide's *The derivation on disk: vledger l1*: ``l1 update`` is
  "what the integration runs live: append what completed, print what is
  new", rebuilding only when one is due — "no manifest, a different library
  version, a changed configuration of the vehicle or of a charge point, or
  changed receipts" — which ``l1 status`` reports by exiting 1, "which is
  what the integration checks at startup"; ``derive all --write`` rebuilds
  all of ``l1/`` from scratch; ``l1 clean`` deletes ``l1/``, "it is
  regenerable";
- the user guide's *The raw log: vledger l0* and l0-format.md's ``start``,
  ``stop`` and ``config``: what a restart leaves in L0 — an orderly ``stop``
  (or none, after a crash), then a ``start`` with a snapshot of every
  assigned role and the configuration again;
- l1-format.md's *Who writes what* (event files are append-only between
  rebuilds), *The cursor* ("re-reads L0 from there … appends only events
  whose ``start`` is later than that kind's ``through`` … there is no state
  outside L0, receipts, configuration and L1"; ``l1 update`` answers with the
  events new to L1) and *The manifest* (``derived_at`` says when rather than
  from what, and is left out where two L1s are compared).

A restart of Home Assistant is, to the derivation, what it leaves on disk:
the L0 lines above, written here with ``vledger l0`` as the integration
writes them, and the next run of ``l1 update`` in a process of its own. Each
run is given a home, a working directory and a temporary directory it has
never seen, so that nothing outside the data directory can carry over from
one run to the next.

The data is a day of a plug-in hybrid: a drive to a fuel station and a fill,
a drive home and a charge, a drive elsewhere; an orderly restart while
parked after the fill, a crash once the charge has ended, and a crash a few
minutes after the last arrival, inside its standstill. Every step is the
``vledger`` command run as a program; nothing of ``vledger`` is imported."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0048")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
DAY = datetime(2026, 10, 5, tzinfo=UTC)

HOME = (51.0000, 7.0000)
STATION = (51.1000, 7.0800)
AWAY = (51.3000, 7.4000)

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

EVENT_FILES = ("trips.jsonl", "refuellings.jsonl", "charging-sessions.jsonl", "periods.jsonl")
KINDS = ("trip", "refuelling", "charging", "period")

# The restarts of the day: (hour of the stop, or None for a crash; hour of the
# start).
RESTARTS = ((6.0, 6.25), (None, 12.0), (None, 14.2))

# The runs of ``l1 update``: after the lines up to each of these hours. Some
# fall in the middle of a drive or of a charge, where an event is open; the
# first after each restart is right after its start.
RUNS = (2.0, 3.5, 4.6, 5.9, 6.25, 8.5, 10.0, 11.6, 12.0, 13.5, 14.2, 16.0, 20.0)


def at(hours: float) -> str:
    """A time of the scenario: hours after midnight UTC on 5 October 2026."""
    return (DAY + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def vledger(base: Path, *argv: str, env: dict | None = None, cwd: Path | None = None,
            expect: int | None = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ.update(env or {})
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=cwd or base)
    if expect is not None:
        assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def fix(where: tuple[float, float]) -> dict:
    return {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}


def scenario() -> list[tuple]:
    """The day, as (hour, what, …) in time order: ``("state", role, state,
    unit, attrs)``, ``("heartbeat",)``, ``("stop",)`` and ``("start",)``.

    - 01:00 at home, 17 L in the tank, 50 % charged;
    - 03:00–04:00 to a fuel station, the fill at 05:00 from 15 to 45 L;
    - 06:00 an orderly stop, 06:15 the start again;
    - 07:00–08:00 home, charging from 09:00 to 11:00, 40 → 70 %;
    - 12:00 a start without a stop: a crash;
    - 13:00–14:00 a drive elsewhere, and a crash at 14:12, inside its
      standstill;
    - heartbeats every half hour until 20:00, none while capture is off."""
    states = [
        (1, "odometer", "1000", "km", {}), (1, "position", "home", None, fix(HOME)),
        (1, "fuel_level", "17.0", "L", {}), (1, "fuel_price", "1.859", "EUR/L", {}),
        (1, "outside_temperature", "9.0", "°C", {}),
        (1, "soc", "50", "%", {}), (1, "charging_state", "idle", None, {}),
        (3, "odometer", "1010", "km", {}), (3, "position", "not_home", None, fix((51.05, 7.04))),
        (3.5, "outside_temperature", "11.5", "°C", {}),
        (4, "odometer", "1020", "km", {}), (4, "position", "not_home", None, fix(STATION)),
        (4.1, "fuel_level", "15.0", "L", {}), (4.1, "soc", "40", "%", {}),
        (4.5, "fuel_price", "1.899", "EUR/L", {}),
        (5, "fuel_level", "45.0", "L", {}),
        (5.5, "fuel_level", "45.0", "L", {}),
        (7, "odometer", "1030", "km", {}), (7, "position", "not_home", None, fix((51.05, 7.04))),
        (8, "odometer", "1040", "km", {}), (8, "position", "home", None, fix(HOME)),
        (8.1, "fuel_level", "44.0", "L", {}),
        (9, "charging_state", "charging", None, {}), (9.1, "position", "home", None, fix(HOME)),
        (10, "soc", "55", "%", {}),
        (11, "soc", "70", "%", {}), (11, "charging_state", "idle", None, {}),
        (12.5, "outside_temperature", "14.0", "°C", {}),
        (13, "odometer", "1060", "km", {}), (13, "position", "not_home", None, fix((51.2, 7.2))),
        (14, "odometer", "1085", "km", {}), (14, "position", "not_home", None, fix(AWAY)),
        (14.1, "soc", "50", "%", {}), (14.1, "fuel_level", "42.0", "L", {}),
    ]
    lines = [(h, "state", *rest) for h, *rest in states]
    off = [(stop if stop is not None else start, start) for stop, start in RESTARTS]
    for stop, start in RESTARTS:
        if stop is not None:
            lines.append((stop, "stop"))
        lines.append((start, "start"))
    lines += [(h / 2, "heartbeat") for h in range(1, 41)
              if not any(a <= h / 2 <= b for a, b in off)]
    order = {"stop": 0, "start": 1, "state": 2, "heartbeat": 3}
    return sorted(lines, key=lambda x: (x[0], order[x[1]]))


class Capture:
    """What the integration writes to L0, keeping what Home Assistant holds
    of each role for the snapshot of a start."""

    def __init__(self, base: Path):
        self.base = base
        self.held: dict[str, dict] = {}

    def start(self, hour: float) -> None:
        snapshot = [{"role": role, "entity": VEHICLE_CONFIG["roles"][role]["entity"], **held}
                    for role, held in self.held.items()]
        vledger(self.base, "l0", "start", "--vehicle", VEHICLE, "--t", at(hour),
                "--homeassistant", "2026.9.4", "--snapshot", json.dumps(snapshot))
        vledger(self.base, "l0", "config", "--vehicle", VEHICLE, "--t", at(hour),
                "--config", json.dumps(VEHICLE_CONFIG))

    def log(self, line: tuple) -> None:
        hour, what, *rest = line
        if what == "start":
            self.start(hour)
        elif what == "stop":
            vledger(self.base, "l0", "stop", "--vehicle", VEHICLE, "--t", at(hour),
                    "--reason", "shutdown")
        elif what == "heartbeat":
            vledger(self.base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(hour),
                    "--lines", "0")
        else:
            role, value, unit, attrs = rest
            argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(hour), "--role", role,
                    "--entity", VEHICLE_CONFIG["roles"][role]["entity"], "--state", value]
            if unit:
                argv += ["--unit", unit]
            for k, v in attrs.items():
                argv += ["--attr", f"{k}={v}"]
            vledger(self.base, *argv)
            before = self.held.get(role)
            changed = (before is None or before["state"] != value
                       or before.get("attrs", {}) != attrs or role == "position")
            held = {"state": value,
                    "since": at(hour) if changed else before["since"]}
            if unit:
                held["unit"] = unit
            if attrs:
                held["attrs"] = attrs
            self.held[role] = held


# The receipts, entered once their event has completed and before the next
# restart, as (hour entered, argv): the pump's receipt for the fill and the
# bill for the charge. A changed receipts file makes a rebuild due, which the
# run after it makes, before the restart.
RECEIPTS = (
    (5.85, ("receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(5.2),
            "--quantity-l", "41.37", "--total-price", "72.36", "--full")),
    (11.55, ("receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(10),
             "--energy-kwh", "8.1", "--total-price", "2.83")),
)


def stranger(tmp: Path, n: int) -> tuple[dict, Path]:
    """A home, a working directory and a temporary directory no run has seen,
    and another time zone and hash seed: a fresh process after a restart."""
    root = tmp / f"process-{n}"
    for d in ("home", "cwd", "tmp"):
        (root / d).mkdir(parents=True)
    env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
           "XDG_CONFIG_HOME": str(root / "home" / ".config"),
           "XDG_CACHE_HOME": str(root / "home" / ".cache"),
           "XDG_STATE_HOME": str(root / "home" / ".local" / "state"),
           "TZ": ("Pacific/Kiritimati", "America/Los_Angeles", "UTC")[n % 3],
           "PYTHONHASHSEED": str(n)}
    return env, root / "cwd"


def l1_dir(base: Path) -> Path:
    return base / f"vehicle-{VEHICLE}" / "l1"


def event_files(base: Path) -> dict[str, bytes]:
    """The vehicle's event files and periods, as they lie on disk."""
    d = l1_dir(base)
    return {name: (d / name).read_bytes() if (d / name).exists() else b"" for name in EVENT_FILES}


def manifest(base: Path) -> dict:
    """The manifest, without the moment it was written."""
    m = json.loads((l1_dir(base) / "manifest.json").read_text(encoding="utf-8"))
    m.pop("derived_at", None)
    return m


def held_events(base: Path) -> set[tuple[str, str]]:
    """The completed events L1 holds, by (kind, start)."""
    out = set()
    for kind in KINDS[:3]:
        out |= {(e["kind"], e["start"])
                for e in jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE,
                                       "--kind", kind).stdout)}
    return out


def inputs_only(base: Path, to: Path) -> Path:
    """L0 and the receipts alone, without any L1, in another directory."""
    shutil.copytree(base, to, ignore=shutil.ignore_patterns("l1"))
    assert not list(to.rglob("l1"))
    return to


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> dict:
    """The day captured and derived as the integration does: the lines
    logged, each receipt entered after its event, and ``l1 update`` run in a
    fresh process after each part. Kept for the tests: the data directory, and for every run
    the event files and the events L1 held before it, what it printed, and
    what ``l1 status`` said before it."""
    tmp = tmp_path_factory.mktemp("req0048")
    base = tmp / "vledger"
    base.mkdir()
    capture = Capture(base)
    capture.start(0)
    lines = scenario()
    runs = []
    done = -1.0
    for n, upto in enumerate(RUNS):
        for line in (x for x in lines if done < x[0] <= upto):
            capture.log(line)
        for _, argv in (r for r in RECEIPTS if done < r[0] <= upto):
            vledger(base, *argv)
        done = upto
        env, cwd = stranger(tmp, n)
        before = {"files": event_files(base) if l1_dir(base).exists() else None,
                  "held": held_events(base) if l1_dir(base).exists() else set(),
                  "status": vledger(base, "l1", "status", "--vehicle", VEHICLE, env=env,
                                    cwd=cwd, expect=None).returncode}
        printed = vledger(base, "l1", "update", "--vehicle", VEHICLE, env=env, cwd=cwd).stdout
        runs.append({"upto": upto, "before": before, "printed": jsonl(printed),
                     "after": event_files(base), "manifest": manifest(base)})
    return {"base": base, "runs": runs, "tmp": tmp}


def test_the_day_derives_events_on_both_sides_of_the_restarts(live):
    """The scenario is not trivially empty: L1 holds trips before the first
    restart and after it, the fill and the charge."""
    trips = jsonl(vledger(live["base"], "l1", "read", "--vehicle", VEHICLE, "--kind", "trip").stdout)
    assert any(t["start"] < at(6.0) for t in trips), trips
    assert any(t["start"] > at(6.25) for t in trips), trips
    [fill] = jsonl(vledger(live["base"], "l1", "read", "--vehicle", VEHICLE,
                           "--kind", "refuelling").stdout)
    assert fill["quantity_l"] == 41.37, fill
    [charge] = jsonl(vledger(live["base"], "l1", "read", "--vehicle", VEHICLE,
                             "--kind", "charging").stdout)
    assert charge["grid_kwh"] == 8.1, charge


@pytest.mark.parametrize("restart", RESTARTS, ids=["orderly", "crash", "crash-in-standstill"])
def test_after_a_restart_no_rebuild_is_due(live, restart):
    """At the first run after a restart — the orderly one as much as a
    crash — L1 is not due for a rebuild: ``l1 status``, which the
    integration checks at startup, exits 0, and the run resumes from L1."""
    [run] = [r for r in live["runs"] if r["upto"] == restart[1]]
    assert run["before"]["files"] is not None
    assert run["before"]["status"] == 0, run


def test_what_l1_held_before_a_restart_stays_as_it_was(live):
    """Every run no rebuild was due for, those after a restart included,
    leaves the event files as they were and appends to them: the events
    completed before stay byte for byte, and what L1 held is not derived
    anew. (A run after a receipt was entered rebuilds, as it should.)"""
    for run in live["runs"]:
        before = run["before"]["files"]
        if before is None or run["before"]["status"] != 0:
            continue
        for name in EVENT_FILES[:3]:
            assert run["after"][name].startswith(before[name]), (run["upto"], name)


def test_a_run_after_a_restart_prints_only_what_is_new(live):
    """``l1 update`` after a restart answers with the events new to L1, never
    one L1 held before the restart, and each completed event is announced
    once over the day."""
    seen: list[tuple[str, str]] = []
    for run in live["runs"]:
        new = [(e["kind"], e["start"]) for e in run["printed"]]
        assert not set(new) & run["before"]["held"], (run["upto"], new)
        seen += new
    assert len(seen) == len(set(seen)), seen
    assert {k for k in seen if k[0] != "period"} == held_events(live["base"])


def test_the_cursor_never_moves_back(live):
    """The manifest's cursor, ``through`` per kind, and ``l0_through`` never
    go back from one run to the next, across the restarts as well."""
    for a, b in zip(live["runs"], live["runs"][1:], strict=False):
        assert b["manifest"]["l0_through"] >= a["manifest"]["l0_through"], (a["upto"], b["upto"])
        for kind, t in a["manifest"].get("through", {}).items():
            assert b["manifest"]["through"].get(kind, "") >= t, (kind, a["upto"], b["upto"])


def test_l1_kept_live_across_restarts_is_what_a_rebuild_from_the_inputs_writes(live, tmp_path):
    """L0, receipts and configuration alone, copied without L1 to another
    directory and rebuilt with ``derive all --write`` by a process that never
    saw the live runs, give the event files the live derivation wrote across
    the restarts, byte for byte, and the same manifest."""
    other = inputs_only(live["base"], tmp_path / "elsewhere")
    env, cwd = stranger(tmp_path, 99)
    vledger(other, "derive", "all", "--vehicle", VEHICLE, "--write", env=env, cwd=cwd)
    assert event_files(other) == event_files(live["base"])
    assert manifest(other) == manifest(live["base"])


def test_l1_lost_is_regenerated_by_the_next_run(live, tmp_path):
    """L1 deleted with ``l1 clean`` makes a rebuild due, and the next
    ``l1 update`` — the run the integration makes at startup — writes it
    again as it was."""
    base = tmp_path / "vledger"
    shutil.copytree(live["base"], base)
    vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    assert not l1_dir(base).exists()
    assert vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=None).returncode == 1
    env, cwd = stranger(tmp_path, 7)
    vledger(base, "l1", "update", "--vehicle", VEHICLE, env=env, cwd=cwd)
    assert event_files(base) == event_files(live["base"])
    assert manifest(base) == manifest(live["base"])
    assert vledger(base, "l1", "status", "--vehicle", VEHICLE).returncode == 0
