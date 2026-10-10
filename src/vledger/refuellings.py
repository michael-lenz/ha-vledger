# SPDX-License-Identifier: BSD-3-Clause
"""Refuelling candidates: a fuel rise at unchanged odometer (TNK-01 to
TNK-03, TNK-05).

A rise is a step between two consecutive fuel samples of at least the
refuelling threshold, in litres, while the odometer did not go up — or,
where no odometer is assigned, while no other movement role moved. Rises
that follow one another within T_settle at unchanged odometer are one
refuelling: a pump that the sensor sees in several steps. Its level after
is read only once T_settle has elapsed after the last rise, judged by the
stream's last line, not by the clock (ABL-01), and a candidate is complete
only then — which is what L1 holds (ADR-0009). No fuel flap is needed
(TNK-03).

A rise across movement counts too when the vehicle stopped and started
again in between — a start marker lies between the two samples — or its
fuel flap opened: a vehicle that reports its level once per driving cycle
shows the refuelling only at the next stop (ADR-0022). Such a candidate
spans the two samples and its level after is the later one.

What the sampling cannot show, the candidate cannot show either: a rise
between two odometer samples taken while driving looks like one at rest,
and the threshold is what keeps a sloshing tank from being a refuelling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

from vledger import __version__, clock, l1, series, trips
from vledger.layout import Subject
from vledger.series import Sample, Stream

MEASURED, ESTIMATED, INCOMPLETE = "measured", "estimated", "incomplete"


@dataclass
class Refuelling:
    kind: str
    subject: str
    start: str                         # the first sample that showed the rise
    end: str                           # the last; T_settle runs from here
    quality: str                       # measured, or incomplete across a gap
    position: dict | None
    zone: str | None                   # the tracker's zone (FAH-07)
    level_before_l: float | None
    level_after_l: float | None        # read once T_settle has elapsed
    settled_at: str | None             # the time of that reading
    sensor_delta_l: float | None
    sensor_delta_quality: str          # estimated, always (TNK-02)
    price_suggestion: float | None     # the fuel_price role at start (TNK-05)
    flap_opened_at: str | None         # the fuel flap's opening within it (ADR-0022)
    version: str


def cannot_detect(s: Stream) -> str | None:
    """Why this stream can yield no candidate at all, or ``None``: the
    flagged reason TNK-01 asks for when % cannot become litres."""
    if "fuel_level" in s.unconverted:
        return s.unconverted["fuel_level"]
    if s.config is not None and "fuel_level" not in (s.config.get("roles") or {}):
        return "no fuel_level role is assigned"
    return None


def _moved(s: Stream, a: str, b: str) -> bool:
    """Whether the vehicle moved after ``a`` and up to ``b``: the odometer
    went up, or — without an odometer — any other movement role moved."""
    ta, tb = clock.parse(a), clock.parse(b)
    odo = s.series.get("odometer", [])
    if odo:
        ref = series.last_at_or_before(odo, a)
        return any(ta < clock.parse(x.t) <= tb and (ref is None or x.value > ref.value)
                   for x in odo)
    return any(ta < clock.parse(m.t) <= tb for m in trips.movements(s))


def _rises(s: Stream, threshold_l: float) -> list[tuple[Sample, Sample]]:
    """Every step of at least the threshold at unchanged odometer."""
    fuel = s.series.get("fuel_level", [])
    return [(a, b) for a, b in pairwise(fuel)
            if b.value - a.value >= threshold_l and not _moved(s, a.t, b.t)]


def _flap_openings(s: Stream) -> list[trips.Marker]:
    return trips.markers(s, {"fuel_flap": "open"})


def _rises_across_a_stop(s: Stream, threshold_l: float) -> list[tuple[Sample, Sample]]:
    """Every step of at least the threshold across movement, with a start
    marker or a fuel flap opening between the two samples (ADR-0022)."""
    evidence = [clock.parse(m.t) for m in trips.markers(s, trips.START_MARKERS) + _flap_openings(s)]
    fuel = s.series.get("fuel_level", [])
    out = []
    for a, b in pairwise(fuel):
        if b.value - a.value < threshold_l or not _moved(s, a.t, b.t):
            continue
        ta, tb = clock.parse(a.t), clock.parse(b.t)
        if any(ta < t < tb for t in evidence):
            out.append((a, b))
    return out


def _flap_opened(s: Stream, a: str, b: str) -> str | None:
    ta, tb = clock.parse(a), clock.parse(b)
    return next((m.t for m in _flap_openings(s) if ta <= clock.parse(m.t) <= tb), None)


def _settled(s: Stream, end: str, settle_s: float) -> tuple[Sample | None, bool, bool]:
    """(the reading T_settle after ``end``, across a gap, still waiting).

    The reading is the value in effect once T_settle has elapsed; if the
    sensor had dropped out by then, the first value after it came back.
    Across a capture gap there is no reading (ABL-04)."""
    due = clock.to_text(clock.parse(end) + timedelta(seconds=settle_s))
    if s.last_t is None or clock.parse(s.last_t) < clock.parse(due):
        return None, False, True
    if series.gap_between(s, end, due):
        return None, True, False
    x = series.in_effect(s, "fuel_level", due)
    if x is not None:
        return x, False, False
    after = next((y for y in s.series.get("fuel_level", [])
                  if clock.parse(y.t) > clock.parse(due)), None)
    if after is None:
        return None, False, True
    if series.gap_between(s, due, after.t):
        return None, True, False
    return after, False, False


def derive(s: Stream, *, completed_only: bool = False) -> list[Refuelling]:
    """Every refuelling candidate in the stream, in order (TNK-01 to TNK-05).

    With ``completed_only``, only those whose level after has been read —
    T_settle elapsed after the last rise, by the stream — which is what L1
    holds (ADR-0009).
    """
    if cannot_detect(s):
        return []
    thr = s.thresholds()
    threshold, settle = float(thr["refuel_threshold_l"]), float(thr["t_settle_s"])
    groups: list[list[tuple[Sample, Sample]]] = []
    for a, b in _rises(s, threshold):
        if groups:
            last = groups[-1][-1][1]
            if (clock.parse(b.t) - clock.parse(last.t) <= timedelta(seconds=settle)
                    and not _moved(s, last.t, b.t)):
                groups[-1].append((a, b))
                continue
        groups.append([(a, b)])
    out: list[Refuelling] = []
    for a, b in _rises_across_a_stop(s, threshold):
        # The level after is the later reading itself: T_settle has long
        # elapsed, and no reading falls inside it (ADR-0022, point 2).
        fix = series.last_at_or_before(s.fixes, a.t)
        price = series.in_effect(s, "fuel_price", a.t)
        out.append(Refuelling(
            kind="refuelling", subject=s.subject.id, start=a.t, end=b.t,
            quality=INCOMPLETE if series.gap_between(s, a.t, b.t) else MEASURED,
            position=series.fix_dict(fix), zone=fix.zone if fix else None,
            level_before_l=round(a.value, 3), level_after_l=round(b.value, 3),
            settled_at=b.t, sensor_delta_l=round(b.value - a.value, 3),
            sensor_delta_quality=ESTIMATED,
            price_suggestion=price.value if price else None,
            flap_opened_at=_flap_opened(s, a.t, b.t),
            version=__version__,
        ))
    for steps in groups:
        before, start, end = steps[0][0], steps[0][1].t, steps[-1][1].t
        after, gap_after, waiting = _settled(s, end, settle)
        if waiting and completed_only:
            continue
        crossed = gap_after or any(series.gap_between(s, a.t, b.t) for a, b in steps)
        fix = series.last_at_or_before(s.fixes, start)
        price = series.in_effect(s, "fuel_price", start)
        out.append(Refuelling(
            kind="refuelling", subject=s.subject.id, start=start, end=end,
            quality=INCOMPLETE if crossed else MEASURED,
            position=series.fix_dict(fix), zone=fix.zone if fix else None,
            level_before_l=round(before.value, 3),
            level_after_l=round(after.value, 3) if after else None,
            settled_at=after.t if after else None,
            sensor_delta_l=round(after.value - before.value, 3) if after else None,
            sensor_delta_quality=ESTIMATED,
            price_suggestion=price.value if price else None,
            flap_opened_at=_flap_opened(s, before.t, after.t if after else end),
            version=__version__,
        ))
    out.sort(key=lambda r: clock.parse(r.start))
    return out


def derive_from(base: Path, subject: Subject, *, since: str | None = None,
                until: str | None = None, completed_only: bool = False) -> list[Refuelling]:
    return derive(series.load(base, subject, since=since, until=until), completed_only=completed_only)


def to_dict(refuelling: Refuelling) -> dict:
    return asdict(refuelling)


def _completed_dicts(base: Path, subject: Subject, since: str | None) -> list[dict]:
    return [to_dict(r) for r in derive_from(base, subject, since=since, completed_only=True)]


l1.DERIVATIONS["refuelling"] = _completed_dicts
