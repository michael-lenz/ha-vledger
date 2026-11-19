# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0051 with the ``vledger`` command: a trip is the period between two
standstills; a standstill exists when no movement role (odometer, position,
trip counter) changes for at least T_still, 30 minutes by default, which is
configurable per vehicle.

Derived from the requirement ("A trip is the period between two standstills.
A standstill exists when no movement role (odometer, position, trip counter)
changes for at least T_still (default 30 min). T_still is configurable per
vehicle (FZG-06) and must be at least twice the longest sampling interval of
the assigned movement roles; the measured interval is in the diagnostics
(HAI-07)") and from the user documentation:

- the user guide's *The derivations*: "A trip is the span between two
  standstills: from the first sample that moved after at least T_still of
  nothing moving, to the last before the next such span";
- the user guide's *In Home Assistant*: "at least one movement role
  (odometer, position or trip counter) is required"; and its *Writing*: the
  roles ``odometer``, ``position``, ``trip_distance`` and
  ``outside_temperature``, and ``thresholds`` with ``t_still_s``, "A
  parameter or threshold left out reads as its default";
- the user guide's *Periods*: "for the standstill time after a trip (half an
  hour unless configured otherwise)";
- the user guide's *The derivation on disk*: "L1 holds completed events
  only: a trip is written once its standstill has elapsed … judged by the
  stream's last line rather than the clock";
- l0-format.md's ``config``: ``thresholds.t_still_s``.

The lower bound on T_still is the user's to keep when setting it: the user
guide advises it ("set T_still to twice its update interval"), and what
shows the measured interval is the diagnostics, which REQ-0090 (HAI-07)
accepts. The tests here show what the bound is for, in the requirement's
rationale: with T_still at twice the sampling interval a single missed
update does not cut a trip in two, and below it, it does.

The vehicle is sampled, the default. Its stream is written with the verbs of
the user guide's *Writing*, as the integration writes one, and its trips are
read with ``vledger derive trips``. Nothing of ``vledger`` is imported: the
command runs in a process of its own."""

import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.req("REQ-0051")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
T0 = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
HOME = (51.0000, 7.0000)
ODOMETER, POSITION, COUNTER = "sensor.car_odometer", "device_tracker.car", "sensor.car_trip"
TEMPERATURE = "sensor.car_outside_temperature"


def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(text):
    """A time the command wrote, in the spelling of :func:`iso`."""
    return iso(datetime.fromisoformat(text).astimezone(UTC))


def span(trip):
    return instant(trip["start"]), instant(trip["end"])


class Car:
    """A sampled vehicle with the three movement roles — odometer, position,
    trip counter — and an outside temperature, its stream written line by
    line at the minutes given (relative to 06:00 UTC on 5 October 2026)."""

    def __init__(self, base, vehicle="d051"):
        self.base, self.vehicle = base, vehicle
        self.km, self.trip_km, self.step, self.celsius = 1000, 0.0, 0, 12

    def start(self, t_still_min=None, minutes=-120):
        """The start line and the configuration after it, the car parked at
        home. ``t_still_min`` is the vehicle's T_still in minutes; ``None``
        leaves the threshold out, so it reads as its default."""
        since = iso(at(minutes - 10))
        snapshot = [
            {"role": "odometer", "entity": ODOMETER, "state": str(self.km), "unit": "km",
             "since": since},
            {"role": "position", "entity": POSITION, "state": "home", "since": since,
             "attrs": {"latitude": HOME[0], "longitude": HOME[1], "gps_accuracy": 0}},
            {"role": "trip_distance", "entity": COUNTER, "state": str(self.trip_km),
             "unit": "km", "since": since},
            {"role": "outside_temperature", "entity": TEMPERATURE, "state": str(self.celsius),
             "unit": "°C", "since": since},
        ]
        thresholds = {"heartbeat_s": 3600}
        if t_still_min is not None:
            thresholds["t_still_s"] = t_still_min * 60
        config = {"name": f"Car {self.vehicle}",
                  "roles": {"odometer": {"entity": ODOMETER},
                            "position": {"entity": POSITION},
                            "trip_distance": {"entity": COUNTER},
                            "outside_temperature": {"entity": TEMPERATURE}},
                  "thresholds": thresholds}
        self._write("start", minutes, "--homeassistant", "2026.10.1",
                    "--snapshot", json.dumps(snapshot))
        self._write("config", minutes, "--config", json.dumps(config))
        return self

    def _write(self, verb, minutes, *argv):
        vledger(self.base, "l0", verb, "--vehicle", self.vehicle, "--t", iso(at(minutes)), *argv)

    def odometer(self, minutes):
        self.km += 2
        self._write("state", minutes, "--role", "odometer", "--entity", ODOMETER,
                    "--state", str(self.km), "--unit", "km")
        return self

    def position(self, minutes):
        """A fix a couple of kilometres on from the last one."""
        self.step += 1
        lat, lon = HOME[0] + 0.02 * self.step, HOME[1] + 0.02 * self.step
        self._write("state", minutes, "--role", "position", "--entity", POSITION,
                    "--state", "not_home", "--attr", f"latitude={lat}",
                    "--attr", f"longitude={lon}", "--attr", "gps_accuracy=0")
        return self

    def trip_distance(self, minutes):
        self.trip_km += 2
        self._write("state", minutes, "--role", "trip_distance", "--entity", COUNTER,
                    "--state", f"{self.trip_km:.1f}", "--unit", "km")
        return self

    def outside_temperature(self, minutes):
        self.celsius += 3
        self._write("state", minutes, "--role", "outside_temperature", "--entity", TEMPERATURE,
                    "--state", str(self.celsius), "--unit", "°C")
        return self

    def moves(self, minutes):
        """One moving sample: the odometer on and a fix away from the last."""
        return self.odometer(minutes).position(minutes)

    def drives(self, first, last, every=5):
        """Moving samples every ``every`` minutes from ``first`` to ``last``."""
        for m in range(first, last + 1, every):
            self.moves(m)
        return self

    def heartbeat(self, minutes):
        self._write("heartbeat", minutes, "--lines", "0")
        return self

    def trips(self):
        out = vledger(self.base, "derive", "trips", "--vehicle", self.vehicle)
        return [json.loads(x) for x in out.splitlines() if x.strip()]

    def trips_in_l1(self):
        vledger(self.base, "derive", "trips", "--vehicle", self.vehicle, "--write")
        out = vledger(self.base, "l1", "read", "--vehicle", self.vehicle, "--kind", "trip")
        return [json.loads(x) for x in out.splitlines() if x.strip()]


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "vledger"
    b.mkdir()
    return b


# Every test drives from 06:30 to 07:00, stops, and drives again for twenty
# minutes; the heartbeat three hours on has every standstill long elapsed.
FIRST, LAST, LATER = 30, 60, 300


def two_drives(car, stop, every=5):
    """The first drive, a stop of ``stop`` minutes between its last moving
    sample and the next drive's first, and the second drive; the minute the
    second drive starts."""
    again = LAST + stop
    car.drives(FIRST, LAST, every).drives(again, again + 20, every).heartbeat(LATER)
    return again


def test_a_trip_runs_from_the_first_moving_sample_to_the_last(base):
    """One drive between two standstills is one trip, from the first sample
    that moved after the standstill to the last before the next."""
    car = Car(base).start().drives(FIRST, LAST).heartbeat(LATER)
    [trip] = car.trips()
    assert span(trip) == (iso(at(FIRST)), iso(at(LAST)))


@pytest.mark.parametrize("stop", [20, 29])
def test_a_stop_shorter_than_the_default_t_still_is_inside_one_trip(base, stop):
    """With T_still left at its default, 30 minutes, a stop shorter than it
    is no standstill: both drives make one trip."""
    car = Car(base).start()
    again = two_drives(car, stop)
    [trip] = car.trips()
    assert span(trip) == (iso(at(FIRST)), iso(at(again + 20)))


@pytest.mark.parametrize("stop", [31, 45])
def test_a_stop_of_the_default_t_still_or_longer_is_a_standstill(base, stop):
    """With T_still left at its default, a stop of 30 minutes or longer is a
    standstill: it ends the first trip at its last moving sample, and the
    next starts at the first sample that moved after it."""
    car = Car(base).start()
    again = two_drives(car, stop)
    trips = car.trips()
    assert [span(t) for t in trips] == [(iso(at(FIRST)), iso(at(LAST))),
                                         (iso(at(again)), iso(at(again + 20)))]


@pytest.mark.parametrize(("t_still", "stop", "count"), [
    (60, 45, 1),   # longer than the default, so a 45-minute stop stays inside the trip
    (60, 65, 2),
    (15, 20, 2),   # shorter, so a 20-minute stop ends it
    (15, 10, 1),
])
def test_t_still_is_the_vehicle_s_own(base, t_still, stop, count):
    """T_still set in the vehicle's configuration is the one its trips are
    cut by, not the default: a stop is a standstill when it lasts at least
    the vehicle's T_still."""
    car = Car(base).start(t_still_min=t_still)
    two_drives(car, stop)
    assert len(car.trips()) == count, car.trips()


def test_two_vehicles_each_keep_their_own_t_still(base):
    """Two vehicles under one base driving the same day, one with T_still of
    60 minutes and one with 15: the same 40-minute stop is inside one trip of
    the first and a standstill between two trips of the second."""
    slow, quick = Car(base, "a051").start(t_still_min=60), Car(base, "b051").start(t_still_min=15)
    for car in (slow, quick):
        two_drives(car, 40)
    assert (len(slow.trips()), len(quick.trips())) == (1, 2)


@pytest.mark.parametrize("role", ["odometer", "position", "trip_distance"])
def test_a_change_of_any_movement_role_holds_off_the_standstill(base, role):
    """A stop of 40 minutes, longer than T_still, during which one movement
    role alone changes once, 20 minutes in: no movement role went unchanged
    for T_still, so there is no standstill and both drives are one trip."""
    car = Car(base).start(t_still_min=30)
    car.drives(FIRST, LAST)
    getattr(car, role)(LAST + 20)
    car.drives(LAST + 40, LAST + 60).heartbeat(LATER)
    [trip] = car.trips()
    assert span(trip) == (iso(at(FIRST)), iso(at(LAST + 60)))


def test_a_role_that_is_no_movement_role_does_not_hold_off_the_standstill(base):
    """The same stop, but what changes 20 minutes in is the outside
    temperature, which is no movement role: the stop is still a standstill,
    and the drives are two trips."""
    car = Car(base).start(t_still_min=30)
    car.drives(FIRST, LAST).outside_temperature(LAST + 20)
    car.drives(LAST + 40, LAST + 60).heartbeat(LATER)
    trips = car.trips()
    assert [span(t) for t in trips] == [(iso(at(FIRST)), iso(at(LAST))),
                                         (iso(at(LAST + 40)), iso(at(LAST + 60)))]


def test_a_trip_is_completed_once_its_standstill_has_elapsed(base):
    """A trip ends with a standstill, so L1 has it only once T_still has
    passed after its last moving sample: twenty minutes after it, with
    T_still of 30, nothing; forty minutes after it, the trip."""
    car = Car(base).start(t_still_min=30).drives(FIRST, LAST).heartbeat(LAST + 20)
    assert car.trips_in_l1() == []
    car.heartbeat(LAST + 40)
    [trip] = car.trips_in_l1()
    assert span(trip) == (iso(at(FIRST)), iso(at(LAST)))


@pytest.mark.parametrize(("t_still", "count"), [(30, 1), (15, 2)])
def test_a_missed_update_cuts_the_trip_only_below_twice_the_sampling_interval(
        base, t_still, count):
    """The requirement's rationale: a vehicle sampled every 10 minutes while
    it drives, one of whose updates, at 06:50, never came — 20 minutes
    between two moving samples. With T_still at 30 minutes, at least twice
    the sampling interval, the drive stays one trip; at 15, below it, the
    missed update cuts it in two."""
    car = Car(base).start(t_still_min=t_still)
    for m in (30, 40, 60, 70, 80):
        car.moves(m)
    car.heartbeat(LATER)
    assert len(car.trips()) == count, car.trips()
