# SPDX-License-Identifier: BSD-3-Clause
"""Receipts (ADR-0013): what a person states about a refuelling or a
charge, kept in ``receipts.jsonl`` next to L0, append-only, and matched to
the derived events on every derivation.

A receipt is never edited. A correction is a whole new receipt that
``replaces`` the current one; a cancellation is a receipt of its own that
``cancels`` it. Which receipts count follows from the file's content alone,
never from its order or the clock, and so does the matching.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from vledger import __version__, clock, l0, layout
from vledger.layout import Subject

#: The receipt schema version this module writes and the newest it reads.
VERSION = 1

#: The kinds that stand for an event, and the one that takes a receipt out.
EVENT_KINDS = ("refuelling", "charging")
KINDS = (*EVENT_KINDS, "cancel")

#: Optional free text per kind (BEL-01).
TEXT_FIELDS = {"refuelling": ("place", "fuel", "note"),
               "charging": ("place", "provider", "note")}

#: The charging event's key naming its charge point; ``FOREIGN`` there
#: means none of the configured ones (LAD-05).
CHARGEPOINT_KEY = "chargepoint"
FOREIGN = "foreign"

#: What a receipt displaces (BEL-07), under the keys the detecting
#: derivations write: the fuel the sensor saw rise; a session's grid-side
#: energy with its quality and source (LAD-06), and its cost (LAD-08).
SENSOR_DELTA_KEY = "sensor_delta_l"
GRID_KEY, GRID_QUALITY_KEY, GRID_SOURCE_KEY = "grid_kwh", "grid_kwh_quality", "grid_kwh_source"
COST_KEY, COST_QUALITY_KEY = "cost_eur", "cost_quality"

RECEIPT = "receipt"


class Refused(ValueError):
    """A receipt the file must not take: the reason is the message."""


# --- building lines ----------------------------------------------------

def _amount(name: str, value, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Refused(f"{name} must be a number, not {value!r}")
    if positive and value <= 0:
        raise Refused(f"{name} must be positive, not {value!r}")
    if value < 0:
        raise Refused(f"{name} must not be negative, not {value!r}")
    return value


def _envelope(t: str, kind: str, subject: Subject, id: str | None) -> dict:
    if subject.kind != "vehicle":
        raise Refused("receipts belong to a vehicle, not to a charge point")
    return {"v": VERSION, "t": clock.to_text(clock.parse(t)), "kind": kind,
            "subject": subject.id, "id": id or str(uuid.uuid4())}


def _body(line: dict, *, anchor: str, exact: bool, replaces: str | None) -> dict:
    if replaces:
        line["replaces"] = replaces
    line["anchor"] = clock.to_text(clock.parse(anchor))
    line["exact"] = bool(exact)
    return line


def anchor_of(kind: str, *, anchor: str | None = None, from_candidate: str | None = None,
              detected: Callable[[], list[dict]]) -> tuple[str, bool]:
    """The anchor time, and whether it is exact (ADR-0013, 3; BEL-04).
    Exactly one of ``anchor`` and ``from_candidate``; the latter must be
    the start of an event ``detected`` yields — called only then, since
    detecting means deriving. What the verb, the action and the form all
    call (ADR-0015, consequence 1)."""
    if (anchor is None) == (from_candidate is None):
        raise Refused("say when: an anchor time, or the start of a detected event — exactly one")
    if anchor is not None:
        return clock.to_text(clock.parse(anchor)), False
    start = clock.parse(from_candidate)
    if start not in {clock.parse(e["start"]) for e in detected()}:
        raise Refused(f"no {kind} event starts at {clock.to_text(start)}; "
                      f"give an anchor time to enter the receipt freely")
    return clock.to_text(start), True


def refuelling(t: str, subject: Subject, *, anchor: str, quantity_l: float, full: bool,
               total_price: float | None = None, unit_price: float | None = None,
               exact: bool = False, place: str | None = None, fuel: str | None = None,
               note: str | None = None, replaces: str | None = None,
               id: str | None = None) -> dict:
    """A refuelling receipt (BEL-01): quantity, a price, full tank or not."""
    if total_price is None and unit_price is None:
        raise Refused("a refuelling receipt needs a total price, a per-litre price, or both")
    if not isinstance(full, bool):
        raise Refused(f"full must be true or false, not {full!r}")
    line = _body(_envelope(t, "refuelling", subject, id), anchor=anchor, exact=exact, replaces=replaces)
    line["quantity_l"] = _amount("quantity_l", quantity_l, positive=True)
    line["total_price"] = None if total_price is None else _amount("total_price", total_price, positive=False)
    line["unit_price"] = None if unit_price is None else _amount("unit_price", unit_price, positive=False)
    line["full"] = full
    line.update(place=place, fuel=fuel, note=note)
    return line


def charging(t: str, subject: Subject, *, anchor: str, energy_kwh: float, total_price: float,
             exact: bool = False, place: str | None = None, provider: str | None = None,
             note: str | None = None, replaces: str | None = None,
             id: str | None = None) -> dict:
    """A charging receipt (BEL-01): billed energy and its price."""
    line = _body(_envelope(t, "charging", subject, id), anchor=anchor, exact=exact, replaces=replaces)
    line["energy_kwh"] = _amount("energy_kwh", energy_kwh, positive=True)
    line["total_price"] = _amount("total_price", total_price, positive=False)
    line.update(place=place, provider=provider, note=note)
    return line


def cancel(t: str, subject: Subject, *, cancels: str, note: str | None = None,
           id: str | None = None) -> dict:
    """A receipt that takes another out (BEL-02)."""
    line = _envelope(t, "cancel", subject, id)
    line["cancels"] = cancels
    line["note"] = note
    return line


# --- reading -----------------------------------------------------------

@dataclass(frozen=True)
class Read:
    line: dict
    number: int


def read(base: Path, subject: Subject) -> Iterator[Read]:
    """Every receipt line, in file order; a torn last line is skipped, a
    line of an unknown kind is passed over like an unknown L0 kind."""
    p = layout.receipts_file(base, subject)
    if not p.is_file():
        return
    for number, text, torn in l0._raw_lines(p):
        try:
            line = json.loads(text)
        except json.JSONDecodeError:
            if torn:
                return
            raise l0.TornLine(f"{p}:{number}: not JSON and not the last line") from None
        if not isinstance(line, dict):
            raise l0.TornLine(f"{p}:{number}: not a JSON object")
        if line.get("v", 0) > VERSION:
            raise ValueError(f"{p}:{number}: receipt version {line.get('v')} is newer "
                             f"than this reader ({VERSION})")
        if line.get("kind") not in KINDS or not line.get("id"):
            continue
        yield Read(line, number)


@dataclass
class Ledger:
    """The receipts of one vehicle: every line, and which of them count."""

    lines: dict[str, dict] = field(default_factory=dict)
    replaced_by: dict[str, str] = field(default_factory=dict)
    cancelled_by: dict[str, str] = field(default_factory=dict)

    def is_current(self, id: str) -> bool:
        line = self.lines.get(id)
        return (line is not None and line["kind"] in EVENT_KINDS
                and id not in self.replaced_by and id not in self.cancelled_by)

    def current(self, kind: str | None = None) -> list[dict]:
        """The receipts that count, by anchor, then id: never by entry order."""
        out = [ln for i, ln in self.lines.items() if self.is_current(i)
               and (kind is None or ln["kind"] == kind)]
        return sorted(out, key=lambda ln: (clock.parse(ln["anchor"]), ln["id"]))


def ledger(base: Path, subject: Subject) -> Ledger:
    led = Ledger()
    for r in read(base, subject):
        line = r.line
        led.lines.setdefault(line["id"], line)
        if line.get("replaces"):
            led.replaced_by.setdefault(line["replaces"], line["id"])
        if line.get("cancels"):
            led.cancelled_by.setdefault(line["cancels"], line["id"])
    return led


# --- writing -----------------------------------------------------------

def check(led: Ledger, line: dict) -> None:
    """Refuse what would make the history a tree, or point nowhere (ADR-0013, 2)."""
    if line["id"] in led.lines:
        raise Refused(f"receipt {line['id']} exists already")
    target = line.get("replaces") or line.get("cancels")
    if target is None:
        return
    old = led.lines.get(target)
    if old is None:
        raise Refused(f"no receipt {target}")
    if old["subject"] != line["subject"]:
        raise Refused(f"receipt {target} belongs to another vehicle")
    if not led.is_current(target):
        why = ("it is a cancellation" if old["kind"] == "cancel"
               else f"it was replaced by {led.replaced_by[target]}" if target in led.replaced_by
               else f"it was cancelled by {led.cancelled_by[target]}")
        raise Refused(f"receipt {target} is not current: {why}")
    if line.get("replaces") and old["kind"] != line["kind"]:
        raise Refused(f"a correction keeps the kind: {target} is a {old['kind']} receipt")


def append(base: Path, subject: Subject, line: dict) -> Path:
    """Check the line against the file, then append it whole, flushed and
    synced — like an L0 line. Changes the receipts hash, so the next
    derivation rebuilds L1 (ADR-0009, 4)."""
    if line.get("subject") != subject.id:
        raise Refused("receipt and vehicle disagree on the subject")
    check(ledger(base, subject), line)
    p = layout.receipts_file(base, subject)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(l0.encode(line) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return p


# --- matching (ADR-0013, 4) --------------------------------------------

def distance_s(anchor: str, event: dict) -> float:
    """0 inside ``[start, end]``, else the distance to the nearer end. A
    refuelling whose fuel flap was seen open is that moment (ADR-0022)."""
    a = clock.parse(anchor)
    start, end = clock.parse(event["start"]), clock.parse(event["end"])
    if event.get("flap_opened_at"):
        start = end = clock.parse(event["flap_opened_at"])
    if a < start:
        return (start - a).total_seconds()
    if a > end:
        return (a - end).total_seconds()
    return 0.0


@dataclass
class Match:
    """One kind's pairing: event index -> receipt, and what became of the rest."""

    assigned: dict[int, str] = field(default_factory=dict)
    contenders: dict[int, list[str]] = field(default_factory=dict)
    own: list[str] = field(default_factory=list)        # met nothing: an event of their own
    ambiguous: list[str] = field(default_factory=list)


def _claim(receipts: list[dict], nearest: dict[str, list[int]], m: Match) -> None:
    """Assign a receipt only when it has one nearest event and is that
    event's only claimant; everything else is ambiguous, never guessed."""
    claimants: dict[int, list[str]] = {}
    for r in receipts:
        for i in nearest[r["id"]]:
            claimants.setdefault(i, []).append(r["id"])
    for r in receipts:
        near = nearest[r["id"]]
        if len(near) == 1 and claimants[near[0]] == [r["id"]]:
            m.assigned[near[0]] = r["id"]
        else:
            m.ambiguous.append(r["id"])
    for i, ids in claimants.items():
        if i not in m.assigned:
            m.contenders[i] = sorted(ids)


def match(receipts: list[dict], events: list[dict], tolerance_s: float) -> Match:
    """Pair one kind's current receipts with its events.

    Exact receipts first, without tolerance; then the nearest remaining
    event within the tolerance. Depends on nothing but its arguments.
    """
    m = Match()
    by_start: dict = {}
    for i, e in enumerate(events):
        by_start.setdefault(clock.parse(e["start"]), []).append(i)
    exact = [r for r in receipts if r.get("exact") and clock.parse(r["anchor"]) in by_start]
    _claim(exact, {r["id"]: by_start[clock.parse(r["anchor"])] for r in exact}, m)
    taken = set(m.assigned) | set(m.contenders)
    rest = [r for r in receipts if r not in exact]
    nearest: dict[str, list[int]] = {}
    for r in rest:
        dist = [(distance_s(r["anchor"], e), i) for i, e in enumerate(events) if i not in taken]
        dist = [(d, i) for d, i in dist if d <= tolerance_s]
        best = min((d for d, _ in dist), default=None)
        nearest[r["id"]] = [i for d, i in dist if d == best]
    _claim([r for r in rest if nearest[r["id"]]], nearest, m)
    m.own = [r["id"] for r in rest if not nearest[r["id"]]]
    m.ambiguous.sort()
    return m


# --- what an event carries (ADR-0013, 5) -------------------------------

def _price(r: dict) -> tuple[float, float]:
    total, unit = r.get("total_price"), r.get("unit_price")
    if total is None:
        total = round(unit * r["quantity_l"], 2)
    if unit is None:
        unit = round(total / r["quantity_l"], 3)
    return total, unit


def _plausibility(e: dict, receipt_value: float, sensor_value, threshold_pct: float) -> None:
    if sensor_value is None:
        e["deviation_pct"], e["implausible"] = None, False
        return
    dev = abs(receipt_value - sensor_value) / receipt_value * 100
    e["deviation_pct"] = round(dev, 1)
    e["implausible"] = dev > threshold_pct


def _carry(e: dict, r: dict, threshold_pct: float) -> dict:
    """An event with its receipt's values first (BEL-07), the sensor value
    it displaced beside it, and the check between them (BEL-08)."""
    e = dict(e)
    e["receipt"], e["confirmation"] = r["id"], RECEIPT
    if r["kind"] == "refuelling":
        total, unit = _price(r)
        e["quantity_l"], e["quantity_quality"] = r["quantity_l"], RECEIPT
        e["price"], e["unit_price"], e["price_quality"] = total, unit, RECEIPT
        e["full"] = r["full"]
        _plausibility(e, r["quantity_l"], e.get(SENSOR_DELTA_KEY), threshold_pct)
    else:
        sensor = e.get(GRID_KEY)
        if sensor is not None:
            e["sensor_grid_kwh"] = sensor
            e["sensor_grid_kwh_quality"] = e.get(GRID_QUALITY_KEY)
            e["sensor_grid_kwh_source"] = e.get(GRID_SOURCE_KEY)
        kwh = r["energy_kwh"]
        e[GRID_KEY], e[GRID_QUALITY_KEY], e[GRID_SOURCE_KEY] = kwh, RECEIPT, RECEIPT
        e[COST_KEY], e[COST_QUALITY_KEY] = r["total_price"], RECEIPT
        # LAD-09: billed energy is as good as a meter for these two.
        delta, battery = e.get("delta_soc_pct"), e.get("battery_kwh")
        if delta:
            e["kwh_per_pct"] = round(kwh / delta, 4)
        if battery is not None:
            e["charging_loss_kwh"] = round(kwh - battery, 3)
        _plausibility(e, kwh, sensor, threshold_pct)
    for key in TEXT_FIELDS[r["kind"]]:
        e[key] = r.get(key)
    return e


def _waiting(e: dict, contenders: list[str] | None) -> dict:
    e = dict(e)
    e["receipt"] = None
    if contenders:
        e["confirmation"], e["contenders"] = "ambiguous", contenders
    elif e["kind"] == "charging" and e.get(CHARGEPOINT_KEY) not in (None, FOREIGN):
        e["confirmation"] = "chargepoint"
    else:
        e["confirmation"] = "unconfirmed"
    return e


def own_event(r: dict, threshold_pct: float) -> dict:
    """A receipt that met nothing: a confirmed event of its own (BEL-06)."""
    e = {"kind": r["kind"], "subject": r["subject"], "start": r["anchor"], "end": r["anchor"],
         "quality": RECEIPT}
    e = _carry(e, r, threshold_pct)
    e["version"] = __version__
    return e


def apply(kind: str, events: list[dict], receipts: list[dict], *,
          tolerance_s: float, plausibility_pct: float) -> list[dict]:
    """One kind's events as L1 holds them: matched, waiting or ambiguous,
    plus the receipts that met nothing, in order of start."""
    mine = [r for r in receipts if r["kind"] == kind]
    m = match(mine, events, tolerance_s)
    by_id = {r["id"]: r for r in mine}
    out = [_carry(e, by_id[m.assigned[i]], plausibility_pct) if i in m.assigned
           else _waiting(e, m.contenders.get(i)) for i, e in enumerate(events)]
    out += [own_event(by_id[i], plausibility_pct) for i in m.own]
    return sorted(out, key=lambda e: clock.parse(e["start"]))


def is_own(event: dict) -> bool:
    """Whether an event was made from a receipt alone, not detected."""
    return event.get("quality") == RECEIPT


def pairing(receipts: list[dict], events_by_kind: dict[str, list[dict]],
            tolerance_s: float) -> list[dict]:
    """The matching, receipt by receipt — what ``vledger derive match`` prints."""
    out = []
    for kind in EVENT_KINDS:
        mine = [r for r in receipts if r["kind"] == kind]
        events = events_by_kind.get(kind, [])
        m = match(mine, events, tolerance_s)
        event_of = {rid: i for i, rid in m.assigned.items()}
        for r in mine:
            row = {"receipt": r["id"], "kind": kind, "anchor": r["anchor"], "exact": r.get("exact", False)}
            if r["id"] in event_of:
                e = events[event_of[r["id"]]]
                row.update(match="event", event_start=e["start"],
                           distance_s=distance_s(r["anchor"], e))
            elif r["id"] in m.own:
                row["match"] = "own"
            else:
                row["match"] = "ambiguous"
                row["events"] = sorted(events[i]["start"] for i, ids in m.contenders.items()
                                       if r["id"] in ids)
            out.append(row)
    return out
