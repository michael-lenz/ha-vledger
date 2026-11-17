# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0038, as a user meets it: a receipt refers to an event through its
anchor time, not through ids of derived events, and a receipt entered from
a candidate takes the candidate's time as its anchor time.

Black-box: every step is the ``vledger`` CLI run as a separate process,
the way the user guide shows it — a stream written with ``vledger l0``,
the candidates listed with ``vledger derive refuellings``, receipts
entered with ``vledger receipt``, L1 built, read and deleted with ``vledger
derive`` and ``vledger l1``. Nothing here imports the library.

What the user can see of it, by the user guide's "Receipts: ``vledger
receipt``" (``give its start as --from-candidate``) and "Receipts in Home
Assistant" (``Picking a candidate gives the receipt that event's time
exactly``):

- a receipt entered from a candidate carries the candidate's start, the
  time ``derive refuellings`` printed, and meets that fill;
- the line written holds nothing else of the derived event: no value the
  candidate carries but its times turns up in the receipt;
- the receipt keeps meeting its fill when L1 is deleted and rebuilt, and
  when it is read beside a stream in which the derived fills stand in
  another order — an earlier fill before it — which a receipt holding a
  derived event's number or position would get wrong;
- a time that is no candidate's start is refused, and nothing is written.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0038")]

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


def vledger(base: Path, *argv: str, expect: int | None = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``;
    ``expect=None`` takes any non-zero exit."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    if expect is None:
        assert p.returncode != 0, (argv, p.stdout, p.stderr)
    else:
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

DAY = 24 * 60


def write_stream(base: Path, earlier_fill: bool = False) -> None:
    """A morning as the integration would have logged it: a drive to a fuel
    station, a fill there seen in two steps, the drive home, hours of
    quiet. With ``earlier_fill`` the stream begins a day before, with a
    fill at home that the morning's fill then follows."""
    begin = -DAY - 120 if earlier_fill else -120
    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": "1000", "unit": "km", "since": at(begin)},
        {"role": "position", "entity": "sensor.position", "state": "home", "since": at(begin),
         "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]), "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": "8.0" if earlier_fill else "20.5",
         "unit": "L", "since": at(begin)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(begin + 60), "--homeassistant", "2026.9.4",
            "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(begin + 60), "--config", json.dumps(CONFIG))
    if earlier_fill:
        # A can poured in at home, the odometer standing, settled long before the morning.
        state(base, -DAY, "fuel_level", "8.0", "L")
        state(base, -DAY + 5, "fuel_level", "20.5", "L")
        vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(-DAY + 60), "--lines", "2")
        vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(-60), "--lines", "0")
    # To the station.
    state(base, 0, "ignition", "on")
    state(base, 15, "odometer", "1007", "km"), fix(base, 15, ROAD[0])
    state(base, 30, "odometer", "1015", "km"), fix(base, 30, ROAD[1])
    state(base, 45, "odometer", "1022", "km"), fix(base, 45, STATION, zone="fuel_station")
    state(base, 46, "fuel_level", "18.9", "L")
    state(base, 47, "ignition", "off")
    # The fill, seen in two steps, settling a little higher.
    state(base, 52, "fuel_level", "40.0", "L")
    state(base, 54, "fuel_level", "60.0", "L")
    state(base, 58, "fuel_level", "60.4", "L")
    # Home, after the stop.
    state(base, 100, "ignition", "on")
    state(base, 115, "odometer", "1037", "km"), fix(base, 115, ROAD[2])
    state(base, 130, "odometer", "1044", "km"), fix(base, 130, HOME, zone="home")
    state(base, 131, "fuel_level", "58.1", "L")
    state(base, 132, "ignition", "off")
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(300), "--lines", "0")


TYPED = ("--quantity-l", "41.37", "--total-price", "72.36", "--full", "--place", "motorway services")


def candidates(base: Path) -> list[dict]:
    return lines(vledger(base, "derive", "refuellings", "--vehicle", VEHICLE))


def the_morning_s_fill(base: Path) -> dict:
    """The candidate at the station: the one that started on the morning."""
    [fill] = [c for c in candidates(base) if c["start"] >= at(0)]
    return fill


def add_from_candidate(base: Path, start: str) -> dict:
    """The pump's receipt entered from the candidate, as ``add`` printed it."""
    [line] = lines(vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE,
                           "--from-candidate", start, *TYPED))
    return line


def refuellings(base: Path) -> list[dict]:
    return lines(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", "refuelling"))


def values(record: object) -> list[object]:
    """Every scalar a record holds, at any depth: the keys a receipt and a
    derived event are written under are the design's, not the user guide's."""
    if isinstance(record, dict):
        return [v for x in record.values() for v in values(x)]
    if isinstance(record, list):
        return [v for x in record for v in values(x)]
    return [record]


def same_moment(a: str, b: str) -> bool:
    """Two UTC ISO 8601 times naming the same instant, however written."""
    def parse(t: str) -> datetime:
        return datetime.fromisoformat(t)
    try:
        return parse(a) == parse(b)
    except (TypeError, ValueError):
        return False


def is_time(text: str) -> bool:
    return same_moment(text, text)


def holds_time(record: dict, t: str) -> bool:
    return any(isinstance(v, str) and same_moment(v, t) for v in values(record))


def met_by(fills: list[dict], receipt_id: str) -> list[dict]:
    """The derived fills that name the receipt."""
    return [f for f in fills if receipt_id in values(f)]


def receipts_file(base: Path) -> Path:
    [path] = list(base.rglob("receipts.jsonl"))
    return path


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "data"
    b.mkdir()
    write_stream(b)
    return b


def test_a_receipt_from_a_candidate_takes_the_candidate_s_start_as_its_anchor(base):
    """``--from-candidate`` with the start ``derive refuellings`` printed:
    the line ``add`` prints, and the line ``receipt list`` shows, carry
    that time exactly, and the receipt meets that fill."""
    fill = the_morning_s_fill(base)
    receipt = add_from_candidate(base, fill["start"])
    assert holds_time(receipt, fill["start"]), receipt
    [listed] = lines(vledger(base, "receipt", "list", "--vehicle", VEHICLE))
    assert listed["id"] == receipt["id"]
    assert holds_time(listed, fill["start"]), listed
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    [met] = met_by(refuellings(base), receipt["id"])
    assert met["start"] == fill["start"]
    assert receipt["id"] in values(lines(vledger(base, "derive", "match", "--vehicle", VEHICLE)))


def test_a_receipt_from_a_candidate_holds_nothing_of_the_derived_event_but_its_time(base):
    """The receipt refers to the fill by its time alone: no text the
    candidate carries — an id, a key of its own — turns up in the receipt's
    line, apart from a time and what was typed (the kind of receipt, the
    vehicle, the place)."""
    fill = the_morning_s_fill(base)
    receipt = add_from_candidate(base, fill["start"])
    typed = {"refuelling", VEHICLE, *TYPED}
    shared = {v for v in values(receipt) if isinstance(v, str)} & {v for v in values(fill) if isinstance(v, str)}
    assert {v for v in shared - typed if not is_time(v)} == set(), (receipt, fill)


def test_the_receipt_still_meets_its_fill_after_l1_is_deleted_and_rebuilt(base):
    """L1 written, deleted, built again by ``l1 update`` and by ``derive all
    --write``: each time the same fill, at the candidate's start, holds the
    receipt."""
    receipt = add_from_candidate(base, the_morning_s_fill(base)["start"])
    starts = []
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    starts.append([f["start"] for f in met_by(refuellings(base), receipt["id"])])
    vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    vledger(base, "l1", "update", "--vehicle", VEHICLE)
    starts.append([f["start"] for f in met_by(refuellings(base), receipt["id"])])
    vledger(base, "l1", "clean", "--vehicle", VEHICLE)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    starts.append([f["start"] for f in met_by(refuellings(base), receipt["id"])])
    assert starts == [[at(52)]] * 3, starts


def test_the_receipt_meets_its_fill_where_the_derived_fills_stand_in_another_order(base, tmp_path):
    """The receipt entered from the morning's candidate, where that fill is
    the stream's first, is carried with the receipts file to a data
    directory whose stream has the same morning after a fill the day
    before — the morning's fill is then the second derived. It meets the
    morning's fill there, not the first one, which waits for a receipt."""
    alone = the_morning_s_fill(base)
    assert len(candidates(base)) == 1
    receipt = add_from_candidate(base, alone["start"])

    other = tmp_path / "other"
    other.mkdir()
    write_stream(other, earlier_fill=True)
    fills = candidates(other)
    assert len(fills) == 2 and fills[1]["start"] == alone["start"], fills
    target = other / receipts_file(base).relative_to(base)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(receipts_file(base), target)

    vledger(other, "derive", "all", "--vehicle", VEHICLE, "--write")
    written = refuellings(other)
    [met] = met_by(written, receipt["id"])
    assert met["start"] == alone["start"]
    [first] = [f for f in written if f["start"] < at(0)]
    assert receipt["id"] not in values(first)
    waiting = lines(vledger(other, "l1", "read", "--vehicle", VEHICLE, "--kind", "refuelling", "--last", "10", "--waiting"))
    assert [f["start"] for f in waiting] == [first["start"]]


def test_a_time_that_is_no_candidate_s_start_is_refused(base):
    """A candidate that is gone is refused, as the user guide's form says:
    ``--from-candidate`` with a time no candidate starts at exits non-zero
    and writes nothing; the receipts that count are none."""
    fill = the_morning_s_fill(base)
    before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE,
            "--from-candidate", at(200), *TYPED, expect=None)
    assert fill["start"] != at(200)
    assert {p: p.read_bytes() for p in base.rglob("*") if p.is_file()} == before
    assert lines(vledger(base, "receipt", "list", "--vehicle", VEHICLE)) == []
