# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0037, as a user meets it: every receipt receives a UUID at entry; it
does not depend on the derivation and stays unchanged across
recomputations.

Black-box: every step is the ``vledger`` CLI run as a separate process,
the way the user guide shows it — a stream written with ``vledger l0``,
receipts entered with ``vledger receipt``, L1 built, read and deleted with
``vledger derive`` and ``vledger l1``. Nothing here imports the library.

What the user can see of it, by the user guide's "Receipts: ``vledger
receipt``" and "Receipts in Home Assistant" (``receipt.id is its UUID``):

- ``receipt add`` prints the line it wrote, and its ``id`` is a UUID, there
  before anything was derived;
- the id is minted, not computed: the same receipt typed twice, or typed
  in two data directories, gets two ids, and a correction or cancellation
  is a receipt with an id of its own;
- the id stays what ``add`` printed through every recomputation — ``derive
  all --write``, ``l1 clean`` and a rebuild, a stream that grows — and
  whether the receipt meets an event, waits for one, or stands alone.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0037")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
HOME = ("51.0000", "7.0000")
ROAD = [("51.0300", "7.0200"), ("51.0600", "7.0500"), ("51.0900", "7.0700")]
STATION = ("51.1000", "7.0800")


def at(minutes: int) -> str:
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    h, m = divmod(6 * 60 + minutes, 60)
    d, h = divmod(h, 24)
    return f"2026-10-{5 + d:02d}T{h:02d}:{m:02d}:00.000Z"


def vledger(base: Path, *argv: str, expect: int = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def lines(p: subprocess.CompletedProcess) -> list[dict]:
    """The JSON lines a verb printed, any summary line beside them left out."""
    return [json.loads(line) for line in p.stdout.splitlines() if line.lstrip().startswith("{")]


def state(base: Path, minutes: int, role: str, value: str, unit: str | None = None, **attrs: str) -> None:
    argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", role,
            "--entity", f"sensor.{role}", "--state", value]
    if unit:
        argv += ["--unit", unit]
    for k, v in attrs.items():
        argv += ["--attr", f"{k}={v}"]
    vledger(base, *argv)


def fix(base: Path, minutes: int, where: tuple[str, str], zone: str = "not_home") -> None:
    state(base, minutes, "position", zone, latitude=where[0], longitude=where[1], gps_accuracy="0")


CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "position": {"entity": "sensor.position"},
        "fuel_level": {"entity": "sensor.fuel_level"},
        "ignition": {"entity": "sensor.ignition", "map": {"on": ["on"]}},
    },
    "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
}


def start_stream(base: Path) -> None:
    """The stream's start and the vehicle's configuration, nothing driven yet."""
    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": "1000", "unit": "km", "since": at(-120)},
        {"role": "position", "entity": "sensor.position", "state": "home", "since": at(-120),
         "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]), "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": "20.5", "unit": "L", "since": at(-120)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60), "--homeassistant", "2026.9.4",
            "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60), "--config", json.dumps(CONFIG))


def drive_to_the_pump(base: Path) -> None:
    """A drive to a fuel station and the fill there, settling a little higher."""
    state(base, 0, "ignition", "on")
    state(base, 15, "odometer", "1007", "km"), fix(base, 15, ROAD[0])
    state(base, 30, "odometer", "1015", "km"), fix(base, 30, ROAD[1])
    state(base, 45, "odometer", "1022", "km"), fix(base, 45, STATION, zone="fuel_station")
    state(base, 46, "fuel_level", "18.9", "L")
    state(base, 47, "ignition", "off")
    state(base, 52, "fuel_level", "40.0", "L")
    state(base, 54, "fuel_level", "60.0", "L")
    state(base, 58, "fuel_level", "60.4", "L")


def drive_home(base: Path) -> None:
    """The drive home after 70 minutes at the station, and hours of quiet."""
    state(base, 100, "ignition", "on")
    state(base, 115, "odometer", "1037", "km"), fix(base, 115, ROAD[2])
    state(base, 130, "odometer", "1044", "km"), fix(base, 130, HOME, zone="home")
    state(base, 131, "fuel_level", "58.1", "L")
    state(base, 132, "ignition", "off")
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(300), "--lines", "0")


RECEIPT = ("--anchor", at(55), "--quantity-l", "41.37", "--total-price", "72.36", "--full",
           "--place", "motorway services")


def add_receipt(base: Path, *extra: str) -> dict:
    """The pump's receipt for the fill, as ``add`` printed the line it wrote."""
    [line] = lines(vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, *RECEIPT, *extra))
    return line


def ids(base: Path, *flags: str) -> list[str]:
    """The ids ``receipt list`` shows."""
    return [r["id"] for r in lines(vledger(base, "receipt", "list", "--vehicle", VEHICLE, *flags))]


def match(base: Path) -> list[dict]:
    return lines(vledger(base, "derive", "match", "--vehicle", VEHICLE))


def refuellings(base: Path) -> list[dict]:
    return lines(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", "refuelling"))


def values(record: object) -> list[object]:
    """Every scalar a derived record holds, at any depth: the key a receipt
    is named under in L1 is the design's, not the user guide's."""
    if isinstance(record, dict):
        return [v for x in record.values() for v in values(x)]
    if isinstance(record, list):
        return [v for x in record for v in values(x)]
    return [record]


def is_uuid(text: object) -> bool:
    try:
        return isinstance(text, str) and str(uuid.UUID(text)) == text.lower()
    except ValueError:
        return False


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "data"
    b.mkdir()
    start_stream(b)
    drive_to_the_pump(b)
    drive_home(b)
    return b


def test_a_receipt_gets_a_uuid_when_it_is_entered_before_anything_is_derived(base):
    """``add`` prints the line it wrote, with the receipt's id, a UUID; no L1
    exists yet, and the receipts that count show it under that id."""
    receipt = add_receipt(base)
    assert is_uuid(receipt["id"]), receipt
    assert not (base / f"vehicle-{VEHICLE}" / "l1").exists()
    assert ids(base) == [receipt["id"]]
    assert ids(base, "--all") == [receipt["id"]]


def test_a_charging_receipt_gets_a_uuid_too(base):
    """Both kinds of receipt get one, also a charge the stream has not seen."""
    [receipt] = lines(vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE,
                              "--anchor", at(200), "--energy-kwh", "11.8", "--total-price", "6.49",
                              "--provider", "roaming card"))
    assert is_uuid(receipt["id"]), receipt
    assert ids(base) == [receipt["id"]]


def test_the_same_receipt_entered_twice_gets_two_ids(base, tmp_path):
    """The id is minted at entry, not computed from what was typed or from
    the stream: the same receipt typed twice, at the same stated time of
    entry, gets two ids, and so does it typed in another data directory
    holding the same stream."""
    first = add_receipt(base, "--t", at(400))
    second = add_receipt(base, "--t", at(400))
    other = tmp_path / "elsewhere"
    other.mkdir()
    start_stream(other), drive_to_the_pump(other), drive_home(other)
    third = add_receipt(other, "--t", at(400))
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert all(is_uuid(r["id"]) for r in (first, second, third))
    assert sorted(ids(base)) == sorted([first["id"], second["id"]])


def test_a_correction_and_a_cancellation_are_receipts_with_ids_of_their_own(base):
    """Correcting or cancelling leaves the earlier receipts' ids as they were
    and mints a new one each time."""
    first = add_receipt(base)
    [correction] = lines(vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE,
                                 "--replaces", first["id"], "--anchor", at(55), "--quantity-l", "41.73",
                                 "--total-price", "72.36", "--full"))
    [cancel] = lines(vledger(base, "receipt", "cancel", correction["id"], "--vehicle", VEHICLE,
                             "--note", "entered twice"))
    assert all(is_uuid(r["id"]) for r in (correction, cancel))
    assert len({first["id"], correction["id"], cancel["id"]}) == 3
    assert ids(base) == []
    assert set(ids(base, "--all")) == {first["id"], correction["id"], cancel["id"]}


def test_the_id_stays_the_same_across_recomputations(base):
    """``derive all --write``, ``l1 clean`` and a rebuild, and a rebuild
    again: the receipt keeps the id ``add`` printed, the event it met names
    it, and its line is what ``add`` printed."""
    receipt = add_receipt(base)
    seen = []
    for _ in range(2):
        vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
        [met] = match(base)
        [fill] = refuellings(base)
        seen.append((ids(base), met, fill))
        vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    vledger(base, "l1", "update", "--vehicle", VEHICLE)
    [met] = match(base)
    [fill] = refuellings(base)
    seen.append((ids(base), met, fill))
    for listed, met, fill in seen:
        assert listed == [receipt["id"]]
        assert receipt["id"] in values(met), met
        assert receipt["id"] in values(fill), fill
    assert lines(vledger(base, "receipt", "list", "--vehicle", VEHICLE, "--all")) == [receipt]


def test_the_id_does_not_depend_on_whether_the_receipt_has_met_an_event(tmp_path):
    """A receipt entered at the pump, before the stream shows the fill, is
    derived without the fill; once the stream has the fill and the drive
    home, it meets the fill — under the id it got at entry, unchanged
    through each derivation. How it stands before the fill is matching's,
    not this requirement's, so only its id is looked at there."""
    base = tmp_path / "data"
    base.mkdir()
    start_stream(base)
    receipt = add_receipt(base)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert ids(base) == [receipt["id"]]
    [listed] = match(base)
    assert receipt["id"] in values(listed), listed

    drive_to_the_pump(base)
    vledger(base, "l1", "update", "--vehicle", VEHICLE)
    assert ids(base) == [receipt["id"]]
    [listed] = match(base)
    assert receipt["id"] in values(listed), listed

    drive_home(base)
    vledger(base, "l1", "update", "--vehicle", VEHICLE)
    [met] = match(base)
    [fill] = refuellings(base)
    assert ids(base) == [receipt["id"]]
    assert receipt["id"] in values(met), met
    assert receipt["id"] in values(fill), fill
