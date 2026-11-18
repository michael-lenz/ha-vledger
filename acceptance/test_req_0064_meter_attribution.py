# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0064 (LAD-07), as a user meets it: a charge point's meter difference
is assigned to a session only if exactly one vehicle charges at that charge
point during the session; otherwise the session is flagged *meter not
attributable*.

Derived from the requirement and from the user documentation:

- the user guide's *The derivations*: "Every session carries … the charge
  point whose radius holds that position — its subject id, or ``foreign``
  … ``grid_kwh`` comes from the charge point's meter (``grid_kwh_source``
  ``meter``, measured), read from the charge point's own stream, when no
  other vehicle charged there during the session — otherwise
  ``meter_attributable`` is ``false`` — else, at a charge point with a
  charging power, from the time … × that power …, when … no other vehicle
  charged there — else from ``battery_kwh`` × the charging loss factor …,
  else it is empty"; and "Only finished sessions are printed — one still
  charging appears once it ends and the charge point's and the other
  vehicles' streams have reached that moment, a heartbeat at the latest";
- its *Writing*: a charge point's configuration, given with
  ``--chargepoint``, has ``name``, ``latitude``, ``longitude``,
  ``radius_m``, ``meter``, ``power_kw`` and ``tariffs``; l0-format.md:
  ``meter`` is ``{"entity": …}``, "whose readings the stream logs under the
  role ``energy_meter``";
- l1-format.md: meter attribution is derived on the vehicle's side, which
  reads the charge points' streams and "the other vehicles' streams to know
  whether one of them charged at the same meter meanwhile (LAD-07)";
  ``charging-sessions.jsonl`` read back with ``vledger l1 read --kind
  charging``;
- the glossary's *Charge point* and *Grid-side energy*.

Each scenario is written with ``vledger l0`` as the integration writes the
streams: a charge point *Home* with an energy meter, and two vehicles, each
with an odometer, a position, a SoC and a charging state whose source reads
``Charging`` for *charging* and ``Connected`` or ``Charge complete``
otherwise. Neither vehicle has a net battery capacity and the charge point no
charging power, so a session whose meter difference is not assigned to it has
no other grid-side energy to fall back on. Each stream's lines are written in
time order, its heartbeats among them, as the integration appends them: a
stream whose t ran backwards would show a silence before each session, across
which nothing is read (ISSUE-0187). No car reports a fix while charging or
after it, so the position fix in its start line's snapshot is "the one before
the start" that places its session; and the meter reports a reading before the
first session besides the start line's snapshot, except in the one test where
the snapshot is all there is. The sessions are read as ``vledger derive
charging`` prints them and as L1 holds them after ``derive all --write``.
Nothing of ``vledger`` is imported: the command runs in a process of its
own."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0064")]

SRC = Path(__file__).resolve().parent.parent / "src"
DAY = datetime(2026, 10, 7, tzinfo=UTC)

HOME = (51.0000, 7.0000)
WORK = (51.1000, 7.1000)            # some 13 km away
TICKED = "Charging"


def at(minutes: float) -> str:
    """Minutes after midnight UTC on 7 October 2026, as the ledger writes it."""
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


class Stream:
    """One subject's stream, begun at midnight with its start and config
    lines. Every later line is held back and written by ``heartbeats``,
    together with the heartbeats and in time order, as the integration appends
    them: a line at the same moment as a heartbeat goes before it. ``vledger l0
    validate`` then finds nothing to warn of, t running backwards least of
    all."""

    def __init__(self, base: Path, flag: str, subject: str):
        self.base, self.flag, self.subject = base, flag, subject
        self.pending: list[tuple[float, list[str]]] = []

    def line(self, minutes: float, *argv: str) -> None:
        self.pending.append((minutes, ["l0", argv[0], self.flag, self.subject,
                                       "--t", at(minutes), *argv[1:]]))

    def heartbeats(self, until: float, every: float = 30):
        m = every
        while m <= until:
            self.line(m, "heartbeat", "--lines", "0")
            m += every
        for _, argv in sorted(self.pending, key=lambda entry: entry[0]):
            vledger(self.base, *argv)
        self.pending = []
        report = vledger(self.base, "l0", "validate", self.flag, self.subject)
        assert "0 error(s), 0 warning(s)" in report, (self.subject, report)
        return self


class ChargePoint(Stream):
    """A charge point with an energy meter, its stream begun at midnight and
    the meter standing at ``kwh`` from 00:30 on, after a last small draw. With
    ``snapshot_only`` the meter stands at ``kwh`` already in the start line's
    snapshot, and no reading follows before the first session."""

    def __init__(self, base: Path, subject: str, name: str, where: tuple[float, float],
                 kwh: float, snapshot_only: bool = False):
        super().__init__(base, "--chargepoint", subject)
        self.entity = f"sensor.{subject}_energy"
        self.kwh = kwh if snapshot_only else kwh - 0.2
        vledger(base, "l0", "start", "--chargepoint", subject, "--t", at(0),
                "--homeassistant", "2026.10.1", "--snapshot", json.dumps([
                    {"role": "energy_meter", "entity": self.entity,
                     "state": f"{self.kwh:.1f}", "unit": "kWh", "since": at(-60)}]))
        vledger(base, "l0", "config", "--chargepoint", subject, "--t", at(0),
                "--config", json.dumps({
                    "name": name, "latitude": where[0], "longitude": where[1],
                    "radius_m": 100.0, "meter": {"entity": self.entity}, "power_kw": None,
                    "tariffs": [{"from": "2026-01-01", "eur_per_kwh": 0.30}]}))
        if not snapshot_only:
            self.meter(30, kwh)

    def meter(self, minutes: float, kwh: float) -> "ChargePoint":
        self.kwh = kwh
        self.line(minutes, "state", "--role", "energy_meter", "--entity", self.entity,
                  "--state", f"{kwh:.1f}", "--unit", "kWh")
        return self

    def draws(self, first: float, last: float, kwh: float, every: float = 10) -> "ChargePoint":
        """The meter counts ``kwh`` in even steps, every ``every`` minutes
        after ``first`` up to ``last``."""
        steps = int((last - first) // every)
        start = self.kwh
        for i in range(1, steps + 1):
            self.meter(first + i * every, start + kwh * i / steps)
        return self


class Car(Stream):
    """A vehicle parked at ``where`` all day, its charging state's source
    reading ``Connected`` when capture starts at midnight; its only position
    fix is the start line's snapshot."""

    def __init__(self, base: Path, subject: str, where: tuple[float, float]):
        super().__init__(base, "--vehicle", subject)
        self.soc, self.where = 30, where
        self.entities = {role: f"sensor.{subject}_{role}"
                         for role in ("odometer", "soc", "charging_state")}
        self.entities["position"] = f"device_tracker.{subject}"
        snapshot = [
            {"role": "odometer", "entity": self.entities["odometer"], "state": "5000",
             "unit": "km", "since": at(-60)},
            {"role": "position", "entity": self.entities["position"], "state": "not_home",
             "since": at(-60),
             "attrs": {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}},
            {"role": "soc", "entity": self.entities["soc"], "state": str(self.soc),
             "unit": "%", "since": at(-60)},
            {"role": "charging_state", "entity": self.entities["charging_state"],
             "state": "Connected", "since": at(-60)},
        ]
        config = {
            "name": f"Car {subject}",
            "roles": {
                "odometer": {"entity": self.entities["odometer"]},
                "position": {"entity": self.entities["position"]},
                "soc": {"entity": self.entities["soc"]},
                "charging_state": {"entity": self.entities["charging_state"],
                                   "map": {"charging": [TICKED]}},
            },
            "parameters": {"fuel": "petrol"},
            "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600},
            "time_zone": "Europe/Berlin",
        }
        vledger(base, "l0", "start", "--vehicle", subject, "--t", at(0),
                "--homeassistant", "2026.10.1", "--snapshot", json.dumps(snapshot))
        vledger(base, "l0", "config", "--vehicle", subject, "--t", at(0),
                "--config", json.dumps(config))

    def state(self, minutes: float, role: str, value: str, unit: str | None = None) -> None:
        self.line(minutes, "state", "--role", role, "--entity", self.entities[role],
                  "--state", value, *(["--unit", unit] if unit else []))

    def charges(self, first: float, last: float) -> "Car":
        """The charging state reads ``Charging`` from ``first`` and ``Charge
        complete`` from ``last``, the SoC rising every 15 minutes between."""
        self.state(first, "charging_state", TICKED)
        m = first + 15
        while m < last:
            self.soc += 5
            self.state(m, "soc", str(self.soc), "%")
            m += 15
        self.state(last, "charging_state", "Charge complete")
        return self

    def derived(self) -> list[dict]:
        assert not self.pending, (self.subject, "lines held back; call heartbeats()")
        return jsonl(vledger(self.base, "derive", "charging", "--vehicle", self.subject))

    def l1(self) -> list[dict]:
        vledger(self.base, "derive", "all", "--vehicle", self.subject, "--write")
        return jsonl(vledger(self.base, "l1", "read", "--vehicle", self.subject,
                             "--kind", "charging"))

    def sessions(self) -> list[tuple[str, list[dict]]]:
        """The sessions as ``derive charging`` prints them and as L1 holds them."""
        return [("derive", self.derived()), ("l1", self.l1())]


def the_session(sessions: list[dict], first: float, last: float) -> dict:
    """The one session from ``first`` to ``last``, the only one there is."""
    assert [(instant(s["start"]), instant(s["end"])) for s in sessions] == [
        (instant(at(first)), instant(at(last)))], sessions
    return sessions[0]


def assigned(session: dict, kwh: float) -> None:
    """The meter difference ``kwh`` is the session's grid-side energy."""
    assert session.get("meter_attributable") is not False, session
    assert session["grid_kwh_source"] == "meter", session
    assert session["grid_kwh"] == pytest.approx(kwh, abs=0.05), session
    assert session["grid_kwh_quality"] == "measured", session


def not_attributable(session: dict) -> None:
    """The session is flagged, and nothing of the meter is its energy."""
    assert session.get("meter_attributable") is False, session
    assert session.get("grid_kwh_source") != "meter", session
    assert session.get("grid_kwh") is None, session


HOME_CP, WORK_CP = "cp64", "cw64"
A, B = "a064", "b064"
END = 420                           # every stream heartbeats until 07:00


def test_a_vehicle_charging_alone_at_the_charge_point_gets_its_meter_difference(tmp_path):
    """Car A charges at Home from 01:00 to 03:00 while the meter counts
    10 kWh; car B, configured too, stands elsewhere and does not charge. A's
    session is at Home, its grid-side energy the meter's 10 kWh, measured."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0)
    car_a = Car(tmp_path, A, HOME).charges(60, 180)
    Car(tmp_path, B, WORK).heartbeats(END)
    home.draws(60, 180, 10.0).heartbeats(END)
    car_a.heartbeats(END)
    for where, sessions in car_a.sessions():
        session = the_session(sessions, 60, 180)
        assert session["chargepoint"] == HOME_CP, (where, session)
        assigned(session, 10.0)


def test_two_vehicles_charging_at_once_at_one_charge_point_are_both_flagged(tmp_path):
    """Car A charges at Home from 01:00 to 03:00, car B at Home from 02:00
    to 04:00; the meter counts both. Neither session gets the meter's
    difference: each is flagged *meter not attributable*."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0)
    car_a = Car(tmp_path, A, HOME).charges(60, 180)
    car_b = Car(tmp_path, B, HOME).charges(120, 240)
    home.draws(60, 240, 18.0).heartbeats(END)
    car_a.heartbeats(END)
    car_b.heartbeats(END)
    for car, (first, last) in ((car_a, (60, 180)), (car_b, (120, 240))):
        for where, sessions in car.sessions():
            session = the_session(sessions, first, last)
            assert session["chargepoint"] == HOME_CP, (car.subject, where, session)
            not_attributable(session)


def test_a_session_that_contains_another_vehicle_s_whole_session_is_flagged(tmp_path):
    """Car A charges at Home from 01:00 to 05:00; car B plugs in at Home and
    charges from 02:00 to 03:00 only, inside A's session. Both are flagged."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0)
    car_a = Car(tmp_path, A, HOME).charges(60, 300)
    car_b = Car(tmp_path, B, HOME).charges(120, 180)
    home.draws(60, 300, 24.0).heartbeats(END)
    car_a.heartbeats(END)
    car_b.heartbeats(END)
    for car, (first, last) in ((car_a, (60, 300)), (car_b, (120, 180))):
        for where, sessions in car.sessions():
            not_attributable(the_session(sessions, first, last))


def test_vehicles_charging_one_after_the_other_each_get_their_own_difference(tmp_path):
    """Car A charges at Home from 01:00 to 03:00 (10 kWh on the meter), car B
    at Home from 03:30 to 05:30 (6 kWh): only one vehicle charges there during
    either session, so each is assigned its own meter difference."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0)
    car_a = Car(tmp_path, A, HOME).charges(60, 180)
    car_b = Car(tmp_path, B, HOME).charges(210, 330)
    home.draws(60, 180, 10.0).draws(210, 330, 6.0).heartbeats(END)
    car_a.heartbeats(END)
    car_b.heartbeats(END)
    for car, (first, last), kwh in ((car_a, (60, 180), 10.0), (car_b, (210, 330), 6.0)):
        for where, sessions in car.sessions():
            session = the_session(sessions, first, last)
            assert session["chargepoint"] == HOME_CP, (car.subject, where, session)
            assigned(session, kwh)


def test_a_vehicle_plugged_in_but_not_charging_meanwhile_does_not_count(tmp_path):
    """Car B stands at Home all day with its charging state ``Connected`` —
    plugged in, not charging — while car A charges there from 01:00 to 03:00.
    Exactly one vehicle charges: A gets the meter's 10 kWh."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0)
    car_a = Car(tmp_path, A, HOME).charges(60, 180)
    car_b = Car(tmp_path, B, HOME).heartbeats(END)
    home.draws(60, 180, 10.0).heartbeats(END)
    car_a.heartbeats(END)
    for where, sessions in car_a.sessions():
        assigned(the_session(sessions, 60, 180), 10.0)
    for where, sessions in car_b.sessions():
        assert sessions == [], (where, sessions)


def test_a_vehicle_charging_at_another_charge_point_meanwhile_does_not_count(tmp_path):
    """Car A charges at Home and car B at Work, both from 01:00 to 03:00.
    Each charge point has one vehicle charging at it, so each session gets its
    own charge point's meter difference: 10 kWh at Home, 7 kWh at Work."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0)
    work = ChargePoint(tmp_path, WORK_CP, "Work", WORK, 500.0)
    car_a = Car(tmp_path, A, HOME).charges(60, 180)
    car_b = Car(tmp_path, B, WORK).charges(60, 180)
    home.draws(60, 180, 10.0).heartbeats(END)
    work.draws(60, 180, 7.0).heartbeats(END)
    car_a.heartbeats(END)
    car_b.heartbeats(END)
    for car, cp, kwh in ((car_a, HOME_CP, 10.0), (car_b, WORK_CP, 7.0)):
        for where, sessions in car.sessions():
            session = the_session(sessions, 60, 180)
            assert session["chargepoint"] == cp, (car.subject, where, session)
            assigned(session, kwh)


def test_a_vehicle_charging_at_a_foreign_place_meanwhile_does_not_count(tmp_path):
    """Car B charges far from any charge point while car A charges at Home,
    both from 01:00 to 03:00: B's session is ``foreign``, and A, alone at
    Home, gets the meter's 10 kWh."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0)
    car_a = Car(tmp_path, A, HOME).charges(60, 180)
    car_b = Car(tmp_path, B, WORK).charges(60, 180)
    home.draws(60, 180, 10.0).heartbeats(END)
    car_a.heartbeats(END)
    car_b.heartbeats(END)
    for where, sessions in car_a.sessions():
        assigned(the_session(sessions, 60, 180), 10.0)
    for where, sessions in car_b.sessions():
        assert the_session(sessions, 60, 180)["chargepoint"] == "foreign", (where, sessions)


def test_a_meter_known_only_from_the_snapshot_still_gives_the_difference(tmp_path):
    """The charge point's stream begins with the meter at 1000.0 kWh in its
    start line's snapshot, and the meter reports nothing more until car A,
    alone at Home, charges from 01:00 to 03:00 and it counts 10 kWh. The
    snapshot is the meter's value before the session: A gets the 10 kWh."""
    home = ChargePoint(tmp_path, HOME_CP, "Home", HOME, 1000.0, snapshot_only=True)
    car_a = Car(tmp_path, A, HOME).charges(60, 180)
    home.draws(60, 180, 10.0).heartbeats(END)
    car_a.heartbeats(END)
    for where, sessions in car_a.sessions():
        assigned(the_session(sessions, 60, 180), 10.0)
