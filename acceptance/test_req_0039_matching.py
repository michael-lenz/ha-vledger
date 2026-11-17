# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0039, as a user meets it: matching of receipt and candidate is redone
on every derivation; a receipt goes to the nearest candidate in time of the
same kind within the matching tolerance (6 h by default); an ambiguous case
is flagged and assigned to nothing; a receipt may be entered arbitrarily
late as long as its anchor time is right; a receipt entered from a
candidate meets that candidate without tolerance.

Black-box: every step is the ``vledger`` CLI run as a separate process, the
way the user guide shows it — a stream written with ``vledger l0``, receipts
entered with ``vledger receipt add``, the pairing read with ``vledger derive
match``, and L1 built with ``vledger derive all --write`` or ``vledger l1
update`` and read with ``vledger l1 read`` and ``vledger l1 status``.
Nothing here imports the library.

Every check of who met what reads the pairing twice — as ``derive match``
prints it, and as the events in L1 carry it — and asserts that they agree.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0039")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
DAY = datetime(2026, 10, 5, tzinfo=UTC)

CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "fuel_level": {"entity": "sensor.fuel_level"},
        "charging_state": {"entity": "sensor.charging_state", "map": {"charging": ["charging"]}},
        "soc": {"entity": "sensor.soc"},
    },
    "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
}


def at(hours: float) -> str:
    """A time of the scenario: hours after midnight UTC on 5 October 2026."""
    return (DAY + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def vledger(base: Path, *argv: str, expect: int | None = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``;
    ``expect`` None asks for any exit code but 0."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    ok = p.returncode != 0 if expect is None else p.returncode == expect
    assert ok, (argv, p.returncode, p.stdout, p.stderr)
    return p


def jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def scenario(fills=(), charges=(), until: float = 30) -> list[tuple]:
    """A vehicle's day, as (hour, role, state, unit) in time order, ``role``
    None for a heartbeat every half hour. Each refuelling at hour ``T`` is a
    short drive ending 1.5 h before it, the level falling to 15 L, and a rise
    to 45 L at ``T`` with the odometer standing; each charging session at
    hour ``T`` charges for an hour, the state of charge rising from 40 to
    80 %, with no charge point configured, so it is a candidate too."""
    lines = [(0, "odometer", "1000", "km"), (0, "fuel_level", "20.0", "L"),
             (0, "charging_state", "idle", None), (0, "soc", "40", "%")]
    odometer = 1000
    for t in sorted(fills):
        odometer += 25
        lines += [(t - 1.5, "odometer", str(odometer), "km"), (t - 1.4, "fuel_level", "15.0", "L"),
                  (t, "fuel_level", "45.0", "L")]
    for t in sorted(charges):
        lines += [(t, "charging_state", "charging", None), (t + 0.5, "soc", "60", "%"),
                  (t + 1, "soc", "80", "%"), (t + 1, "charging_state", "idle", None),
                  (t + 2, "soc", "80", "%")]
    lines += [(h / 2, None, None, None) for h in range(1, int(until * 2) + 1)]
    return sorted((x for x in lines if x[0] <= until), key=lambda x: (x[0], x[1] is None))


def write(base: Path, lines: list[tuple], after: float | None = None) -> None:
    """Log ``lines`` as the integration would: a start and the configuration
    first, unless ``after`` says the stream already runs up to that hour and
    only what follows it is appended."""
    if after is None:
        vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-0.1),
                "--homeassistant", "2026.9.4", "--snapshot", "[]")
        vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-0.1),
                "--config", json.dumps(CONFIG))
    for hour, role, value, unit in lines:
        if after is not None and hour <= after:
            continue
        if role is None:
            vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(hour), "--lines", "0")
            continue
        argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(hour), "--role", role,
                "--entity", CONFIG["roles"][role]["entity"], "--state", value]
        if unit:
            argv += ["--unit", unit]
        vledger(base, *argv)


def receipt(base: Path, kind: str = "refuelling", anchor: float | None = None,
            from_candidate: float | None = None, entered: str | None = None,
            replaces: str | None = None, expect: int | None = 0) -> subprocess.CompletedProcess:
    argv = ["receipt", "add", kind, "--vehicle", VEHICLE, "--total-price", "50.00"]
    argv += ["--quantity-l", "30", "--full"] if kind == "refuelling" else ["--energy-kwh", "20"]
    if anchor is not None:
        argv += ["--anchor", at(anchor)]
    if from_candidate is not None:
        argv += ["--from-candidate", at(from_candidate)]
    if entered:
        argv += ["--t", entered]
    if replaces:
        argv += ["--replaces", replaces]
    return vledger(base, *argv, expect=expect)


def add(base: Path, kind: str = "refuelling", **kw) -> str:
    """Enter a receipt; returns its id."""
    return jsonl(receipt(base, kind, **kw).stdout)[0]["id"]


def events(base: Path) -> dict[tuple[str, str], dict]:
    """The refuellings and charging sessions in L1, by (kind, start)."""
    out = {}
    for kind in ("refuelling", "charging"):
        for e in jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind).stdout):
            out[(kind, e["start"])] = e
    return out


def pairing(base: Path, rebuild: bool = True) -> dict[str, dict]:
    """What ``derive match`` prints, by receipt id — after L1 is rebuilt
    (unless ``rebuild`` is false, when L1 is left as it stands), and checked
    against what the events in L1 say."""
    if rebuild:
        vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    match = {m["receipt"]: m for m in jsonl(vledger(base, "derive", "match", "--vehicle", VEHICLE).stdout)}
    l1 = events(base)
    for rid, m in match.items():
        holding = [key for key, e in l1.items() if e.get("receipt") == rid]
        if m["match"] == "event":
            assert holding == [(m["kind"], m["event_start"])], (m, holding)
        else:
            # Met by no detected event: at most the receipt's own line in L1,
            # at its anchor, stands for it.
            assert holding in ([], [(m["kind"], m["anchor"])]), (m, holding)
        if m["match"] == "ambiguous":
            for start in m["events"]:
                e = l1[(m["kind"], start)]
                assert e["confirmation"] == "ambiguous" and not e.get("receipt"), e
                assert rid in e["contenders"], e
    return match


def met(base: Path, rid: str, rebuild: bool = True) -> str | None:
    """The start of the event receipt ``rid`` was assigned to, or None."""
    m = pairing(base, rebuild)[rid]
    return m["event_start"] if m["match"] == "event" else None


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.parametrize(("anchor", "nearest"), [(11, 8), (13, 16), (4, 8), (19, 16)])
def test_a_receipt_meets_the_nearest_refuelling_in_time(base, anchor, nearest):
    """Two refuellings eight hours apart, both within the tolerance of an
    anchor between them: the receipt meets the nearer, before or after it,
    and the other still waits for one."""
    write(base, scenario(fills=(8, 16)))
    rid = add(base, anchor=anchor)
    assert met(base, rid) == at(nearest)
    other = at(8 if nearest == 16 else 16)
    assert events(base)[("refuelling", other)]["confirmation"] == "unconfirmed"


@pytest.mark.parametrize("side", [-1, 1])
def test_a_receipt_within_the_tolerance_meets_its_event_and_one_beyond_it_does_not(base, side):
    """The default tolerance is 6 h, on either side of the anchor: a minute
    inside it the receipt meets the refuelling, a minute outside it meets
    nothing and the refuelling stays unconfirmed."""
    write(base, scenario(fills=(12,)))
    inside = add(base, anchor=12 + side * (6 - 1 / 60))
    assert met(base, inside) == at(12)
    vledger(base, "receipt", "cancel", inside, "--vehicle", VEHICLE, "--note", "test")
    outside = add(base, anchor=12 + side * (6 + 1 / 60))
    assert met(base, outside) is None
    assert events(base)[("refuelling", at(12))]["confirmation"] == "unconfirmed"


def test_a_receipt_meets_only_an_event_of_its_own_kind(base):
    """A refuelling at 08:00 and a charging session away from any charge
    point at 11:00: a refuelling receipt anchored at 11:00 meets the
    refuelling three hours off, not the session at its anchor; a charging
    receipt anchored at 08:30 meets the session, not the refuelling half an
    hour off."""
    write(base, scenario(fills=(8,), charges=(11,)))
    fuel = add(base, "refuelling", anchor=11)
    charge = add(base, "charging", anchor=8.5)
    match = pairing(base)
    assert (match[fuel]["kind"], match[fuel]["match"], match[fuel]["event_start"]) == (
        "refuelling", "event", at(8))
    assert (match[charge]["kind"], match[charge]["match"], match[charge]["event_start"]) == (
        "charging", "event", at(11))


def test_a_receipt_of_one_kind_never_meets_an_event_of_the_other(base):
    """With no refuelling in reach, a refuelling receipt anchored at a
    charging session's start meets nothing, and the session still waits
    for a receipt of its own kind."""
    write(base, scenario(charges=(11,)))
    rid = add(base, "refuelling", anchor=11)
    assert met(base, rid) is None
    assert events(base)[("charging", at(11))]["confirmation"] == "unconfirmed"


def test_a_receipt_equally_near_two_events_is_ambiguous_and_assigned_to_neither(base):
    """Anchored at 12:00, midway between refuellings at 08:00 and 16:00: the
    receipt is flagged ambiguous, both refuellings say so and name it, and
    both are counted as waiting for a receipt."""
    write(base, scenario(fills=(8, 16)))
    rid = add(base, anchor=12)
    m = pairing(base)[rid]
    assert m["match"] == "ambiguous"
    assert sorted(m["events"]) == [at(8), at(16)]
    for start in (8, 16):
        assert events(base)[("refuelling", at(start))]["confirmation"] == "ambiguous"
    status = vledger(base, "l1", "status", "--vehicle", VEHICLE).stdout
    assert "refuelling: waiting for a receipt: 0 unconfirmed, 2 ambiguous" in status


def test_two_receipts_competing_for_one_event_are_ambiguous_and_neither_is_assigned(base):
    """Two receipts an hour either side of one refuelling, nearer to it than
    to anything else: neither is guessed, both are flagged, and the
    refuelling names both as its contenders."""
    write(base, scenario(fills=(8,)))
    first, second = add(base, anchor=7), add(base, anchor=9)
    match = pairing(base)
    assert match[first]["match"] == match[second]["match"] == "ambiguous"
    fill = events(base)[("refuelling", at(8))]
    assert fill["confirmation"] == "ambiguous"
    assert sorted(fill["contenders"]) == sorted([first, second])


def test_an_ambiguous_receipt_meets_its_event_once_its_anchor_is_corrected(base):
    """The user guide's remedy for an ambiguous receipt: correct its anchor.
    The corrected receipt meets the refuelling it is now nearest to, at the
    next derivation, and the other no longer waits on an ambiguity."""
    write(base, scenario(fills=(8, 16)))
    rid = add(base, anchor=12)
    assert pairing(base)[rid]["match"] == "ambiguous"
    fixed = add(base, anchor=15.5, replaces=rid)
    match = pairing(base)
    assert rid not in match
    assert (match[fixed]["match"], match[fixed]["event_start"]) == ("event", at(16))
    assert events(base)[("refuelling", at(8))]["confirmation"] == "unconfirmed"


@pytest.mark.parametrize("live", [False, True], ids=["rebuild", "live"])
def test_matching_is_redone_when_a_nearer_event_appears(base, live):
    """The stream runs to 11:00 with a refuelling at 08:00; a receipt
    anchored at 13:00 meets it, five hours off. Once the stream runs on and
    a refuelling at 16:00 is complete, three hours off, the next derivation
    — a rebuild, or the live update the integration runs — moves the
    receipt to it, and the refuelling at 08:00 waits for a receipt again."""
    lines = scenario(fills=(8, 16), until=30)
    write(base, [x for x in lines if x[0] <= 11])
    rid = add(base, anchor=13)
    assert met(base, rid) == at(8)
    write(base, lines, after=11)
    if live:
        vledger(base, "l1", "update", "--vehicle", VEHICLE)
    assert met(base, rid, rebuild=not live) == at(16)
    l1 = events(base)
    assert l1[("refuelling", at(8))]["confirmation"] == "unconfirmed"
    assert l1[("refuelling", at(16))]["confirmation"] == "receipt"


def test_a_receipt_with_no_event_in_reach_meets_one_that_appears_later(base):
    """A receipt entered for a refuelling the stream does not show yet meets
    nothing; once the refuelling is logged and complete, the next
    derivation assigns it."""
    lines = scenario(fills=(16,), until=30)
    write(base, [x for x in lines if x[0] <= 6])
    rid = add(base, anchor=16.5)
    assert met(base, rid) is None
    write(base, lines, after=6)
    assert met(base, rid) == at(16)


@pytest.mark.parametrize("entered", ["2026-10-05T08:00:00Z", "2026-10-07T12:00:00Z",
                                     "2027-03-01T10:00:00Z"])
def test_a_receipt_entered_late_meets_its_event_when_its_anchor_is_right(base, entered):
    """The tolerance is about the anchor, not the moment of entry: a receipt
    entered at the pump, two days later or five months later, anchored half
    an hour off, meets the refuelling all the same."""
    write(base, scenario(fills=(8,)))
    rid = add(base, anchor=7.5, entered=entered)
    assert met(base, rid) == at(8)


def test_a_receipt_entered_late_with_its_anchor_beyond_the_tolerance_meets_nothing(base):
    """Late entry buys no tolerance: entered months later and anchored at
    the time of entry's clock rather than the fill's, it meets nothing."""
    write(base, scenario(fills=(8,)))
    rid = add(base, anchor=20, entered="2027-03-01T10:00:00Z")
    assert met(base, rid) is None


def test_a_receipt_entered_from_a_candidate_meets_exactly_that_candidate(base):
    """Two refuellings three hours apart, both within the tolerance of
    either: a receipt entered from the later one's start meets that one
    and no other, at no distance."""
    write(base, scenario(fills=(8, 11)))
    rid = add(base, from_candidate=11)
    m = pairing(base)[rid]
    assert (m["match"], m["event_start"], m["distance_s"]) == ("event", at(11), 0)
    l1 = events(base)
    assert l1[("refuelling", at(11))]["receipt"] == rid
    assert l1[("refuelling", at(8))]["confirmation"] == "unconfirmed"


def test_a_receipt_from_a_candidate_that_is_not_there_is_refused(base):
    """--from-candidate names a detected event's start; a start where no
    refuelling begins is refused with a message, and nothing is kept."""
    write(base, scenario(fills=(8,)))
    p = receipt(base, from_candidate=9.5, expect=None)
    assert p.stderr.strip()
    assert jsonl(vledger(base, "receipt", "list", "--vehicle", VEHICLE).stdout) == []
