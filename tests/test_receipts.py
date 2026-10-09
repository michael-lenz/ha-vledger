# SPDX-License-Identifier: BSD-3-Clause
"""Receipts (ADR-0013): the file, corrections and cancellations, and the
matching against detected events — receipts entered through the verbs.

Refuelling and charging detection are their own derivations; here a stand-in
registered in ``l1.DERIVATIONS`` returns fixed events, so what is tested is
the matching and not the detection.
"""

import json
import random
from datetime import timedelta

import pytest

from vledger import clock, l1, receipts
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
T0 = clock.parse("2026-10-09T10:00:00Z")


def at(minutes: float) -> str:
    return clock.to_text(T0 + timedelta(minutes=minutes))


def refuelling(minutes, delta_l=40.0, length=2):
    return {"kind": "refuelling", "subject": V.id, "start": at(minutes), "end": at(minutes + length),
            "quality": "measured", "sensor_delta_l": delta_l, "version": "x"}


def charging(minutes, length=60, chargepoint="foreign", energy=10.0):
    return {"kind": "charging", "subject": V.id, "start": at(minutes), "end": at(minutes + length),
            "quality": "measured", "chargepoint": chargepoint, "delta_soc_pct": 50.0,
            "battery_kwh": 8.9, "grid_kwh": energy, "grid_kwh_quality": "estimated",
            "grid_kwh_source": "loss_factor", "kwh_per_pct": None, "charging_loss_kwh": None,
            "cost_eur": None, "cost_quality": None, "version": "x"}


class Detected:
    """A stand-in derivation: the events it is given, from a cursor on."""

    def __init__(self):
        self.events = {"refuelling": [], "charging": []}

    def __call__(self, kind):
        def derive(base, subject, since):
            return [e for e in self.events[kind]
                    if since is None or clock.parse(e["end"]) >= clock.parse(since)]
        return derive


@pytest.fixture
def detected(monkeypatch):
    d = Detected()
    for kind in d.events:
        monkeypatch.setitem(l1.DERIVATIONS, kind, d(kind))
    return d


class Entry:
    def __init__(self, base, capsys):
        self.b = ["--base", str(base), "--vehicle", V.id]
        self.base = base
        self.capsys = capsys

    def run(self, *argv, ok=True):
        code = main(list(argv))
        out = self.capsys.readouterr()
        assert (code == 0) == ok, (argv, out.err)
        return out

    def refuel(self, anchor=None, *, q=41.0, full=True, price=72.0, cand=None, t=None, **more):
        args = ["receipt", "add", "refuelling", *self.b, "--quantity-l", str(q), "--total-price", str(price),
                "--full" if full else "--partial", "--t", t or at(0)]
        args += ["--from-candidate", cand] if cand else ["--anchor", anchor]
        for k, v in more.items():
            args += [f"--{k.replace('_', '-')}", str(v)]
        return json.loads(self.run(*args).out)

    def charge(self, anchor=None, *, kwh=11.0, price=6.5, cand=None, **more):
        args = ["receipt", "add", "charging", *self.b, "--energy-kwh", str(kwh),
                "--total-price", str(price), "--t", at(0)]
        args += ["--from-candidate", cand] if cand else ["--anchor", anchor]
        for k, v in more.items():
            args += [f"--{k.replace('_', '-')}", str(v)]
        return json.loads(self.run(*args).out)

    def cancel(self, id, ok=True):
        out = self.run("receipt", "cancel", id, *self.b, "--t", at(0), ok=ok)
        return json.loads(out.out) if ok else out.err

    def listed(self, *more):
        return [json.loads(x) for x in self.run("receipt", "list", *self.b, *more).out.splitlines()]


@pytest.fixture
def entry(tmp_path, capsys):
    return Entry(tmp_path, capsys)


def events(base, kind):
    return l1.derive(base, V, kind)


# --- the file ----------------------------------------------------------

def test_a_receipt_gets_a_uuid_and_one_line(entry):
    r = entry.refuel(at(5), unit_price=1.749, place="motorway services")
    assert r["v"] == 1 and r["kind"] == "refuelling" and r["subject"] == V.id
    assert len(r["id"]) == 36 and r["exact"] is False and r["anchor"] == at(5)
    assert r["quantity_l"] == 41.0 and r["full"] is True and r["unit_price"] == 1.749
    assert r["place"] == "motorway services" and r["note"] is None
    lines = (entry.base / f"vehicle-{V.id}" / "receipts.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == [r]
    assert entry.listed() == [r]


def test_what_a_receipt_must_hold(entry):
    entry.run("receipt", "add", "refuelling", *entry.b, "--anchor", at(0), "--quantity-l", "40",
              "--full", ok=False)                                  # no price at all
    entry.run("receipt", "add", "refuelling", *entry.b, "--anchor", at(0), "--quantity-l", "-1",
              "--total-price", "1", "--full", ok=False)
    entry.run("receipt", "add", "charging", *entry.b, "--anchor", at(0), "--energy-kwh", "5",
              "--total-price", "-2", ok=False)
    entry.run("receipt", "add", "charging", "--base", str(entry.base), "--chargepoint", "c1",
              "--anchor", at(0), "--energy-kwh", "5", "--total-price", "2", ok=False)
    entry.run("receipt", "add", "charging", *entry.b, "--energy-kwh", "5", "--total-price", "2",
              ok=False)                                            # no anchor
    assert entry.listed() == []


def test_corrections_and_cancellations_form_a_chain(entry):
    a = entry.refuel(at(0), q=40)
    b = entry.refuel(at(0), q=41, replaces=a["id"])
    assert b["replaces"] == a["id"] and b["id"] != a["id"]
    assert [r["id"] for r in entry.listed()] == [b["id"]]
    # Only the current receipt may be corrected or cancelled.
    entry.run("receipt", "add", "refuelling", *entry.b, "--anchor", at(0), "--quantity-l", "42",
              "--total-price", "1", "--full", "--replaces", a["id"], ok=False)
    assert "replaced by" in entry.cancel(a["id"], ok=False)
    # A correction keeps the kind.
    entry.run("receipt", "add", "charging", *entry.b, "--anchor", at(0), "--energy-kwh", "5",
              "--total-price", "2", "--replaces", b["id"], ok=False)
    c = entry.cancel(b["id"])
    assert c["kind"] == "cancel" and c["cancels"] == b["id"] and len(c["id"]) == 36
    assert entry.listed() == []
    assert "cancelled by" in entry.cancel(b["id"], ok=False)
    assert "a cancellation" in entry.cancel(c["id"], ok=False)
    assert "no receipt" in entry.cancel("nope", ok=False)
    assert [r["id"] for r in entry.listed("--all")] == [a["id"], b["id"], c["id"]]


def test_a_torn_last_line_is_skipped_and_unknown_kinds_passed_over(entry):
    a = entry.refuel(at(0))
    p = entry.base / f"vehicle-{V.id}" / "receipts.jsonl"
    with open(p, "a") as f:
        f.write('{"v":1,"kind":"voucher","id":"z"}\n{"v":1,"kind":"refu')
    assert [r["id"] for r in entry.listed()] == [a["id"]]


# --- matching ----------------------------------------------------------

def test_a_late_receipt_meets_its_refuelling(entry, detected):
    detected.events["refuelling"] = [refuelling(60), refuelling(60 * 24 * 3)]
    # Entered a week later; the anchor is what counts, 2 h off.
    r = entry.refuel(at(60 + 120), t=at(60 * 24 * 7))
    first, second = events(entry.base, "refuelling")
    assert first["receipt"] == r["id"] and first["confirmation"] == "receipt"
    assert first["quantity_l"] == 41.0 and first["quantity_quality"] == "receipt"
    assert first["price"] == 72.0 and first["unit_price"] == round(72 / 41, 3)
    assert first["sensor_delta_l"] == 40.0 and first["quality"] == "measured"
    assert first["deviation_pct"] == 2.4 and first["implausible"] is False
    assert second["receipt"] is None and second["confirmation"] == "unconfirmed"


def test_beyond_the_tolerance_a_receipt_is_an_event_of_its_own(entry, detected):
    detected.events["refuelling"] = [refuelling(0)]
    r = entry.refuel(at(2 + 6 * 60 + 1))
    cand, own = events(entry.base, "refuelling")
    assert cand["confirmation"] == "unconfirmed"
    assert own["quality"] == "receipt" and own["start"] == own["end"] == r["anchor"]
    assert own["receipt"] == r["id"] and own["deviation_pct"] is None and own["implausible"] is False


def test_ambiguity_is_flagged_never_guessed(entry, detected):
    detected.events["refuelling"] = [refuelling(0), refuelling(120)]
    # Equally near both.
    r = entry.refuel(at(61))
    a, b = events(entry.base, "refuelling")
    assert a["confirmation"] == b["confirmation"] == "ambiguous"
    assert a["contenders"] == b["contenders"] == [r["id"]]
    assert len(events(entry.base, "refuelling")) == 2      # no event of its own
    entry.cancel(r["id"])
    # Two receipts whose nearest event is the same one.
    r1, r2 = entry.refuel(at(10)), entry.refuel(at(20))
    a, b = events(entry.base, "refuelling")
    assert a["confirmation"] == "ambiguous" and a["contenders"] == sorted([r1["id"], r2["id"]])
    assert b["confirmation"] == "unconfirmed"
    out = entry.run("derive", "match", *entry.b)
    rows = [json.loads(x) for x in out.out.splitlines()]
    assert {x["match"] for x in rows} == {"ambiguous"} and "2 ambiguous" in out.err


def test_a_correction_moves_the_match(entry, detected):
    detected.events["refuelling"] = [refuelling(0), refuelling(60 * 24)]
    r = entry.refuel(at(30))
    fixed = entry.refuel(at(60 * 24 + 15), q=38, replaces=r["id"])
    a, b = events(entry.base, "refuelling")
    assert a["receipt"] is None and b["receipt"] == fixed["id"] and b["quantity_l"] == 38


def test_from_a_candidate_the_match_needs_no_tolerance(entry, detected, capsys):
    detected.events["refuelling"] = [refuelling(0), refuelling(30)]
    r = entry.refuel(cand=at(30))
    assert r["exact"] is True and r["anchor"] == at(30)
    a, b = events(entry.base, "refuelling")
    assert a["receipt"] is None and b["receipt"] == r["id"]
    # A start no candidate has is refused.
    entry.run("receipt", "add", "refuelling", *entry.b, "--from-candidate", at(31), "--quantity-l", "4",
              "--total-price", "1", "--full", ok=False)
    # The candidate moves (a new release): the receipt falls back to tolerance.
    detected.events["refuelling"] = [refuelling(-600), refuelling(35)]
    a, b = events(entry.base, "refuelling")
    assert b["receipt"] == r["id"] and a["receipt"] is None


def test_plausibility_against_the_sensor_delta(entry, detected):
    detected.events["refuelling"] = [refuelling(0, delta_l=30.0)]
    entry.refuel(at(0), q=41)
    (e,) = events(entry.base, "refuelling")
    assert e["deviation_pct"] == 26.8 and e["implausible"] is True


def test_charging_sessions_receipts_and_charge_points(entry, detected):
    detected.events["charging"] = [charging(0, length=7 * 60, chargepoint="foreign"),
                                   charging(60 * 24, chargepoint="home"),
                                   charging(60 * 48, chargepoint="foreign")]
    # Printed when a seven-hour session ended: inside the interval.
    r = entry.charge(at(7 * 60), kwh=11.0, price=6.5, provider="roaming card")
    long, home, foreign = events(entry.base, "charging")
    assert long["receipt"] == r["id"] and long["grid_kwh"] == 11.0
    assert long["grid_kwh_quality"] == long["grid_kwh_source"] == "receipt"
    assert long["sensor_grid_kwh"] == 10.0 and long["sensor_grid_kwh_quality"] == "estimated"
    assert long["sensor_grid_kwh_source"] == "loss_factor"
    assert long["cost_eur"] == 6.5 and long["cost_quality"] == "receipt" and long["provider"] == "roaming card"
    assert long["kwh_per_pct"] == 0.22 and long["charging_loss_kwh"] == 2.1
    assert long["deviation_pct"] == 9.1 and long["implausible"] is False
    assert home["confirmation"] == "chargepoint" and foreign["confirmation"] == "unconfirmed"
    # A session at a configured charge point accepts a receipt too.
    h = entry.charge(at(60 * 24 + 30))
    assert events(entry.base, "charging")[1]["receipt"] == h["id"]


def test_matching_does_not_depend_on_entry_order(tmp_path, capsys, detected):
    detected.events["refuelling"] = [refuelling(m) for m in (0, 100, 200, 300, 400)]
    anchors = [at(m) for m in (5, 100, 150, 210, 390, 2000)]
    results = []
    for seed in (1, 2, 3):
        base = tmp_path / str(seed)
        e = Entry(base, capsys)
        order = anchors[:]
        random.Random(seed).shuffle(order)
        for i, a in enumerate(order):
            e.refuel(a, q=10 + anchors.index(a), t=at(i))
        results.append([(x["start"], x.get("quantity_l"), x["confirmation"])
                        for x in events(base, "refuelling")])
    assert results[0] == results[1] == results[2]


# --- L1 ----------------------------------------------------------------

def test_l1_holds_receipts_without_a_derivation(entry):
    r = entry.refuel(at(0))
    m = l1.rebuild(entry.base, V)
    (e,) = l1.read(entry.base, V, "refuelling")
    assert e["receipt"] == r["id"] and e["quality"] == "receipt"
    assert "refuelling" not in m["through"]     # the cursor follows the stream only


def test_incremental_rebuilds_when_a_new_event_could_meet_a_receipt(entry, detected):
    detected.events["refuelling"] = [refuelling(0)]
    l1.rebuild(entry.base, V)
    r = entry.refuel(at(60 * 24 + 10))         # nothing to meet yet: its own event
    l1.incremental(entry.base, V)               # receipts changed: rebuilt
    assert [e["quality"] for e in l1.read(entry.base, V, "refuelling")] == ["measured", "receipt"]
    # The refuelling it describes completes afterwards.
    detected.events["refuelling"].append(refuelling(60 * 24))
    l1.incremental(entry.base, V)
    on_disk = list(l1.read(entry.base, V, "refuelling"))
    assert [e["receipt"] for e in on_disk] == [None, r["id"]] and on_disk[1]["quality"] == "measured"
    # A refuelling far from every receipt is appended, waiting.
    detected.events["refuelling"].append(refuelling(60 * 24 * 5))
    added = l1.incremental(entry.base, V)
    assert [e["confirmation"] for e in added["refuelling"]] == ["unconfirmed"]
    # And the live result is the batch result.
    live = list(l1.read(entry.base, V, "refuelling"))
    l1.rebuild(entry.base, V)
    assert list(l1.read(entry.base, V, "refuelling")) == live


def test_derive_match_prints_the_pairing(entry, detected):
    detected.events["refuelling"] = [refuelling(0)]
    r1 = entry.refuel(at(1))
    r2 = entry.refuel(at(60 * 24))
    out = entry.run("derive", "match", *entry.b)
    rows = {x["receipt"]: x for x in map(json.loads, out.out.splitlines())}
    assert rows[r1["id"]]["match"] == "event" and rows[r1["id"]]["distance_s"] == 0
    assert rows[r2["id"]]["match"] == "own"
    assert "1 met an event, 1 stand alone, 0 ambiguous" in out.err


def test_receipts_change_the_manifest_hash(entry):
    l1.rebuild(entry.base, V)
    assert l1.rebuild_due(entry.base, V) is None
    entry.refuel(at(0))
    assert l1.rebuild_due(entry.base, V) == "the receipts changed"


def test_match_is_a_function_of_its_arguments():
    r = receipts.refuelling(at(0), V, anchor=at(1), quantity_l=5, full=False, unit_price=2.0, id="r")
    m = receipts.match([r], [refuelling(0)], tolerance_s=0)
    assert m.assigned == {0: "r"}                      # inside the interval: distance 0
    assert receipts.match([r], [refuelling(10)], tolerance_s=0).own == ["r"]
    e = receipts.own_event(r, 15)
    assert e["price"] == 10.0 and e["unit_price"] == 2.0 and e["full"] is False


def test_writing_one_kind_writes_what_a_rebuild_writes(entry, detected):
    detected.events["refuelling"] = [refuelling(0)]
    entry.refuel(at(1))
    entry.run("derive", "refuellings", *entry.b, "--write")
    written = list(l1.read(entry.base, V, "refuelling"))
    l1.rebuild(entry.base, V)
    assert written == list(l1.read(entry.base, V, "refuelling"))
    assert written[0]["confirmation"] == "receipt"


def test_anchor_of_takes_exactly_one_time_and_detects_only_for_a_candidate():
    def never():
        raise AssertionError("a typed anchor needs no derivation")

    assert receipts.anchor_of("refuelling", anchor="2026-10-09T12:00:00+02:00",
                              detected=never) == (at(0), False)
    for when in ({}, {"anchor": at(0), "from_candidate": at(0)}):
        with pytest.raises(receipts.Refused, match="exactly one"):
            receipts.anchor_of("refuelling", **when, detected=never)
    def starts():
        return [refuelling(30)]

    assert receipts.anchor_of("refuelling", from_candidate=at(30), detected=starts) == (at(30), True)
    with pytest.raises(receipts.Refused, match="no refuelling event starts"):
        receipts.anchor_of("refuelling", from_candidate=at(31), detected=starts)
