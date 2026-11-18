# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0060, as a user meets it: a charging session begins when the charging
state role changes to ``charging`` and ends when it changes away from it, the
source's values read through the role's mapping (FZG-05, REQ-0018).

Derived from the requirement and from the user documentation:

- the user guide's *The derivations*: "A charging session runs from the
  charging state turning to ``charging`` until it turns to anything else;
  ``unavailable`` and ``unknown`` hold, so a sensor dropping out mid-charge
  does not split the session. A vehicle with no charging state assigned gets
  its sessions from its SoC instead … (``source`` says ``charging_state`` or
  ``soc``). Only finished sessions are printed — one still charging appears
  once it ends and the charge point's and the other vehicles' streams have
  reached that moment, a heartbeat at the latest";
- its **Mapping** step: "tick the source's values that mean *charging* …
  *unavailable* and *unknown* hold the last known state; any other value
  means the opposite — not charging", and its *Writing*: the ``map`` of an
  enumerated role "lists under its one positive state the source values that
  mean it … ``charging`` for ``charging_state``";
- the glossary's *Charging session*: "From the charging state turning to
  ``charging`` until it turns away";
- *Where the data is*: times are UTC ISO 8601 to the millisecond;
  l1-format.md: ``charging-sessions.jsonl`` holds the completed sessions with
  the keys as ``vledger derive`` prints them, read back with ``vledger l1
  read --kind charging``.

Each scenario is written with ``vledger l0`` as the integration writes a
stream: a vehicle with an odometer, a position, a SoC and a charging state
whose source speaks its own words — "Charging" is ticked as *charging*,
"Connected", "Disconnected" and "Charge complete" are not. It parks at home
throughout; no charge point is configured. The sessions are read as ``vledger
derive charging`` prints them and as L1 holds them after ``derive all
--write``. Nothing of ``vledger`` is imported: the command runs in a process
of its own."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0060")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "c060"
DAY = datetime(2026, 10, 7, tzinfo=UTC)
HOME = (51.0000, 7.0000)

ODOMETER = "sensor.odometer"
POSITION = "device_tracker.car"
SOC = "sensor.soc"
CHARGING = "sensor.charging_state"

TICKED = "Charging"                 # the one source value mapped to charging
NOT_TICKED = ["Connected", "Disconnected", "Charge complete"]


def at(minutes: float) -> str:
    """A time of the scenario: minutes after midnight UTC on 7 October 2026,
    as the ledger writes it."""
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


class Car:
    """A vehicle parked at home, its stream written line by line at minutes
    after midnight. Its charging state's source reads ``Disconnected`` when
    capture starts at midnight."""

    def __init__(self, base: Path):
        self.base = base
        self.soc = 30
        snapshot = [
            {"role": "odometer", "entity": ODOMETER, "state": "5000", "unit": "km",
             "since": at(-60)},
            {"role": "position", "entity": POSITION, "state": "home", "since": at(-60),
             "attrs": {"latitude": HOME[0], "longitude": HOME[1], "gps_accuracy": 0}},
            {"role": "soc", "entity": SOC, "state": str(self.soc), "unit": "%",
             "since": at(-60)},
            {"role": "charging_state", "entity": CHARGING, "state": "Disconnected",
             "since": at(-60)},
        ]
        config = {
            "name": "Charging car",
            "roles": {
                "odometer": {"entity": ODOMETER},
                "position": {"entity": POSITION},
                "soc": {"entity": SOC},
                "charging_state": {"entity": CHARGING, "map": {"charging": [TICKED]}},
            },
            "parameters": {"fuel": "petrol"},
            "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600},
            "time_zone": "Europe/Berlin",
        }
        vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(0),
                "--homeassistant", "2026.10.1", "--snapshot", json.dumps(snapshot))
        vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(0),
                "--config", json.dumps(config))
        self.last_heartbeat = 0

    def heartbeats(self, until: float, every: float = 30) -> "Car":
        """Capture alive, a heartbeat every ``every`` minutes up to ``until``."""
        m = self.last_heartbeat + every
        while m <= until:
            vledger(self.base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(m),
                    "--lines", "0")
            m += every
        self.last_heartbeat = max(self.last_heartbeat, until)
        return self

    def charging_state(self, minutes: float, value: str) -> "Car":
        vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", at(minutes),
                "--role", "charging_state", "--entity", CHARGING, "--state", value)
        return self

    def soc_rises(self, first: float, last: float, step: int = 5, every: float = 15) -> "Car":
        """The SoC rises by ``step`` % every ``every`` minutes from ``first`` to
        ``last``, as a battery does while it charges."""
        m = first
        while m <= last:
            self.soc += step
            vledger(self.base, "l0", "state", "--vehicle", VEHICLE, "--t", at(m),
                    "--role", "soc", "--entity", SOC, "--state", str(self.soc),
                    "--unit", "%")
            m += every
        return self

    def derived(self) -> list[dict]:
        return jsonl(vledger(self.base, "derive", "charging", "--vehicle", VEHICLE))

    def l1(self) -> list[dict]:
        vledger(self.base, "derive", "all", "--vehicle", VEHICLE, "--write")
        return jsonl(vledger(self.base, "l1", "read", "--vehicle", VEHICLE,
                             "--kind", "charging"))

    def sessions(self) -> list[tuple[str, list[dict]]]:
        """The sessions as ``derive charging`` prints them and as L1 holds them."""
        return [("derive", self.derived()), ("l1", self.l1())]


def spans(sessions: list[dict]) -> list[tuple[datetime, datetime]]:
    return [(instant(s["start"]), instant(s["end"])) for s in sessions]


def span(first: float, last: float) -> tuple[datetime, datetime]:
    return instant(at(first)), instant(at(last))


def charge(base: Path, away: str) -> Car:
    """Plugged in at 08:50 (``Connected``), charging from 09:00 to 11:00 while
    the SoC rises, then the source turns to ``away``. Heartbeats until 13:00."""
    car = Car(base).heartbeats(510)
    car.charging_state(530, "Connected")
    car.charging_state(540, TICKED)
    car.soc_rises(555, 660)
    car.charging_state(660, away)
    return car.heartbeats(780)


@pytest.mark.parametrize("away", NOT_TICKED)
def test_a_session_runs_from_the_change_to_charging_to_the_change_away(tmp_path, away):
    """The charging state turns to a value ticked as *charging* at 09:00 and
    away from it, to a value not ticked, at 11:00: one session, from 09:00 to
    11:00, its ``source`` the charging state. Being plugged in before
    (``Connected``) begins nothing."""
    car = charge(tmp_path, away)
    for where, sessions in car.sessions():
        assert spans(sessions) == [span(540, 660)], (where, sessions)
        assert sessions[0]["source"] == "charging_state", (where, sessions[0])


@pytest.mark.parametrize("dropout", ["unavailable", "unknown"])
def test_unavailable_and_unknown_hold_the_session(tmp_path, dropout):
    """The source drops out twice in the middle of a charge and once just
    before it ends: each time it reads ``unavailable`` or ``unknown`` the
    session holds — one session from 09:00 to 11:00, ended by the value not
    ticked, not by the drop-out."""
    car = Car(tmp_path).heartbeats(510)
    car.charging_state(540, TICKED)
    car.soc_rises(555, 585)
    car.charging_state(590, dropout).charging_state(605, TICKED)
    car.soc_rises(615, 630)
    car.charging_state(635, dropout).charging_state(640, TICKED)
    car.soc_rises(645, 645)
    car.charging_state(650, dropout)
    car.charging_state(660, "Charge complete")
    car.heartbeats(780)
    for where, sessions in car.sessions():
        assert spans(sessions) == [span(540, 660)], (where, sessions)


def test_a_drop_out_before_charging_begins_no_session(tmp_path):
    """``unavailable`` holds the last known state in either direction: a car
    not charging whose source drops out and comes back not charging has no
    session, whatever the SoC does meanwhile."""
    car = Car(tmp_path).heartbeats(510)
    car.charging_state(540, "unavailable")
    car.charging_state(560, "Connected")
    car.charging_state(600, "unknown")
    car.charging_state(620, "Disconnected")
    car.heartbeats(780)
    for where, sessions in car.sessions():
        assert sessions == [], (where, sessions)


def test_each_change_to_charging_begins_a_session_of_its_own(tmp_path):
    """Two charges the same day — 09:00 to 10:00 and, after a pause in which
    the source reads ``Connected``, 12:00 to 13:30 — are two sessions, each
    from its change to charging to its change away."""
    car = Car(tmp_path).heartbeats(510)
    car.charging_state(540, TICKED).soc_rises(555, 600)
    car.charging_state(600, "Connected")
    car.heartbeats(690)
    car.charging_state(720, TICKED).soc_rises(735, 810)
    car.charging_state(810, "Charge complete")
    car.heartbeats(900)
    for where, sessions in car.sessions():
        assert spans(sessions) == [span(540, 600), span(720, 810)], (where, sessions)


def test_without_a_change_to_charging_there_is_no_session(tmp_path):
    """With a charging state assigned, it is what begins a session: the SoC
    rising by 30 % at standstill while the source reads only values not
    ticked as *charging* begins none."""
    car = Car(tmp_path).heartbeats(510)
    car.charging_state(530, "Connected")
    car.soc_rises(555, 630)
    car.charging_state(660, "Charge complete")
    car.heartbeats(780)
    for where, sessions in car.sessions():
        assert sessions == [], (where, sessions)


def test_a_session_still_charging_is_not_printed_until_it_ends(tmp_path):
    """While the charging state still reads *charging*, hours after the
    change and with heartbeats since, the session has not ended and is not
    printed; once the state turns away it is, ending at that change."""
    car = Car(tmp_path).heartbeats(510)
    car.charging_state(540, TICKED).soc_rises(555, 660)
    car.heartbeats(780)
    assert car.derived() == []
    car.charging_state(800, "Charge complete")
    car.heartbeats(900)
    for where, sessions in car.sessions():
        assert spans(sessions) == [span(540, 800)], (where, sessions)
