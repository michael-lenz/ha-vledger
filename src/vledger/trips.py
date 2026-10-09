# SPDX-License-Identifier: BSD-3-Clause
"""Trips: the spans between standstills (FAH-01 to FAH-07).

A movement event is a change of a movement role that means the vehicle
moved: an odometer or trip counter going up, a position fix that moved. A
standstill is at least T_still without one. A trip runs from the first
movement event after a standstill to the last before the next, refined by
the ignition where it is assigned (FAH-02) — never defined by it.

What the sampling cannot show, the trip cannot show either: a stop shorter
than the sampling interval merges into the trip (FAH-06), and a trip's
start is the first sample that moved, not the moment the wheels turned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

from vledger import __version__, clock, geo, l0, l1, series
from vledger.layout import Subject
from vledger.series import Fix, Sample, Stream

MEASURED, ESTIMATED, INCOMPLETE = "measured", "estimated", "incomplete"


@dataclass(frozen=True)
class Movement:
    t: str
    role: str


@dataclass
class Trip:
    kind: str
    subject: str
    start: str
    end: str
    quality: str                       # measured, or incomplete across a gap
    distance_km: float | None
    distance_quality: str | None       # measured or estimated
    distance_source: str | None        # odometer, trip_counter, waypoints
    start_position: dict | None
    end_position: dict | None
    start_zone: str | None
    end_zone: str | None
    waypoints: list[dict]
    outside_temperature_c: float | None
    delta_soc_pct: float | None        # estimated, FAH-05
    delta_fuel_l: float | None         # estimated, FAH-05
    refined_by_ignition: bool
    version: str


def _moved(a: Fix, b: Fix, min_km: float) -> bool:
    return geo.distance_km(a.latitude, a.longitude, b.latitude, b.longitude) >= min_km


def movements(s: Stream, *, min_move_km: float = 0.05) -> list[Movement]:
    """Every sample that says the vehicle moved, in time order.

    An odometer only counts going up; a trip counter going up counts, going
    down is a reset (FAH-04); a fix counts when it is at least
    ``min_move_km`` from the previous fix, so GPS jitter at rest is not a
    trip.
    """
    out: list[Movement] = []
    for role in ("odometer", "trip_distance"):
        prev: Sample | None = None
        for x in s.series.get(role, []):
            if prev is not None and x.value > prev.value:
                out.append(Movement(x.t, role))
            prev = x
    prev_fix: Fix | None = None
    for f in s.fixes:
        if prev_fix is not None and _moved(prev_fix, f, min_move_km):
            out.append(Movement(f.t, "position"))
        prev_fix = f
    out.sort(key=lambda m: clock.parse(m.t))
    return out


def _spans(moves: list[Movement], t_still_s: float, gaps: list[l0.Gap]) -> list[tuple[str, str, bool]]:
    """(first movement, last movement, crosses a gap) per trip."""
    if not moves:
        return []
    still = timedelta(seconds=t_still_s)
    gap_ranges = [(clock.parse(g.start), clock.parse(g.end)) for g in gaps]

    def gap_between(a: str, b: str) -> bool:
        ta, tb = clock.parse(a), clock.parse(b)
        return any(gs < tb and ge > ta for gs, ge in gap_ranges)

    spans: list[tuple[str, str, bool]] = []
    start = last = moves[0].t
    incomplete = False
    for m in moves[1:]:
        if clock.parse(m.t) - clock.parse(last) >= still or gap_between(last, m.t):
            crossed = gap_between(last, m.t)
            spans.append((start, last, incomplete or crossed))
            start, incomplete = m.t, crossed
        last = m.t
    spans.append((start, last, incomplete))
    return spans


def _refine(s: Stream, start: str, end: str, t_still_s: float) -> tuple[str, str, bool]:
    """Ignition on shortly before the first movement starts the trip; off
    shortly after the last ends it (FAH-02). Bounded by T_still, and never
    the sole criterion: without movement there is no trip."""
    ign = s.domain.get("ignition")
    if not ign:
        return start, end, False
    still = timedelta(seconds=t_still_s)
    refined = False
    ts, te = clock.parse(start), clock.parse(end)
    for x in reversed(ign):
        tx = clock.parse(x.t)
        if tx > ts:
            continue
        if x.state == "on" and ts - tx <= still:
            start, refined = x.t, True
        break
    for x in ign:
        tx = clock.parse(x.t)
        if tx < te:
            continue
        if x.state is not None and x.state != "on" and tx - te <= still:
            end, refined = x.t, True
        break
    return start, end, refined


def _strictly_before(samples: list, t: str):
    limit = clock.parse(t)
    best = None
    for x in samples:
        if clock.parse(x.t) < limit:
            best = x
        else:
            break
    return best


def _gap_between(s: Stream, a: str, b: str) -> bool:
    ta, tb = clock.parse(a), clock.parse(b)
    return any(clock.parse(g.start) < tb and clock.parse(g.end) > ta for g in s.gaps)


def _start_value(s: Stream, samples: list, start: str):
    """The value a trip starts from: the last sample before its first
    movement — unless a capture gap lies between the two, or there is none,
    in which case the sample at the start itself. Nothing is read across a
    gap (ABL-04)."""
    before = _strictly_before(samples, start)
    if before is None or _gap_between(s, before.t, start):
        return series.last_at_or_before(samples, start)
    return before


def _distance(s: Stream, start: str, end: str, waypoints: list[Fix]) -> tuple[float | None, str | None, str | None]:
    """FAH-04: odometer difference, else a monotone trip counter, else the
    straight lines between waypoints."""
    odo = s.series.get("odometer", [])
    before = _start_value(s, odo, start)
    at_end = series.last_at_or_before(odo, end)
    if before and at_end and at_end.value >= before.value:
        return round(at_end.value - before.value, 3), MEASURED, "odometer"
    counter = s.series.get("trip_distance", [])
    before = _start_value(s, counter, start)
    inside = series.between(counter, start, end)
    if before and inside:
        values = [before.value] + [x.value for x in inside]
        if all(b >= a for a, b in pairwise(values)):
            return round(values[-1] - values[0], 3), MEASURED, "trip_counter"
    if len(waypoints) >= 2:
        km = sum(geo.distance_km(a.latitude, a.longitude, b.latitude, b.longitude)
                 for a, b in pairwise(waypoints))
        return round(km, 3), ESTIMATED, "waypoints"
    return None, None, None


def _delta(s: Stream, role: str, start: str, end: str, settle_s: float) -> float | None:
    """Value after the trip (settled) minus the value before it — estimated
    always (FAH-05)."""
    before = _start_value(s, s.series.get(role, []), start)
    settled_until = clock.to_text(clock.parse(end) + timedelta(seconds=settle_s))
    after = series.last_at_or_before(s.series.get(role, []), settled_until)
    if before is None or after is None or clock.parse(after.t) < clock.parse(start):
        return None
    return round(after.value - before.value, 3)


def _fix_dict(f: Fix | None) -> dict | None:
    if f is None:
        return None
    d = {"t": f.t, "latitude": f.latitude, "longitude": f.longitude}
    if f.accuracy_m is not None:
        d["accuracy_m"] = f.accuracy_m
    return d


def derive(s: Stream, *, completed_only: bool = False) -> list[Trip]:
    """Every trip in the stream, in order (FAH-01 to FAH-07).

    With ``completed_only``, only trips whose standstill has elapsed —
    T_still after the last movement, judged by the stream's last line, not
    by the clock, so the answer is the same whenever it is asked (ABL-01)
    — which is what L1 holds (ADR-0009).
    """
    thr = s.thresholds()
    t_still = float(thr["t_still_s"])
    settle = float(thr["t_settle_s"])
    out: list[Trip] = []
    spans = _spans(movements(s), t_still, s.gaps)
    if completed_only and spans and s.last_t:
        first, last, crossed = spans[-1]
        if clock.parse(s.last_t) - clock.parse(last) < timedelta(seconds=t_still):
            spans = spans[:-1]
    for first, last, crossed in spans:
        start, end, refined = _refine(s, first, last, t_still)
        # Where the vehicle was before it moved, then every fix while moving.
        start_fix = _strictly_before(s.fixes, start) or series.last_at_or_before(s.fixes, start)
        inside = series.between(s.fixes, start, end)
        waypoints = ([start_fix] if start_fix and start_fix not in inside else []) + inside
        end_fix = waypoints[-1] if waypoints else None
        km, kq, ksrc = _distance(s, start, end, waypoints)
        temps = series.between(s.series.get("outside_temperature", []), start, end)
        temp = round(sum(x.value for x in temps) / len(temps), 1) if temps else None
        out.append(Trip(
            kind="trip", subject=s.subject.id, start=start, end=end,
            quality=INCOMPLETE if crossed else MEASURED,
            distance_km=km, distance_quality=kq, distance_source=ksrc,
            start_position=_fix_dict(start_fix), end_position=_fix_dict(end_fix),
            start_zone=start_fix.zone if start_fix else None,
            end_zone=end_fix.zone if end_fix else None,
            waypoints=[_fix_dict(f) for f in waypoints],
            outside_temperature_c=temp,
            delta_soc_pct=_delta(s, "soc", start, end, settle),
            delta_fuel_l=_delta(s, "fuel_level", start, end, settle),
            refined_by_ignition=refined, version=__version__,
        ))
    return out


def derive_from(base: Path, subject: Subject, *, since: str | None = None,
                until: str | None = None, completed_only: bool = False) -> list[Trip]:
    return derive(series.load(base, subject, since=since, until=until), completed_only=completed_only)


def to_dict(trip: Trip) -> dict:
    return asdict(trip)


def _completed_dicts(base: Path, subject: Subject, since: str | None) -> list[dict]:
    return [to_dict(t) for t in derive_from(base, subject, since=since, completed_only=True)]


l1.DERIVATIONS["trip"] = _completed_dicts
