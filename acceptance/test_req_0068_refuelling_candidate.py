# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0068, as a user meets it: a refuelling candidate arises when the fuel
quantity rises by at least the refuelling threshold (default 3 L) within one
sampling interval while the odometer is unchanged; a fuel level in % is
converted via the tank capacity.

Derived from the requirement and the user guide: *The derivations: vledger
derive* ("A refuelling candidate is a rise of the fuel level by at least the
refuelling threshold (3 L) between two samples while the odometer stood";
``level_before_l``, ``level_after_l`` read once T_settle has passed after
``end``; "A fuel level in % is converted with the tank capacity; without one,
nothing is detected, and the verb says why on stderr"), *In Home Assistant*
("the tank capacity is required when the fuel level is reported in percent")
and *Writing* (the configuration's ``parameters.tank_capacity_l`` and
``thresholds.refuel_threshold_l``).

Black-box: every step is the ``vledger`` command run as a separate process,
on a stream written with ``vledger l0`` as the integration writes it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0068")]

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


def begin(base: Path, fuel: str, unit: str, parameters: dict | None = None,
          thresholds: dict | None = None) -> None:
    """Home Assistant starts with the car at home and writes its configuration."""
    config = {
        "name": "Test car",
        "roles": {
            "odometer": {"entity": "sensor.odometer"},
            "position": {"entity": "sensor.position"},
            "fuel_level": {"entity": "sensor.fuel_level"},
        },
        "parameters": {"fuel": "petrol", **(parameters or {})},
        "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360,
                       **(thresholds or {})},
    }
    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": "1000", "unit": "km",
         "since": at(-120)},
        {"role": "position", "entity": "sensor.position", "state": "home", "since": at(-120),
         "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]),
                   "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": fuel, "unit": unit,
         "since": at(-120)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60), "--homeassistant",
            "2026.9.4", "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60), "--config",
            json.dumps(config))


def end_of_day(base: Path) -> None:
    """Quiet until long after every settle time has passed."""
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(300), "--lines", "0")


def stop_at_the_pump(base: Path, before: str, after: str, unit: str = "L",
                     parameters: dict | None = None,
                     thresholds: dict | None = None) -> None:
    """A drive to a fuel station; standing there, the odometer reports no
    change, and the fuel level reads ``before`` and, one sample later,
    ``after``."""
    begin(base, before, unit, parameters, thresholds)
    state(base, 15, "odometer", "1007", "km"), fix(base, 15, ROAD)
    state(base, 30, "odometer", "1015", "km"), fix(base, 30, STATION, zone="fuel_station")
    state(base, 35, "fuel_level", before, unit)
    state(base, 40, "fuel_level", after, unit)
    state(base, 60, "fuel_level", after, unit)
    end_of_day(base)


def refuellings(base: Path) -> list[dict]:
    return lines(vledger(base, "derive", "refuellings", "--vehicle", VEHICLE))


def test_a_rise_of_the_default_threshold_or_more_at_a_standing_odometer_is_a_candidate(
        tmp_path: Path) -> None:
    stop_at_the_pump(tmp_path, "18.0", "58.0")
    found = refuellings(tmp_path)
    assert len(found) == 1, found
    candidate = found[0]
    assert candidate["level_before_l"] == pytest.approx(18.0, abs=0.05), candidate
    assert candidate["level_after_l"] == pytest.approx(58.0, abs=0.05), candidate
    assert candidate["sensor_delta_l"] == pytest.approx(40.0, abs=0.05), candidate


def test_a_rise_of_exactly_three_litres_is_a_candidate(tmp_path: Path) -> None:
    stop_at_the_pump(tmp_path, "20.0", "23.0")
    found = refuellings(tmp_path)
    assert len(found) == 1, found
    assert found[0]["sensor_delta_l"] == pytest.approx(3.0, abs=0.05), found


def test_a_rise_below_the_default_threshold_is_no_candidate(tmp_path: Path) -> None:
    stop_at_the_pump(tmp_path, "20.0", "22.5")
    assert refuellings(tmp_path) == []


def test_the_configured_threshold_replaces_the_default(tmp_path: Path) -> None:
    below, above = tmp_path / "below", tmp_path / "above"
    below.mkdir(), above.mkdir()
    stop_at_the_pump(below, "20.0", "24.0", thresholds={"refuel_threshold_l": 5})
    stop_at_the_pump(above, "20.0", "25.5", thresholds={"refuel_threshold_l": 5})
    assert refuellings(below) == []
    found = refuellings(above)
    assert len(found) == 1, found
    assert found[0]["sensor_delta_l"] == pytest.approx(5.5, abs=0.05), found


def test_a_rise_while_the_odometer_moves_is_no_candidate(tmp_path: Path) -> None:
    """The level rises between two samples while the odometer counts on, in one
    drive with no stop in it: the odometer did not stand."""
    begin(tmp_path, "30.0", "L")
    for minute in range(5, 65, 5):
        state(tmp_path, minute, "odometer", str(1000 + minute), "km")
    state(tmp_path, 18, "fuel_level", "30.0", "L")
    state(tmp_path, 23, "fuel_level", "40.0", "L")
    state(tmp_path, 60, "fuel_level", "40.0", "L")
    end_of_day(tmp_path)
    assert refuellings(tmp_path) == []


def test_a_level_in_percent_is_converted_with_the_tank_capacity(tmp_path: Path) -> None:
    stop_at_the_pump(tmp_path, "20", "80", unit="%", parameters={"tank_capacity_l": 50})
    found = refuellings(tmp_path)
    assert len(found) == 1, found
    candidate = found[0]
    assert candidate["level_before_l"] == pytest.approx(10.0, abs=0.05), candidate
    assert candidate["level_after_l"] == pytest.approx(40.0, abs=0.05), candidate
    assert candidate["sensor_delta_l"] == pytest.approx(30.0, abs=0.05), candidate


def test_the_threshold_applies_to_the_litres_a_percent_rise_makes(tmp_path: Path) -> None:
    """5 % of a 50 L tank is 2.5 L, below the 3 L threshold; read as litres the
    same rise would be one."""
    stop_at_the_pump(tmp_path, "20", "25", unit="%", parameters={"tank_capacity_l": 50})
    assert refuellings(tmp_path) == []


def test_a_level_in_percent_without_a_tank_capacity_detects_nothing_and_says_why(
        tmp_path: Path) -> None:
    stop_at_the_pump(tmp_path, "20", "80", unit="%")
    p = run(tmp_path, "derive", "refuellings", "--vehicle", VEHICLE)
    assert lines(p) == [], p.stdout
    assert p.stderr.strip(), "the verb says on stderr why nothing was detected"
