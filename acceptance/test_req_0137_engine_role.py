# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0137 with the ``vledger`` command: the role ``engine``, a binary sensor
or an enumeration mapped to the domain state running, refines a trip's start
and never its end, and is never the sole criterion.

Derived from the requirement ("the engine starting is a start marker; it
stopping is never an end marker, since a hybrid drives on electrically …
Never the sole criterion"), the decisions it cites (ADR-0021 point 3:
"``engine`` turning running [is a] start marker … never an end marker";
ADR-0064 point 3), and the user documentation:

- the user guide's *In Home Assistant*: an engine status "is assigned as
  **Engine running** … it can start a trip, never end one, since the engine
  stops while the car drives on electrically"; the **Mapping** step ticks
  the values that mean *engine running*, "*unavailable* and *unknown* hold
  the last known state; any other value means the opposite";
- the user guide's *The derivations*: "A trip is the span between two
  standstills … lock and engine the start only: the start moves back to the
  latest … *engine running* within T_still before the first moving sample";
  "``refined_by`` names the role that set each end";
- the glossary's *Not-driving marker*: "A marker only moves the boundary of
  a trip the movement roles found, by at most T_still, never creates one";
- l0-format.md: the ``config`` line, whose enumerated role carries a ``map``
  of "the source values that mean its one positive domain state", and the
  writing verbs of the user guide's *The raw log*;
- l1-format.md: ``refined_by`` has a ``start`` and an ``end``.

The stream is written the way the integration writes one, with an odometer,
a position and an engine status whose source reports ``Running`` and
``Stopped``; T_still is 30 minutes. The same role assigned in Home Assistant,
as a binary sensor and as an enumeration, is test_req_0137_engine_role_ha.py.
Nothing of ``vledger`` is imported: the command runs in a process of its
own."""

import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime

import pytest

pytestmark = [pytest.mark.req("REQ-0137")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
VEHICLE = "e1c7"
ENGINE = "sensor.car_engine"
HOME = ("51.0000", "7.0000")
ROAD = [("51.0300", "7.0200"), ("51.0600", "7.0500")]
SHOP = ("51.0900", "7.0700")


# --- the vledger command -----------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def jsonl(text):
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def at(minutes):
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    h, m = divmod(6 * 60 + minutes, 60)
    return f"2026-10-05T{h:02d}:{m:02d}:00.000Z"


def instant(text):
    """A time the command wrote, compared by the instant it names, not by
    how it is spelt."""
    return datetime.fromisoformat(text).astimezone(UTC)


CONFIG = {"name": "Hybrid",
          "roles": {"odometer": {"entity": "sensor.car_odometer"},
                    "position": {"entity": "device_tracker.car"},
                    "engine": {"entity": ENGINE, "map": {"running": ["Running"]}}},
          "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600}}


class Car:
    """One vehicle's raw log, written line by line as the integration would
    write it: started an hour before the scenario, parked at home with the
    engine stopped, then whatever the test has it report."""

    def __init__(self, base):
        self.base = base
        snapshot = [
            {"role": "odometer", "entity": "sensor.car_odometer", "state": "1000",
             "unit": "km", "since": at(-120)},
            {"role": "position", "entity": "device_tracker.car", "state": "home",
             "since": at(-120),
             "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]),
                       "gps_accuracy": 0}},
            {"role": "engine", "entity": ENGINE, "state": "Stopped", "since": at(-120)},
        ]
        vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60),
                "--homeassistant", "2026.10.1", "--snapshot", json.dumps(snapshot))
        vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60),
                "--config", json.dumps(CONFIG))

    def engine(self, minutes, state):
        vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", at(minutes),
                "--role", "engine", "--entity", ENGINE, "--state", state)
        return self

    def moves(self, minutes, km, where, zone="not_home"):
        vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", at(minutes),
                "--role", "odometer", "--entity", "sensor.car_odometer",
                "--state", str(km), "--unit", "km")
        vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", at(minutes),
                "--role", "position", "--entity", "device_tracker.car", "--state", zone,
                "--attr", f"latitude={where[0]}", "--attr", f"longitude={where[1]}",
                "--attr", "gps_accuracy=0")
        return self

    def drive(self):
        """To the shop: moving samples at 06:10, 06:20 and 06:30, then parked."""
        return self.moves(10, 1005, ROAD[0]).moves(20, 1012, ROAD[1]).moves(30, 1018, SHOP)

    def trips(self):
        """The trips ``vledger derive trips`` prints, after the stream ran on
        with a heartbeat each hour to 10:00 — long past T_still parked."""
        for minutes in (60, 120, 180, 240):
            vledger(self.base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(minutes),
                    "--lines", "0")
        return jsonl(vledger(self.base, "derive", "trips", "--vehicle", VEHICLE))


@pytest.fixture
def car(tmp_path):
    base = tmp_path / "vledger"
    base.mkdir()
    return Car(base)


def refined(trip, end):
    return (trip.get("refined_by") or {}).get(end)


# --- the tests ----------------------------------------------------------------

def test_without_the_engine_the_trip_spans_its_moving_samples(car):
    """The reference the other tests move from: the engine never reports,
    and the trip runs from the first moving sample to the last."""
    [trip] = car.drive().trips()
    assert instant(trip["start"]) == instant(at(10))
    assert instant(trip["end"]) == instant(at(30))
    assert refined(trip, "start") != "engine"


def test_the_engine_starting_moves_the_start_back_to_it(car):
    """The engine starts at 06:05, five minutes before the first moving
    sample: the trip starts at 06:05, and ``refined_by`` names the engine as
    what set the start."""
    [trip] = car.engine(5, "Running").drive().trips()
    assert instant(trip["start"]) == instant(at(5))
    assert refined(trip, "start") == "engine"
    assert instant(trip["end"]) == instant(at(30))


def test_the_engine_stopping_is_never_the_end_of_a_trip(car):
    """The engine starts at 06:05, stops at 06:15 while the car drives on
    electrically, runs again at 06:25 and stops for good at 06:35, five
    minutes after the last moving sample. Still one trip from 06:05 to the
    last moving sample at 06:30: neither stop ends it, splits it or moves its
    end, and the engine sets no end."""
    car.engine(5, "Running").moves(10, 1005, ROAD[0]).engine(15, "Stopped")
    car.moves(20, 1012, ROAD[1]).engine(25, "Running").moves(30, 1018, SHOP)
    car.engine(35, "Stopped")
    [trip] = car.trips()
    assert instant(trip["start"]) == instant(at(5))
    assert instant(trip["end"]) == instant(at(30))
    assert refined(trip, "end") != "engine"


def test_the_engine_running_without_movement_is_no_trip(car):
    """The engine runs from 06:05 to 06:45 — warming up in the drive — and
    nothing moves: no trip. The engine is never the sole criterion."""
    car.engine(5, "Running").engine(45, "Stopped")
    assert car.trips() == []


def test_an_engine_start_longer_than_T_still_before_the_movement_moves_nothing(car):
    """The engine starts at 05:20, fifty minutes before the first moving
    sample and so more than T_still (30 minutes) before it: the start stays
    at the first moving sample, and the engine sets nothing."""
    [trip] = car.engine(-40, "Running").drive().trips()
    assert instant(trip["start"]) == instant(at(10))
    assert refined(trip, "start") != "engine"


@pytest.mark.parametrize("between, start", [
    ("unavailable", -10),
    ("unknown", -10),
    ("Idle", 5),
], ids=["unavailable-holds", "unknown-holds", "unmapped-is-stopped"])
def test_the_engine_s_source_values_are_read_through_its_mapping(car, between, start):
    """The engine reports ``Running`` at 05:50, then ``between`` at 06:00,
    then ``Running`` again at 06:05, before the first moving sample at 06:10.
    *unavailable* and *unknown* hold running, so the engine started at 05:50
    and the trip starts there; ``Idle``, a value the mapping does not list,
    means stopped, so the engine started anew at 06:05 and the trip starts
    there."""
    car.engine(-10, "Running").engine(0, between).engine(5, "Running")
    [trip] = car.drive().trips()
    assert instant(trip["start"]) == instant(at(start))
    assert refined(trip, "start") == "engine"
