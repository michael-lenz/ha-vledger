# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0136 with the ``vledger`` command: the role ``lock`` — a lock, a
binary sensor or an enumeration, mapped to *locked* — refines a trip's start
and never its end, and is never the sole criterion.

Derived from the requirement ("Role ``lock``, expected type: lock, binary
sensor or enumeration, mapped to the domain state locked (FZG-05). Enables
the refinement of a trip's start: unlocking is a start marker, and a door
when no sign of driving follows within T_still (ADR-0050, ADR-0057);
locking is never an end marker (ADR-0021). Never the sole criterion
(FAH-02)"), from ADR-0021 points 1 and 3, and from the user documentation:

- the user guide's *In Home Assistant*: "A **Lock** likewise only starts
  one — the car locks itself on driving off"; the mapping ticks "the
  source's values that mean … *locked* …. *unavailable* and *unknown* hold
  the last known state; any other value means the opposite — … unlocked";
- *The derivations*: "Ignition, plug state and charging state, where
  assigned, refine both ends, lock and engine the start only: the start
  moves back to the latest … *unlocked* … within T_still before the first
  moving sample, the end forward to the earliest *ignition off*, *plugged
  in* or *charging* within T_still after the last"; "``refined_by`` names
  the role that set each end";
- the glossary's *Not-driving marker*: "*unlocked* … never end a trip, since
  a car locks itself on driving off"; "A marker only moves the boundary of a
  trip the movement roles found, by at most T_still, never creates one";
- l0-format.md's ``config``: a role's ``entity`` and an enumerated role's
  ``map`` of "the source values that mean its one positive domain state",
  and ``thresholds.t_still_s``.

The vehicle here is sampled, the default. The rule that an unlock followed
by no sign of driving was a door is the user guide's for a vehicle set to
report *per driving cycle*, and the documentation does not say how a
stream's configuration declares one (ISSUE-0159), so no test here shows it.

The stream is written with the verbs of the user guide's *Writing*, as the
integration writes one, and the trips are read with ``vledger derive
trips``. Nothing of ``vledger`` is imported: the command runs in a process of
its own."""

import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.req("REQ-0136")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
VEHICLE = "c4e2"
T0 = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
T_STILL_MIN = 30
HOME = (51.0000, 7.0000)

# The three types the requirement names, each with the source values it
# reports for locked and unlocked and the mapping a user ticks: a lock entity
# reports ``locked``; a binary sensor of the lock device class is ``on`` when
# unlocked, so ``off`` is ticked as locked; an enumeration has a vocabulary
# of its own.
TYPES = {
    "lock": ("lock.car_doors", "locked", "unlocked", ["locked"]),
    "binary_sensor": ("binary_sensor.car_doors", "off", "on", ["off"]),
    "enumeration": ("sensor.car_lock_status", "Locked", "Unlocked", ["Locked"]),
}


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


class Car:
    """A sampled vehicle with an odometer, a position and a lock of one of
    the three types, its stream written line by line at the minutes given
    (relative to 06:00 UTC on 5 October 2026)."""

    def __init__(self, base, kind="lock"):
        self.base = base
        self.entity, self.locked, self.unlocked, self.ticked = TYPES[kind]
        self.km = 1000
        self.step = 0

    def start(self, minutes=-120, locked_at_start=False):
        """The start line and the configuration after it. The car is parked
        at home; by default it is still unlocked at the start, from getting
        out, and locks five minutes later, so the lock's state before the
        drive is a line of its own. ``locked_at_start`` has it locked
        already, so the start line's snapshot is all the stream holds of
        the lock before the drive."""
        since = iso(at(minutes - 10))
        snapshot = [
            {"role": "odometer", "entity": "sensor.car_odometer", "state": str(self.km),
             "unit": "km", "since": since},
            {"role": "position", "entity": "device_tracker.car", "state": "home", "since": since,
             "attrs": {"latitude": HOME[0], "longitude": HOME[1], "gps_accuracy": 0}},
            {"role": "lock", "entity": self.entity,
             "state": self.locked if locked_at_start else self.unlocked, "since": since},
        ]
        config = {"name": "Test car",
                  "roles": {"odometer": {"entity": "sensor.car_odometer"},
                            "position": {"entity": "device_tracker.car"},
                            "lock": {"entity": self.entity, "map": {"locked": self.ticked}}},
                  "thresholds": {"t_still_s": T_STILL_MIN * 60, "heartbeat_s": 3600}}
        vledger(self.base, "l0", "start", "--vehicle", VEHICLE, "--t", iso(at(minutes)),
                "--homeassistant", "2026.10.1", "--snapshot", json.dumps(snapshot))
        vledger(self.base, "l0", "config", "--vehicle", VEHICLE, "--t", iso(at(minutes)),
                "--config", json.dumps(config))
        return self if locked_at_start else self.locks(minutes + 5)

    def lock(self, minutes, state):
        vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", iso(at(minutes)),
                "--role", "lock", "--entity", self.entity, "--state", state)
        return self

    def unlocks(self, minutes):
        return self.lock(minutes, self.unlocked)

    def locks(self, minutes):
        return self.lock(minutes, self.locked)

    def moves(self, minutes):
        """One moving sample: the odometer two kilometres on, and a fix away
        from home."""
        self.km += 2
        self.step += 1
        lat, lon = HOME[0] + 0.01 * self.step, HOME[1] + 0.01 * self.step
        vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", iso(at(minutes)),
                "--role", "odometer", "--entity", "sensor.car_odometer",
                "--state", str(self.km), "--unit", "km")
        vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", iso(at(minutes)),
                "--role", "position", "--entity", "device_tracker.car", "--state", "not_home",
                "--attr", f"latitude={lat}", "--attr", f"longitude={lon}",
                "--attr", "gps_accuracy=0")
        return self

    def drives(self, first, last, every=5):
        """Moving samples every ``every`` minutes from ``first`` to ``last``."""
        for m in range(first, last + 1, every):
            self.moves(m)
        return self

    def heartbeat(self, minutes):
        vledger(self.base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", iso(at(minutes)),
                "--lines", "0")
        return self

    def trips(self):
        out = vledger(self.base, "derive", "trips", "--vehicle", VEHICLE)
        return [json.loads(x) for x in out.splitlines() if x.strip()]


def refined(trip, end):
    return (trip.get("refined_by") or {}).get(end)


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "vledger"
    b.mkdir()
    return b


# The drive of every test: moving samples from 06:30 to 07:00, and the
# standstill after it long elapsed by the heartbeat at 09:00.
FIRST, LAST, LATER = 30, 60, 180


def test_without_a_change_of_the_lock_the_trip_is_the_movement(base):
    """The reference: the car locked at 04:05 and not unlocked since, a
    drive from 06:30 to 07:00 — one trip from the first moving sample to the
    last, neither end set by the lock."""
    car = Car(base).start().drives(FIRST, LAST).heartbeat(LATER)
    [trip] = car.trips()
    assert (instant(trip["start"]), instant(trip["end"])) == (iso(at(FIRST)), iso(at(LAST)))
    assert refined(trip, "start") != "lock" and refined(trip, "end") != "lock"


@pytest.mark.parametrize("kind", sorted(TYPES))
def test_unlocking_before_the_drive_moves_the_trip_start_back_to_it(base, kind):
    """A lock, a binary sensor or an enumeration, each mapped to locked:
    unlocked at 06:26, four minutes before the first moving sample — the trip
    starts at the unlock, and ``refined_by`` names ``lock`` for its start."""
    car = Car(base, kind).start().unlocks(FIRST - 4).drives(FIRST, LAST).heartbeat(LATER)
    [trip] = car.trips()
    assert instant(trip["start"]) == iso(at(FIRST - 4))
    assert refined(trip, "start") == "lock"
    assert instant(trip["end"]) == iso(at(LAST))


def test_the_latest_unlock_within_t_still_before_the_drive_is_the_start(base):
    """Unlocked at 06:10, locked again at 06:12, unlocked at 06:24: the start
    moves back to the latest unlock, 06:24."""
    car = (Car(base).start().unlocks(FIRST - 20).locks(FIRST - 18).unlocks(FIRST - 6)
           .drives(FIRST, LAST).heartbeat(LATER))
    [trip] = car.trips()
    assert instant(trip["start"]) == iso(at(FIRST - 6))
    assert refined(trip, "start") == "lock"


def test_an_unlock_more_than_t_still_before_the_drive_does_not_move_the_start(base):
    """Unlocked at 05:55, 35 minutes before the first moving sample with
    T_still at 30: a marker moves a boundary by at most T_still, so the trip
    starts at its first moving sample, not set by the lock."""
    car = Car(base).start().unlocks(FIRST - T_STILL_MIN - 5).drives(FIRST, LAST).heartbeat(LATER)
    [trip] = car.trips()
    assert instant(trip["start"]) == iso(at(FIRST))
    assert refined(trip, "start") != "lock"


def test_the_car_locking_itself_on_driving_off_does_not_end_the_trip(base):
    """Unlocked at 06:26, locked by the car itself at 06:32 while it drives
    on: one trip, from the unlock to the last moving sample."""
    car = (Car(base).start().unlocks(FIRST - 4).drives(FIRST, FIRST)
           .locks(FIRST + 2).drives(FIRST + 5, LAST).heartbeat(LATER))
    [trip] = car.trips()
    assert (instant(trip["start"]), instant(trip["end"])) == (iso(at(FIRST - 4)), iso(at(LAST)))


def test_locking_after_the_arrival_is_never_an_end_marker(base):
    """Locked at 07:03, three minutes after the last moving sample: an end
    marker within T_still would move the end forward to it; the lock is
    none, so the trip ends at its last moving sample, its end not set by
    the lock."""
    car = (Car(base).start().unlocks(FIRST - 4).drives(FIRST, LAST)
           .locks(LAST + 3).heartbeat(LATER))
    [trip] = car.trips()
    assert instant(trip["end"]) == iso(at(LAST))
    assert refined(trip, "end") != "lock"


def test_the_lock_alone_makes_no_trip(base):
    """The car unlocked and locked again three times in a morning, with no
    movement role changing: no trip — the lock is never the sole criterion."""
    car = (Car(base).start().unlocks(10).locks(14).unlocks(70).locks(75)
           .unlocks(130).heartbeat(LATER).locks(200).heartbeat(LATER + 120))
    assert car.trips() == []


def test_the_lock_alone_adds_no_trip_beside_a_drive(base):
    """A drive from 06:30 to 07:00 and, hours later, the car unlocked and
    locked again without moving: still the one trip."""
    car = (Car(base).start().unlocks(FIRST - 4).drives(FIRST, LAST).locks(LAST + 3)
           .heartbeat(LATER).unlocks(LATER + 30).locks(LATER + 34).heartbeat(LATER + 180))
    [trip] = car.trips()
    assert instant(trip["start"]) == iso(at(FIRST - 4))


@pytest.mark.parametrize("dropout", ["unavailable", "unknown"])
def test_a_dropout_of_the_lock_holds_locked_and_is_no_unlock(base, dropout):
    """The lock reporting ``unavailable`` or ``unknown`` at 06:24 and
    ``locked`` again at 06:27: the dropout holds the last known state,
    locked, so nothing was unlocked and the trip starts at its first moving
    sample."""
    car = (Car(base).start().lock(FIRST - 6, dropout).locks(FIRST - 3)
           .drives(FIRST, LAST).heartbeat(LATER))
    [trip] = car.trips()
    assert instant(trip["start"]) == iso(at(FIRST))
    assert refined(trip, "start") != "lock"


def test_a_value_not_ticked_as_locked_means_unlocked(base):
    """A lock entity reporting ``unlocking`` at 06:26, a value the mapping
    does not tick as locked: it means unlocked, and the trip starts there."""
    car = Car(base, "lock").start().lock(FIRST - 4, "unlocking").drives(FIRST, LAST).heartbeat(LATER)
    [trip] = car.trips()
    assert instant(trip["start"]) == iso(at(FIRST - 4))
    assert refined(trip, "start") == "lock"


@pytest.mark.xfail(strict=True, reason="ISSUE-0160")
def test_the_first_unlock_after_a_start_is_a_start_marker(base):
    """Home Assistant started at 04:00 with the car already parked locked,
    so the start line's snapshot is the lock's last state; unlocked at 06:26,
    four minutes before the first moving sample. The unlock is a change from
    locked as much as one after a lock line is: the trip starts at 06:26,
    ``refined_by`` naming ``lock``."""
    car = (Car(base).start(locked_at_start=True).unlocks(FIRST - 4)
           .drives(FIRST, LAST).heartbeat(LATER))
    [trip] = car.trips()
    assert instant(trip["start"]) == iso(at(FIRST - 4))
    assert refined(trip, "start") == "lock"
