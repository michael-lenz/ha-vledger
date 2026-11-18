# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0046, as a user meets it: events that span a capture gap that could
hide a standstill are flagged ``incomplete`` — any gap inside a sampled
vehicle's trip, a gap of T_still or longer inside a per-cycle vehicle's trip
or a charging session; a shorter gap inside a per-cycle trip or a charging
session is counted on the event (``gaps``) and leaves it ``measured``; there
is no interpolation across gaps.

Derived from the requirement, from ADR-0054 which it cites (point 1, the
rule; point 2, "Trips and charging sessions carry ``gaps``, the number of
capture gaps inside them, whatever their length"), and from the user
documentation:

- the user guide's *The derivations*: "A trip that spans a capture gap is
  ``incomplete`` … A vehicle that reports per driving cycle, and a charging
  session of any vehicle, is ``incomplete`` only for a gap of T_still or
  longer; a shorter one — a restart of a minute — is counted in the event's
  ``gaps`` and changes nothing. Nothing is read across a gap: the distance
  driven inside one belongs to no trip"; "A session across a capture gap of
  T_still or longer is ``incomplete``; ``gaps`` counts every gap inside it";
  a vehicle set to report per driving cycle "is read as legs instead: each
  from its departure to the upload at its stop";
- its *Writing* (the ``config`` line): ``parameters.movement_reporting`` (``sampled``
  or ``per_cycle``), and the role ``engine`` whose ``map`` lists the values
  that mean ``running``;
- the glossary's *Leg* (a departure — an unlock, an engine start, the trip
  counter's reset — to the arrival, the upload at the stop) and *Quality
  flag*;
- README.md: "nothing is interpolated across one";
- l0-format.md's *Gaps*: an orderly stop and the next start is a gap of
  reason ``stopped``; l1-format.md: event files hold completed events, with
  the keys exactly as ``vledger derive`` prints them.

Each scenario is written with ``vledger l0`` as the integration writes a
stream, a capture gap being an orderly ``l0 stop`` and the next ``l0 start``
whose snapshot holds what Home Assistant kept through it. The events are
read as ``vledger derive`` prints them and as L1 holds them after
``derive all --write``. Nothing of ``vledger`` is imported: the command runs
in a process of its own."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0046")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "g046"
DAY = datetime(2026, 10, 5, tzinfo=UTC)
T_STILL_S = 1800
SHORT = 1           # minutes: a restart of a minute, well short of T_still
LONG = 40           # minutes: longer than T_still (30)

HOME = (51.0000, 7.0000)
AWAY = (51.2000, 7.3000)

ODOMETER = "sensor.odometer"
POSITION = "device_tracker.car"
SOC = "sensor.soc"
CHARGING = "sensor.charging_state"
ENGINE = "sensor.engine"


def at(minutes: float) -> str:
    """A time of the scenario: minutes after midnight UTC on 5 October 2026."""
    return (DAY + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def vledger(base: Path, *argv: str) -> str:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def fix(where) -> list[str]:
    return ["--attr", f"latitude={where[0]}", "--attr", f"longitude={where[1]}",
            "--attr", "gps_accuracy=0"]


class Car:
    """A vehicle's stream, written line by line at minutes after midnight.

    ``reporting`` is ``sampled`` (odometer and position while driving) or
    ``per_cycle`` (uploaded once at the stop, the drive's departure an
    engine start). The car carries an odometer, a position, an engine
    status, a SoC and a charging state; no charge point is configured and no
    battery capacity is set, so a charging session is one measured from the
    vehicle's own sensors."""

    def __init__(self, base: Path, reporting: str):
        self.base = base
        self.reporting = reporting
        self.state = {"odometer": ("1000", "km", ODOMETER, HOME),
                      "position": ("home", None, POSITION, HOME),
                      "engine": ("off", None, ENGINE, None),
                      "soc": ("40", "%", SOC, None),
                      "charging_state": ("idle", None, CHARGING, None)}
        self.since = {role: at(-10) for role in self.state}

    def snapshot(self) -> str:
        entries = []
        for role, (value, unit, entity, where) in self.state.items():
            entry = {"role": role, "entity": entity, "state": value, "since": self.since[role]}
            if unit:
                entry["unit"] = unit
            if where:
                entry["attrs"] = {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}
            entries.append(entry)
        return json.dumps(entries)

    def begin(self, minutes: float = 0) -> "Car":
        config = {
            "name": "Gap car",
            "roles": {
                "odometer": {"entity": ODOMETER},
                "position": {"entity": POSITION},
                "engine": {"entity": ENGINE, "map": {"running": ["on"]}},
                "soc": {"entity": SOC},
                "charging_state": {"entity": CHARGING, "map": {"charging": ["charging"]}},
            },
            "parameters": {"fuel": "petrol", "movement_reporting": self.reporting},
            "thresholds": {"t_still_s": T_STILL_S, "heartbeat_s": 3600},
            "time_zone": "Europe/Berlin",
        }
        self.start(minutes)
        vledger(self.base, "l0", "config", "--vehicle", VEHICLE, "--t", at(minutes),
                "--config", json.dumps(config))
        return self

    def start(self, minutes: float) -> "Car":
        vledger(self.base, "l0", "start", "--vehicle", VEHICLE, "--t", at(minutes),
                "--homeassistant", "2026.10.1", "--snapshot", self.snapshot())
        return self

    def stop(self, minutes: float) -> "Car":
        vledger(self.base, "l0", "stop", "--vehicle", VEHICLE, "--t", at(minutes),
                "--reason", "shutdown")
        return self

    def gap(self, minutes: float, length: float) -> "Car":
        """Capture off from ``minutes`` for ``length`` minutes: an orderly
        stop, and the next start with what Home Assistant kept."""
        return self.stop(minutes).start(minutes + length)

    def report(self, minutes: float, role: str, value: str, where=None) -> "Car":
        _, unit, entity, _ = self.state[role]
        self.state[role] = (value, unit, entity, where)
        self.since[role] = at(minutes)
        argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", role,
                "--entity", entity, "--state", value]
        if unit:
            argv += ["--unit", unit]
        if where:
            argv += fix(where)
        vledger(self.base, *argv)
        return self

    def at_km(self, minutes: float, km: int, where) -> "Car":
        """The odometer and the position, reported together."""
        self.report(minutes, "odometer", str(km))
        return self.report(minutes, "position", "home" if where == HOME else "not_home", where)

    def heartbeats(self, first: float, last: float, every: float = 30) -> "Car":
        m = first
        while m <= last:
            vledger(self.base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(m),
                    "--lines", "0")
            m += every
        return self

    def derived(self, kind: str) -> list[dict]:
        return jsonl(vledger(self.base, "derive", kind, "--vehicle", VEHICLE))

    def l1(self, kind: str) -> list[dict]:
        vledger(self.base, "derive", "all", "--vehicle", VEHICLE, "--write")
        return jsonl(vledger(self.base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind))

    def events(self, kind: str) -> list[tuple[str, list[dict]]]:
        """``kind`` (``trips`` or ``charging``) as derived and as L1 holds it."""
        l1_kind = {"trips": "trip", "charging": "charging"}[kind]
        return [("derive", self.derived(kind)), ("l1", self.l1(l1_kind))]


def overlapping(events: list[dict], first: float, last: float) -> list[dict]:
    """The events that overlap the minutes ``first`` to ``last``."""
    lo, hi = instant(at(first)), instant(at(last))
    return [e for e in events if instant(e["start"]) <= hi and instant(e["end"]) >= lo]


# --- A sampled vehicle's trip: any gap makes it incomplete -------------------

def along(i: int):
    """The ``i``-th of twelve steps from home to the place away."""
    return (HOME[0] + (AWAY[0] - HOME[0]) * i / 12, HOME[1] + (AWAY[1] - HOME[1]) * i / 12)


def sampled_drive(base: Path, gap_minutes: float) -> Car:
    """A sampled drive from 06:00 to 07:00, a moving sample every five
    minutes, capture off for ``gap_minutes`` from 06:30. The car drives on
    through the gap: the start after it finds the odometer and the position
    changed inside it, as Home Assistant kept them. Parked away after, the
    standstill elapsed by the heartbeats until 09:00.

    The car moves inside the gap on purpose: a gap the odometer held through
    with no movement inside it is no gap to the trip derivation (ADR-0037),
    which the user documentation does not say (ISSUE-0176)."""
    car = Car(base, "sampled").begin().heartbeats(30, 330)
    for i, m in enumerate(range(360, 421, 5)):
        if 390 <= m < 390 + gap_minutes:
            continue
        car.at_km(m, 1000 + 2 * i, along(i) if i else HOME)
        if m == 385:
            car.stop(390)
            inside = 390 + gap_minutes / 2
            car.state["odometer"] = ("1013", "km", ODOMETER, None)
            car.state["position"] = ("not_home", None, POSITION, along(6.5))
            car.since["odometer"] = car.since["position"] = at(inside)
            car.start(390 + gap_minutes)
    return car.heartbeats(450, 540)


@pytest.mark.parametrize("gap_minutes", [SHORT, 15])
def test_a_sampled_vehicles_trip_across_any_gap_is_incomplete(tmp_path, gap_minutes):
    """A sampled vehicle's drive that capture was off for a minute of — or a
    quarter of an hour, both shorter than T_still — is ``incomplete``:
    whatever trips the drive yields, each one the gap falls in is."""
    car = sampled_drive(tmp_path, gap_minutes)
    for where, trips in car.events("trips"):
        assert trips, (where, trips)
        across = overlapping(trips, 390, 390 + gap_minutes)
        assert across, (where, trips)
        for trip in across:
            assert trip["quality"] == "incomplete", (where, trip)


def test_a_sampled_vehicles_trip_without_a_gap_is_not_incomplete(tmp_path):
    """The reference: the same drive with capture on throughout is one trip
    that is not ``incomplete``, and counts no gap."""
    car = Car(tmp_path, "sampled").begin().heartbeats(30, 330)
    for i, m in enumerate(range(360, 421, 5)):
        car.at_km(m, 1000 + 2 * i, along(i) if i else HOME)
    car.heartbeats(450, 540)
    for where, trips in car.events("trips"):
        assert len(trips) == 1, (where, trips)
        assert trips[0]["quality"] != "incomplete", (where, trips[0])
        assert trips[0]["gaps"] == 0, (where, trips[0])


# --- A per-cycle vehicle's trip: T_still or longer ---------------------------

def per_cycle_drive(base: Path, gap_minutes: float) -> Car:
    """A per-cycle vehicle at home, its odometer at 1000 km: the engine
    starts at 06:00 — the departure — capture is off for ``gap_minutes``
    from 06:20, and at 07:30 the engine stops and the odometer and position
    are uploaded at the stop, 1040 km, away — the arrival. Parked after,
    the standstill elapsed by the heartbeats until 09:30."""
    car = Car(base, "per_cycle").begin().heartbeats(30, 330)
    car.report(360, "engine", "on")
    car.gap(380, gap_minutes)
    car.heartbeats(420, 420)                # capture is alive while the car drives
    car.report(450, "engine", "off")
    car.at_km(450, 1040, AWAY)
    return car.heartbeats(480, 570)


def test_a_per_cycle_trip_across_a_short_gap_is_measured_and_counts_it(tmp_path):
    """A per-cycle drive that a one-minute restart fell into is one trip,
    ``measured``, with ``gaps`` one: the gap is too short to hide a stop."""
    car = per_cycle_drive(tmp_path, SHORT)
    for where, trips in car.events("trips"):
        assert len(trips) == 1, (where, trips)
        trip = trips[0]
        assert trip["quality"] == "measured", (where, trip)
        assert trip["gaps"] == 1, (where, trip)
        assert trip["distance_km"] == pytest.approx(40), (where, trip)


def test_a_per_cycle_trip_across_a_gap_of_t_still_or_longer_is_incomplete(tmp_path):
    """The same drive with capture off for 40 minutes, longer than T_still:
    every trip the gap falls in is ``incomplete``."""
    car = per_cycle_drive(tmp_path, LONG)
    for where, trips in car.events("trips"):
        across = overlapping(trips, 380, 380 + LONG)
        assert across, (where, trips)
        for trip in across:
            assert trip["quality"] == "incomplete", (where, trip)


# --- A charging session: T_still or longer -----------------------------------

def charge(base: Path, reporting: str, gap_minutes: float) -> Car:
    """A charge away from any charge point, from 09:00 to 11:00, the SoC
    from 40 to 80 % in steps every quarter of an hour, capture off for
    ``gap_minutes`` from 09:50 (none for 0). Heartbeats until 13:00."""
    car = Car(base, reporting).begin().heartbeats(30, 510)
    car.report(540, "charging_state", "charging")
    soc = 40
    for m in range(555, 661, 15):
        if gap_minutes and 590 <= m < 590 + gap_minutes:
            continue
        soc += 5
        car.report(m, "soc", str(soc))
        if gap_minutes and m == 585:
            car.gap(590, gap_minutes)
    car.report(660, "soc", "80")
    car.report(660, "charging_state", "idle")
    return car.heartbeats(690, 780)


@pytest.mark.parametrize("reporting", ["sampled", "per_cycle"])
def test_a_charge_across_a_short_gap_is_measured_and_counts_it(tmp_path, reporting):
    """A charge that a one-minute restart fell into is one session,
    ``measured``, with ``gaps`` one — on a sampled vehicle as on a
    per-cycle one."""
    car = charge(tmp_path, reporting, SHORT)
    for where, sessions in car.events("charging"):
        assert len(sessions) == 1, (where, sessions)
        session = sessions[0]
        assert session["quality"] == "measured", (where, session)
        assert session["gaps"] == 1, (where, session)


@pytest.mark.parametrize("reporting", ["sampled", "per_cycle"])
def test_a_charge_across_a_gap_of_t_still_or_longer_is_incomplete(tmp_path, reporting):
    """A charge that capture was off for 40 minutes of, longer than T_still,
    is ``incomplete`` wherever the gap falls inside a session, and counts
    the gap."""
    car = charge(tmp_path, reporting, LONG)
    for where, sessions in car.events("charging"):
        across = overlapping(sessions, 590, 590 + LONG)
        assert across, (where, sessions)
        for session in across:
            assert session["quality"] == "incomplete", (where, session)
            assert session["gaps"] >= 1, (where, session)


def test_a_charge_without_a_gap_counts_none(tmp_path):
    """The reference: the same charge with capture on throughout is one
    session, ``measured``, ``gaps`` zero."""
    car = charge(tmp_path, "sampled", 0)
    for where, sessions in car.events("charging"):
        assert len(sessions) == 1, (where, sessions)
        assert sessions[0]["quality"] == "measured", (where, sessions[0])
        assert sessions[0]["gaps"] == 0, (where, sessions[0])


# --- No interpolation across a gap -------------------------------------------

def test_the_distance_driven_inside_a_gap_belongs_to_no_trip(tmp_path):
    """The car stands at home at 1000 km; capture is off from 05:00 to
    08:00, and the start after it finds the car away at 1040 km — it was
    driven while nothing was captured. Later it drives 10 km on. Nothing
    is read across the gap: no trip the gap falls in carries a distance,
    and the distance of all trips is the 10 km driven while capture was
    on."""
    car = Car(tmp_path, "sampled").begin().heartbeats(30, 270)
    car.stop(300)
    car.state["odometer"] = ("1040", "km", ODOMETER, None)
    car.state["position"] = ("not_home", None, POSITION, AWAY)
    car.since["odometer"] = car.since["position"] = at(420)
    car.start(480).heartbeats(510, 570)
    for i, m in enumerate(range(600, 631, 5)):
        where = (AWAY[0] + 0.01 * i, AWAY[1] + 0.01 * i)
        car.at_km(m, 1040 + round(10 * i / 6), where)
    car.heartbeats(660, 750)
    for where, trips in car.events("trips"):
        assert trips, (where, trips)
        for trip in overlapping(trips, 300, 480):
            assert not trip["distance_km"], (where, trip)
        total = sum(t["distance_km"] or 0 for t in trips)
        assert total == pytest.approx(10, abs=0.01), (where, trips)
