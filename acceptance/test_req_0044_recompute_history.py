# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0044 on the command line: the whole history can be fully recomputed at
any time.

Derived from the requirement and the user documentation: README.md
("Derives, deterministically … the same logic live in Home Assistant and in
batch from the command line, recomputable from scratch at any time"), the
user guide's *The derivation on disk: vledger l1* (``derive all --write``
"rebuild all of l1/ from scratch, swapped in whole"; ``l1 status`` "exits 1
when a rebuild is due — no manifest, … or changed receipts"; ``l1 clean``
"delete l1/ — it is regenerable"; ``l1 read``) and *The derivations: vledger
derive* (each derivation prints what it found, one event per line), and
l1-format.md's *Files* (one file per kind of event; the manifest's
``derived_at`` is when, not from what). The Home Assistant action is the
other module, ``test_req_0044_recompute_history_ha.py``.

"The whole history" is a stream that spans two months — the last evening of
September and the first morning of October, in the vehicle's time zone —
with drives in both and a refuelling, written with ``vledger l0`` as the user
guide shows it. "Fully recomputed" is L1 rebuilt to hold every event that
the derivations print from that stream, however L1 stood before: missing,
damaged, or derived before a receipt was entered. "At any time" is a rebuild
with a trip still under way, and again once more of the stream has come.

Every step is the ``vledger`` command run as a program; nothing of
``vledger`` is imported."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytestmark = [pytest.mark.req("REQ-0044")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
START = datetime(2026, 9, 30, tzinfo=UTC)   # hours count from here
BERLIN = ZoneInfo("Europe/Berlin")

HOME = (51.0000, 7.0000)
WORK = (51.1000, 7.1000)
STATION = (51.0500, 7.0300)

VEHICLE_CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "position": {"entity": "device_tracker.car"},
        "fuel_level": {"entity": "sensor.fuel_level"},
    },
    "parameters": {"fuel": "petrol", "tank_capacity_l": 50},
    "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
    "time_zone": "Europe/Berlin",
}

KINDS = ("trip", "refuelling", "period")
DERIVE = {"trip": "trips", "refuelling": "refuellings", "period": "periods"}
FILES = {"trip": "trips.jsonl", "refuelling": "refuellings.jsonl"}


def at(hours: float) -> str:
    """A time of the history: hours after midnight UTC on 30 September 2026."""
    return (START + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def instant(text: str) -> datetime:
    """A time the command wrote, as the instant it names."""
    return datetime.fromisoformat(text).astimezone(UTC)


def month(text: str) -> str:
    """The month a time falls in, as the vehicle's time zone counts it."""
    return datetime.fromisoformat(text).astimezone(BERLIN).strftime("%Y-%m")


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


def history() -> list[tuple]:
    """The history, as (hour, role, state, attrs) in time order, ``role``
    None for a heartbeat every half hour:

    - 30 September, 06:00 UTC: at home, 15 L in the tank;
    - 07:00–08:00 to work; 10:00 a stop at a fuel station on the way back,
      filled from 14 to 44 L by 10:30, home at 11:00;
    - 17:00–18:00 to work and back by 19:00 (21:00 in Berlin, still September);
    - 1 October, 06:00–07:00 UTC (08:00 in Berlin, October): to work;
    - heartbeats until 1 October, 12:00 UTC."""
    def fix(where):
        return {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}

    lines = [(6, "odometer", "5000", {}), (6, "position", "home", fix(HOME)),
             (6, "fuel_level", "15.0", {})]
    lines += [(7, "odometer", "5010", {}), (7, "position", "not_home", fix((51.05, 7.05))),
              (8, "odometer", "5020", {}), (8, "position", "not_home", fix(WORK)),
              (8.1, "fuel_level", "14.0", {})]
    lines += [(9.5, "odometer", "5030", {}), (9.5, "position", "not_home", fix((51.08, 7.06))),
              (10, "odometer", "5035", {}), (10, "position", "not_home", fix(STATION)),
              (10.2, "fuel_level", "30.0", {}), (10.3, "fuel_level", "44.0", {}),
              (10.6, "fuel_level", "44.0", {}),
              (11, "odometer", "5042", {}), (11, "position", "home", fix(HOME))]
    lines += [(17, "odometer", "5052", {}), (17, "position", "not_home", fix((51.05, 7.05))),
              (18, "odometer", "5062", {}), (18, "position", "not_home", fix(WORK)),
              (18.5, "odometer", "5072", {}), (18.5, "position", "not_home", fix((51.05, 7.05))),
              (19, "odometer", "5082", {}), (19, "position", "home", fix(HOME)),
              (19.1, "fuel_level", "42.0", {})]
    lines += [(30, "odometer", "5092", {}), (30, "position", "not_home", fix((51.05, 7.05))),
              (31, "odometer", "5102", {}), (31, "position", "not_home", fix(WORK)),
              (31.1, "fuel_level", "41.0", {})]
    lines += [(h / 2, None, None, None) for h in range(12, 73)]
    return sorted(lines, key=lambda x: (x[0], x[1] is None))


def begin(base: Path) -> None:
    """The stream's start and configuration, as the integration writes them."""
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(5.5), "--homeassistant",
            "2026.9.4", "--snapshot", "[]")
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(5.5), "--config",
            json.dumps(VEHICLE_CONFIG))


def log(base: Path, lines: list[tuple]) -> None:
    """Log ``lines`` of the history as the integration would."""
    for hour, role, value, attrs in lines:
        if role is None:
            vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(hour), "--lines", "0")
            continue
        argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(hour), "--role", role,
                "--entity", VEHICLE_CONFIG["roles"][role]["entity"], "--state", value]
        argv += {"odometer": ["--unit", "km"], "fuel_level": ["--unit", "L"]}.get(role, [])
        for k, v in attrs.items():
            argv += ["--attr", f"{k}={v}"]
        vledger(base, *argv)


def derived(base: Path) -> dict[str, list[dict]]:
    """What each derivation prints from the stream as it is now."""
    return {kind: jsonl(vledger(base, "derive", DERIVE[kind], "--vehicle", VEHICLE).stdout)
            for kind in KINDS}


def l1(base: Path) -> dict[str, list[dict]]:
    """Every kind of event in L1, as ``l1 read`` gives it."""
    return {kind: jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind).stdout)
            for kind in KINDS}


def recompute(base: Path) -> None:
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")


def l1_dir(base: Path) -> Path:
    return base / f"vehicle-{VEHICLE}" / "l1"


def starts(events: list[dict]) -> list[str]:
    return [e["start"] for e in events]


def assert_whole(base: Path) -> dict[str, list[dict]]:
    """L1 holds every trip and refuelling the derivations print from the
    stream, and the periods they print; ``l1 status`` finds nothing due."""
    held, printed = l1(base), derived(base)
    for kind in ("trip", "refuelling"):
        assert starts(held[kind]) == starts(printed[kind]), (kind, held[kind], printed[kind])
    assert ({(p["period"], p["start"]) for p in held["period"]}
            == {(p["period"], p["start"]) for p in printed["period"]}), (held["period"],
                                                                         printed["period"])
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=None)
    assert status.returncode == 0, (status.stdout, status.stderr)
    return held


@pytest.fixture(scope="module")
def written(tmp_path_factory) -> Path:
    """The whole history, logged once for the module."""
    base = tmp_path_factory.mktemp("history") / "vledger"
    base.mkdir()
    begin(base)
    log(base, history())
    return base


@pytest.fixture
def base(written: Path, tmp_path: Path) -> Path:
    """A copy of the whole history in a data directory of its own, with no
    L1 yet."""
    assert not list(written.rglob("l1"))
    return Path(shutil.copytree(written, tmp_path / "vledger"))


def test_the_history_spans_two_months_with_drives_in_both(base):
    """The input is not trivially short: the derivations print trips that
    start in September and in October (as the vehicle's time zone counts
    them), the refuelling, and a month period for each month."""
    printed = derived(base)
    months = {month(t["start"]) for t in printed["trip"]}
    assert {"2026-09", "2026-10"} <= months, printed["trip"]
    assert len(printed["refuelling"]) == 1, printed["refuelling"]
    month_periods = [p for p in printed["period"] if p["period"] == "month"]
    assert len(month_periods) >= 2, printed["period"]


def test_derive_all_write_recomputes_the_whole_history_from_nothing(base):
    """With no L1 at all a rebuild is due; ``derive all --write`` writes it,
    holding every event of both months, and nothing is due any more."""
    assert not l1_dir(base).exists()
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=None)
    assert status.returncode == 1, (status.stdout, status.stderr)
    recompute(base)
    held = assert_whole(base)
    assert {month(t["start"]) for t in held["trip"]} >= {"2026-09", "2026-10"}, held["trip"]


def test_after_l1_clean_the_history_is_recomputed_the_same(base):
    """``l1 clean`` deletes L1; recomputing writes again what was there,
    event for event."""
    recompute(base)
    before = l1(base)
    vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    assert not l1_dir(base).exists()
    recompute(base)
    assert l1(base) == before


def test_a_damaged_l1_is_recomputed_whole(base):
    """L1 with its first trip, an October trip and the refuelling cut out of
    their files — damage no hash in the manifest covers — is recomputed whole
    by ``derive all --write``: every event the files lost is back."""
    recompute(base)
    before = l1(base)
    trips = l1_dir(base) / FILES["trip"]
    lines = trips.read_text(encoding="utf-8").splitlines(keepends=True)
    october = next(i for i, x in enumerate(lines) if month(json.loads(x)["start"]) == "2026-10")
    trips.write_text("".join(x for i, x in enumerate(lines) if i not in (0, october)),
                     encoding="utf-8")
    (l1_dir(base) / FILES["refuelling"]).write_text("", encoding="utf-8")
    damaged = l1(base)
    assert len(damaged["trip"]) == len(before["trip"]) - 2 and not damaged["refuelling"], damaged

    recompute(base)
    assert l1(base) == before
    assert_whole(base)


def test_a_receipt_entered_later_reaches_the_recomputed_history(base):
    """A receipt entered after L1 was written makes a rebuild due; recomputing
    then gives the past refuelling the receipt's quantity, and nothing is due
    any more."""
    recompute(base)
    [fill] = l1(base)["refuelling"]
    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(10.4),
            "--quantity-l", "30.12", "--total-price", "55.40", "--full")
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE, expect=None)
    assert status.returncode == 1, (status.stdout, status.stderr)

    recompute(base)
    [after] = assert_whole(base)["refuelling"]
    assert after["start"] == fill["start"], (fill, after)
    assert after["quantity_l"] == 30.12, after


def test_the_history_can_be_recomputed_with_a_trip_under_way(written, tmp_path):
    """Recomputing in the middle of the October drive works and holds every
    event completed so far — the trip under way is not complete, so L1 does
    not hold it yet. Once the rest of the stream has come, recomputing again
    holds the whole history, as the directory that only ever saw the whole
    stream does."""
    lines = history()
    mid = tmp_path / "mid"
    mid.mkdir()
    begin(mid)
    log(mid, [x for x in lines if x[0] < 30.5])
    recompute(mid)
    early = l1(mid)
    printed = derived(mid)
    done = [t for t in printed["trip"] if instant(t["start"]) < instant(at(30))]
    assert done and starts(early["trip"]) == starts(done), (early["trip"], printed["trip"])
    assert starts(early["refuelling"]) == starts(printed["refuelling"]), early["refuelling"]
    status = vledger(mid, "l1", "status", "--vehicle", VEHICLE, expect=None)
    assert status.returncode == 0, (status.stdout, status.stderr)

    log(mid, [x for x in lines if x[0] >= 30.5])
    recompute(mid)
    late = assert_whole(mid)
    assert any(instant(t["start"]) >= instant(at(29)) for t in late["trip"]), late["trip"]
    assert starts(late["trip"])[:len(done)] == starts(done)

    whole = Path(shutil.copytree(written, tmp_path / "whole"))
    recompute(whole)
    assert l1(mid) == l1(whole)
