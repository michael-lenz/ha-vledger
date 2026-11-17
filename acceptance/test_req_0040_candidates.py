# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0040, as a user meets it with refuellings: a candidate without a
receipt stays visible as unconfirmed; a receipt without a candidate is an
event of its own; a receipt entered while its candidate is still settling is
pending — in the receipts file and ``vledger derive match``, in L1 nowhere and
counted nowhere until the candidate completes and it meets it (ADR-0061); and
nothing of either is ever silently discarded.

Derived from the requirement, ADR-0061, README.md ("a detected event without
a receipt stays visible as unconfirmed") and the user guide: *Receipts:
vledger receipt* ("the receipt meets the nearest refuelling … within the
matching tolerance (6 h by default) … a receipt for a fill still settling …
waits for it, and ``derive match`` lists it as pending"; "A receipt that meets
two events equally, or that another receipt competes with, is reported as
ambiguous and assigned to nothing"), *The derivations* (a refuelling settles
T_settle, six minutes, after its last rise; "A candidate still settling is
printed with ``level_after_l`` empty and not yet written to L1"), *Periods*
("the litres and the fuel cost bought, from the receipts"), *The derivation
on disk* (``l1 read --waiting``, ``l1 update``) and the card's "an event
without a receipt says how it stands: *unconfirmed* or *ambiguous*".

Black-box: every step is the ``vledger`` command run as a separate process,
on a stream written with ``vledger l0`` as the integration writes it.
Charging sessions, whose charge point decides whether they are candidates,
are the other module, test_req_0040_candidates_ha.py."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0040")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
HOME = ("51.0000", "7.0000")
ROAD = [("51.0300", "7.0200"), ("51.0600", "7.0500"), ("51.0900", "7.0700")]
STATION = ("51.1000", "7.0800")
WAITING = {"unconfirmed", "ambiguous", "pending"}


def at(minutes: int) -> str:
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    h, m = divmod(6 * 60 + minutes, 60)
    d, h = divmod(h, 24)
    return f"2026-10-{5 + d:02d}T{h:02d}:{m:02d}:00.000Z"


def vledger(base: Path, *argv: str) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p


def lines(p: subprocess.CompletedProcess) -> list[dict]:
    """The JSON lines a verb printed, any summary line beside them left out."""
    return [json.loads(line) for line in p.stdout.splitlines() if line.lstrip().startswith("{")]


def values(record: object) -> list[object]:
    """Every scalar a record holds, at any depth: the keys a receipt, a match
    and a derived event are written under are the design's, not the guide's."""
    if isinstance(record, dict):
        return [v for x in record.values() for v in values(x)]
    if isinstance(record, list):
        return [v for x in record for v in values(x)]
    return [record]


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
    state(base, minutes, "position", zone, latitude=where[0], longitude=where[1], gps_accuracy="0")


def heartbeat(base: Path, minutes: int) -> None:
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(minutes), "--lines", "0")


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

# The fill's first rise and its last; it has settled T_settle after the last,
# at minute 60, so a stream whose last line is at 59 still has it settling.
FILL_START, FILL_END, SETTLING = 52, 54, 59


def to_the_pump(base: Path) -> None:
    """A morning as the integration logs it: start, configuration, the drive
    to a fuel station, and the fill there, seen in two steps."""
    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": "1000", "unit": "km",
         "since": at(-120)},
        {"role": "position", "entity": "sensor.position", "state": "home", "since": at(-120),
         "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]), "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": "20.5", "unit": "L",
         "since": at(-120)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60), "--homeassistant",
            "2026.9.4", "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60), "--config",
            json.dumps(CONFIG))
    state(base, 0, "ignition", "on")
    state(base, 15, "odometer", "1007", "km"), fix(base, 15, ROAD[0])
    state(base, 30, "odometer", "1015", "km"), fix(base, 30, ROAD[1])
    state(base, 45, "odometer", "1022", "km"), fix(base, 45, STATION, zone="fuel_station")
    state(base, 46, "fuel_level", "18.9", "L")
    state(base, 47, "ignition", "off")
    state(base, FILL_START, "fuel_level", "40.0", "L")
    state(base, FILL_END, "fuel_level", "60.0", "L")
    state(base, 58, "fuel_level", "60.4", "L")


def home_again(base: Path, until: int = 300) -> None:
    """The drive home after the stop, and quiet until ``until``."""
    state(base, 100, "ignition", "on")
    state(base, 115, "odometer", "1037", "km"), fix(base, 115, ROAD[2])
    state(base, 130, "odometer", "1044", "km"), fix(base, 130, HOME, zone="home")
    state(base, 131, "fuel_level", "58.1", "L")
    state(base, 132, "ignition", "off")
    heartbeat(base, until)


TYPED = ("--quantity-l", "41.37", "--total-price", "72.36", "--full")


def receipt(base: Path, minutes: int, *typed: str) -> str:
    """A refuelling receipt anchored ``minutes`` into the scenario; its id."""
    [line] = lines(vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE,
                           "--anchor", at(minutes), *(typed or TYPED)))
    assert line["id"] in values(line)
    return line["id"]


def rebuilt(base: Path) -> None:
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")


def refuellings(base: Path, *flags: str) -> list[dict]:
    return lines(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", "refuelling", *flags))


def waiting(base: Path) -> list[dict]:
    """The refuellings waiting for a receipt, as the guide reads them: the
    last ones, with ``--waiting``."""
    return refuellings(base, "--last", "50", "--waiting")


def whole_l1(base: Path) -> list[dict]:
    """Every event and period line L1 holds."""
    return [ln for kind in ("trip", "refuelling", "charging", "period")
            for ln in lines(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind))]


def periods(base: Path) -> list[dict]:
    return lines(vledger(base, "derive", "periods", "--vehicle", VEHICLE))


def match_lines(base: Path, receipt_id: str) -> list[str]:
    """The lines ``derive match`` prints that name the receipt."""
    out = vledger(base, "derive", "match", "--vehicle", VEHICLE).stdout
    return [line for line in out.splitlines() if receipt_id in line]


def listed(base: Path, *flags: str) -> str:
    return vledger(base, "receipt", "list", "--vehicle", VEHICLE, *flags).stdout


def the_fill(events: list[dict]) -> dict:
    """The event that started at the fill's first rise."""
    [fill] = [e for e in events if e.get("start") == at(FILL_START)]
    return fill


@pytest.fixture
def base(tmp_path: Path) -> Path:
    d = tmp_path / "vledger"
    d.mkdir()
    return d


def test_a_refuelling_without_a_receipt_stays_visible_as_unconfirmed(base):
    """Every refuelling is a candidate: with no receipt entered, the fill is
    written to L1 all the same, says it is unconfirmed, and is among the
    events waiting for a receipt."""
    to_the_pump(base), home_again(base)
    rebuilt(base)

    fill = the_fill(refuellings(base))
    assert "unconfirmed" in values(fill), fill
    assert the_fill(waiting(base)) == fill


def test_a_receipt_with_no_candidate_near_it_is_an_event_of_its_own(base):
    """A receipt anchored hours beyond the matching tolerance of any fill
    meets none: it becomes a refuelling of its own, not waiting for
    anything, while the fill the sensor saw still waits for its receipt —
    neither is dropped for the other."""
    to_the_pump(base), home_again(base, until=16 * 60)
    far = FILL_START + 9 * 60
    rid = receipt(base, far)
    rebuilt(base)

    events = refuellings(base)
    own = [e for e in events if rid in values(e)]
    assert len(own) == 1, events
    assert not WAITING & set(values(own[0])), own
    assert all(rid not in values(e) for e in waiting(base))
    fill = the_fill(events)
    assert rid not in values(fill) and "unconfirmed" in values(fill), fill
    assert the_fill(waiting(base)) == fill
    assert rid in listed(base)
    assert match_lines(base, rid)
    assert not any("pending" in line for line in match_lines(base, rid))


def test_a_receipt_for_a_fill_still_settling_is_pending_and_counts_nowhere(base):
    """Entered at the pump, before the fill has settled: the receipt is in the
    receipts file and listed as pending by ``derive match``, but no line of
    L1 names it, it forms no refuelling of its own, and the period figures
    are what they were before it was entered."""
    to_the_pump(base)
    heartbeat(base, SETTLING)
    rebuilt(base)
    figures_before = periods(base)

    rid = receipt(base, FILL_START + 3)
    assert rid in listed(base)
    assert any(rid in f.read_text() for f in base.rglob("receipts.jsonl"))
    assert any("pending" in line for line in match_lines(base, rid)), match_lines(base, rid)

    rebuilt(base)
    assert all(rid not in values(ln) for ln in whole_l1(base))
    assert refuellings(base) == []
    assert periods(base) == figures_before


def test_the_pending_receipt_enters_with_its_candidate_once_it_completes(base):
    """When the stream goes on past the settling time, the fill completes and
    the receipt that waited meets it: one refuelling, naming the receipt, no
    longer waiting, and the litres and price bought now count in the
    period — the same whether L1 is kept live or rebuilt."""
    to_the_pump(base)
    heartbeat(base, SETTLING)
    rebuilt(base)
    rid = receipt(base, FILL_START + 3)
    rebuilt(base)
    figures_pending = periods(base)

    home_again(base)
    vledger(base, "l1", "update", "--vehicle", VEHICLE)
    live = refuellings(base)
    fill = the_fill(live)
    assert rid in values(fill), live
    assert not WAITING & set(values(fill)), fill
    assert len([e for e in live if rid in values(e)]) == 1
    assert waiting(base) == []
    assert not any("pending" in line for line in match_lines(base, rid))

    rebuilt(base)
    assert refuellings(base) == live
    month = [p for p in periods(base) if p.get("period") == "month"]
    assert month and 41.37 in values(month[0]) and 72.36 in values(month[0]), month
    assert periods(base) != figures_pending


def test_a_stream_that_stops_leaves_the_receipt_waiting_with_its_candidate(base):
    """The stream ends orderly while the fill still settles: the candidate is
    still printed, the receipt still listed and pending, and it can still
    be cancelled — waiting is not discarding."""
    to_the_pump(base)
    vledger(base, "l0", "stop", "--vehicle", VEHICLE, "--t", at(SETTLING),
            "--reason", "shutdown")
    rid = receipt(base, FILL_START + 3)

    printed = lines(vledger(base, "derive", "refuellings", "--vehicle", VEHICLE))
    assert any(c.get("start") == at(FILL_START) for c in printed), printed
    assert rid in listed(base)
    assert any("pending" in line for line in match_lines(base, rid)), match_lines(base, rid)

    vledger(base, "receipt", "cancel", rid, "--vehicle", VEHICLE, "--note", "entered twice")
    assert rid in listed(base, "--all")


def test_receipts_that_compete_for_one_fill_are_kept_and_the_fill_still_waits(base):
    """Two receipts for the same fill compete: both stay in the receipts file
    and in ``derive match``, neither is assigned, and the fill stays visible,
    waiting for a receipt as ambiguous."""
    to_the_pump(base), home_again(base)
    first = receipt(base, FILL_START + 3)
    second = receipt(base, FILL_START + 3, "--quantity-l", "41.73", "--total-price", "72.99",
                     "--full")
    rebuilt(base)

    assert first in listed(base) and second in listed(base)
    assert match_lines(base, first) and match_lines(base, second)
    fill = the_fill(refuellings(base))
    assert "ambiguous" in values(fill), fill
    assert the_fill(waiting(base)) == fill
    others = [e for e in refuellings(base) if e is not fill and e.get("start") != at(FILL_START)]
    assert all(first not in values(e) and second not in values(e) for e in others), others


def test_the_whole_story_reads_the_same_from_a_copy_of_the_data_directory(base, tmp_path):
    """What waits is a function of the stream and the receipts file, nothing
    kept elsewhere: a copy of the data directory with L1 deleted shows the
    same pending receipt, and the same refuelling once it completes."""
    to_the_pump(base)
    heartbeat(base, SETTLING)
    rid = receipt(base, FILL_START + 3)
    copy = tmp_path / "copy"
    shutil.copytree(base, copy)
    vledger(copy, "l1", "clean", "--vehicle", VEHICLE)
    assert any("pending" in line for line in match_lines(copy, rid))

    home_again(base), home_again(copy)
    rebuilt(base), rebuilt(copy)
    assert refuellings(copy) == refuellings(base)
    assert rid in values(the_fill(refuellings(copy)))
