# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0069, as a user meets it: a refuelling candidate has its time; the
position and zone where the vehicle stood; the level before; the level after,
determined only once T_settle (default 6 min) has elapsed; and the sensor
delta, ``estimated``.

Derived from the requirement and the user guide: *The derivations: vledger
derive* ("a pump the sensor sees in several steps is one candidate, from
``start``, the first sample that rose, to ``end``, the last. It carries the
position and zone where the vehicle stood, ``level_before_l``,
``level_after_l`` read once T_settle (6 min) has passed after ``end`` together
with ``settled_at``, the time of that reading, the ``sensor_delta_l`` between
the two — always ``estimated``"; "A candidate still settling is printed with
``level_after_l`` empty"; ``vledger derive refuellings … | jq '{start, zone,
level_before_l, level_after_l}'``) and *Writing* (the configuration's
``thresholds.t_settle_s``).

The guide names no key for the position nor for the sensor delta's quality:
the tests look for a fix of the station's coordinates among the candidate's
values, in the shape a trip's positions have (``latitude``, ``longitude``),
and for ``estimated`` among its qualities (ISSUE-0188).

Black-box: every step is the ``vledger`` command run as a separate process,
on a stream written with ``vledger l0`` as the integration writes it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0069")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
HOME = ("51.0000", "7.0000")
ROAD = ("51.0300", "7.0200")
STATION = ("51.0600", "7.0500")


def at(minutes: int) -> str:
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    h, m = divmod(6 * 60 + minutes, 60)
    return f"2026-10-05T{h:02d}:{m:02d}:00.000Z"


def run(base: Path, *argv: str) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    return subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                          check=False, capture_output=True, text=True, env=environ, cwd=base)


def vledger(base: Path, *argv: str) -> subprocess.CompletedProcess:
    p = run(base, *argv)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p


def lines(p: subprocess.CompletedProcess) -> list[dict]:
    """The JSON lines a verb printed, any summary line beside them left out."""
    return [json.loads(line) for line in p.stdout.splitlines() if line.lstrip().startswith("{")]


def state(base: Path, minutes: int, role: str, value: str, unit: str | None = None,
          **attrs: str) -> None:
    argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", role,
            "--entity", f"sensor.{role}", "--state", value]
    if unit:
        argv += ["--unit", unit]
    for k, v in attrs.items():
        argv += ["--attr", f"{k}={v}"]
    vledger(base, *argv)


def fix(base: Path, minutes: int, where: tuple[str, str], zone: str = "not_home") -> None:
    state(base, minutes, "position", zone, latitude=where[0], longitude=where[1],
          gps_accuracy="0")


def fuel(base: Path, minutes: int, litres: str) -> None:
    state(base, minutes, "fuel_level", litres, "L")


def heartbeat(base: Path, minutes: int) -> None:
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(minutes), "--lines", "0")


def drive_to_the_pump(base: Path, thresholds: dict | None = None) -> None:
    """Home Assistant starts with the car at home, 18 L in the tank; it drives
    to a fuel station and stands there from minute 30, the odometer
    unchanged. ``thresholds`` is the configuration's; without ``t_settle_s``
    the default holds."""
    config = {
        "name": "Test car",
        "roles": {
            "odometer": {"entity": "sensor.odometer"},
            "position": {"entity": "sensor.position"},
            "fuel_level": {"entity": "sensor.fuel_level"},
        },
        "parameters": {"fuel": "petrol"},
        "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, **(thresholds or {})},
    }
    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": "1000", "unit": "km",
         "since": at(-120)},
        {"role": "position", "entity": "sensor.position", "state": "home", "since": at(-120),
         "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]),
                   "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": "18.0", "unit": "L",
         "since": at(-120)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60), "--homeassistant",
            "2026.9.4", "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60), "--config",
            json.dumps(config))
    state(base, 15, "odometer", "1007", "km"), fix(base, 15, ROAD)
    state(base, 30, "odometer", "1015", "km"), fix(base, 30, STATION, zone="fuel_station")
    fuel(base, 35, "18.0")


def refuellings(base: Path) -> list[dict]:
    return lines(vledger(base, "derive", "refuellings", "--vehicle", VEHICLE))


def the_candidate(base: Path) -> dict:
    found = refuellings(base)
    assert len(found) == 1, found
    return found[0]


def empty(value: object) -> bool:
    return value in (None, "")


def test_a_pump_seen_in_two_steps_runs_from_the_first_rise_to_the_last(
        tmp_path: Path) -> None:
    drive_to_the_pump(tmp_path)
    fuel(tmp_path, 38, "30.0")
    fuel(tmp_path, 41, "58.0")
    fuel(tmp_path, 60, "58.0")
    heartbeat(tmp_path, 300)
    candidate = the_candidate(tmp_path)
    assert candidate["start"] == at(38), candidate
    assert candidate["end"] == at(41), candidate
    assert candidate["level_before_l"] == pytest.approx(18.0, abs=0.05), candidate
    assert candidate["level_after_l"] == pytest.approx(58.0, abs=0.05), candidate
    assert candidate["sensor_delta_l"] == pytest.approx(40.0, abs=0.05), candidate


def test_the_candidate_carries_the_position_and_zone_where_the_vehicle_stood(
        tmp_path: Path) -> None:
    drive_to_the_pump(tmp_path)
    fuel(tmp_path, 40, "58.0")
    fuel(tmp_path, 60, "58.0")
    heartbeat(tmp_path, 300)
    candidate = the_candidate(tmp_path)
    assert candidate["zone"] == "fuel_station", candidate
    fixes = [v for v in candidate.values()
             if isinstance(v, dict) and {"latitude", "longitude"} <= v.keys()]
    assert any(f["latitude"] == pytest.approx(float(STATION[0]), abs=1e-4)
               and f["longitude"] == pytest.approx(float(STATION[1]), abs=1e-4)
               for f in fixes), candidate


def test_the_sensor_delta_is_after_minus_before_and_estimated(tmp_path: Path) -> None:
    drive_to_the_pump(tmp_path)
    fuel(tmp_path, 40, "58.0")
    fuel(tmp_path, 60, "58.0")
    heartbeat(tmp_path, 300)
    candidate = the_candidate(tmp_path)
    assert candidate["sensor_delta_l"] == pytest.approx(
        candidate["level_after_l"] - candidate["level_before_l"], abs=0.05), candidate
    qualities = {k: v for k, v in candidate.items() if k.endswith("quality")}
    assert "estimated" in qualities.values(), candidate


def test_within_the_default_six_minutes_the_level_after_is_still_empty(
        tmp_path: Path) -> None:
    """The last rise at minute 40; the stream reaches minute 45, five minutes
    on: the candidate is printed, still settling."""
    drive_to_the_pump(tmp_path)
    fuel(tmp_path, 40, "58.0")
    fuel(tmp_path, 44, "57.5")
    heartbeat(tmp_path, 45)
    candidate = the_candidate(tmp_path)
    assert candidate["start"] == at(40), candidate
    assert candidate["level_before_l"] == pytest.approx(18.0, abs=0.05), candidate
    assert empty(candidate.get("level_after_l")), candidate


def test_after_the_default_six_minutes_the_level_after_is_the_settled_reading(
        tmp_path: Path) -> None:
    """The level reads 58.0 at the last rise and settles to 57.5 at minute 44;
    at minute 46, six minutes after the rise, 57.5 is what stands. The
    stream reaches minute 47."""
    drive_to_the_pump(tmp_path)
    fuel(tmp_path, 40, "58.0")
    fuel(tmp_path, 44, "57.5")
    heartbeat(tmp_path, 47)
    candidate = the_candidate(tmp_path)
    assert candidate["level_after_l"] == pytest.approx(57.5, abs=0.05), candidate
    assert candidate["settled_at"] == at(44), candidate
    assert candidate["sensor_delta_l"] == pytest.approx(39.5, abs=0.05), candidate


def test_a_reading_after_the_settle_time_does_not_change_the_level_after(
        tmp_path: Path) -> None:
    drive_to_the_pump(tmp_path)
    fuel(tmp_path, 40, "58.0")
    fuel(tmp_path, 44, "57.5")
    fuel(tmp_path, 52, "57.0")
    fuel(tmp_path, 60, "57.0")
    heartbeat(tmp_path, 300)
    candidate = the_candidate(tmp_path)
    assert candidate["level_after_l"] == pytest.approx(57.5, abs=0.05), candidate
    assert candidate["settled_at"] == at(44), candidate


def test_a_configured_settle_time_replaces_the_default(tmp_path: Path) -> None:
    """With T_settle at 15 min, ten minutes after the rise the candidate is
    still settling; at minute 55 the reading of minute 52 is what stands."""
    early, late = tmp_path / "early", tmp_path / "late"
    early.mkdir(), late.mkdir()
    for base in (early, late):
        drive_to_the_pump(base, thresholds={"t_settle_s": 900})
        fuel(base, 40, "58.0")
        fuel(base, 44, "57.5")
    heartbeat(early, 50)
    fuel(late, 52, "57.0")
    fuel(late, 60, "57.0")
    heartbeat(late, 300)
    assert empty(the_candidate(early).get("level_after_l")), the_candidate(early)
    candidate = the_candidate(late)
    assert candidate["level_after_l"] == pytest.approx(57.0, abs=0.05), candidate
    assert candidate["settled_at"] == at(52), candidate
    assert candidate["sensor_delta_l"] == pytest.approx(39.0, abs=0.05), candidate
