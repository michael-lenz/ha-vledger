# SPDX-License-Identifier: BSD-3-Clause
"""Trips: the spans between standstills (FAH-01 to FAH-07).

A movement event is a change of a movement role that means the vehicle
moved: an odometer or trip counter going up, a position fix that moved. A
standstill is at least T_still without one. A trip runs from the first
movement event after a standstill to the last before the next, refined by
the markers of ignition, plug state, charging state, lock and engine where
they are assigned (FAH-02, ADR-0012, ADR-0021) — never defined by them. A
vehicle reported in use is moving (ADR-0021).

What the sampling cannot show, the trip cannot show either: a stop shorter
than the sampling interval merges into the trip (FAH-06), and a trip's
start is the first sample that moved, not the moment the wheels turned.

A trip carries its own consumption (ADR-0025): fuel from the trip computer
where the vehicle has one, else from the level, and the battery side from
the SoC — each with its error, and a rate only where the quantity exceeds
that error. It is the trip's figure, never the vehicle's (VER-01, VER-10).
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

from vledger import __version__, clock, geo, l0, l1, periods, series
from vledger.layout import Subject
from vledger.series import Fix, Sample, Stream

MEASURED, ESTIMATED, INCOMPLETE = "measured", "estimated", "incomplete"
#: Where a trip's fuel figure was read (ADR-0025, point 2).
TRIP_COMPUTER, FUEL_LEVEL = "trip_computer", "fuel_level"
#: One step of a trip's rate per 100 km, L or kWh (ADR-0026, point 1): the
#: precision the rate entities display, and the trip computer's own display
#: step. A figure whose error over the distance is within it is shown
#: whatever its sign. A constant, not a threshold.
RATE_STEP = 0.1

#: The lines of one poll land milliseconds apart, in an order Home Assistant
#: chooses. A marker this close to a boundary movement is at it (ISSUE-0031),
#: and a slower role's report this close to a faster role's is of the same
#: poll (ADR-0021, point 4) — well under any sampling interval.
POLL_GRACE = timedelta(seconds=1)


@dataclass(frozen=True)
class Movement:
    t: str
    role: str
    #: The sample's own sampling interval, ``t`` minus its reported_before
    #: (ADR-0011); ``None`` where the stream does not say.
    interval_s: float | None = None


def _interval(t: str, reported_before: str | None) -> float | None:
    if reported_before is None:
        return None
    return (clock.parse(t) - clock.parse(reported_before)).total_seconds()


def in_use_spans(s: Stream) -> list[tuple[str, str, float | None]]:
    """(first report, last report, poll interval) of every span the vehicle
    was reported in use (ADR-0021). The last report is the reported_before of
    the line that leaves the state — the last poll that still saw it — or the
    first report where the stream does not say."""
    out: list[tuple[str, str, float | None]] = []
    first = None
    for x in s.domain.get("in_use", []):
        positive = x.state == "in_use"
        if positive and first is None:
            first = x
        elif not positive and first is not None:
            last = x.reported_before
            if last is None or clock.parse(last) < clock.parse(first.t):
                last = first.t
            out.append((first.t, last, _interval(x.t, x.reported_before)))
            first = None
    if first is not None:
        out.append((first.t, first.t, _interval(first.t, first.reported_before)))
    return out


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
    # The trip's consumption (ADR-0025): the quantity with its error, and the
    # rate on the trip's distance, null where the error swallows it.
    fuel_consumed_l: float | None
    fuel_consumed_quality: str | None  # measured from the trip computer, else estimated
    fuel_consumed_source: str | None   # trip_computer or fuel_level
    fuel_consumed_error_l: float | None
    fuel_l_per_100km: float | None
    fuel_l_per_100km_quality: str | None
    fuel_l_per_100km_error_pct: float | None
    battery_consumed_kwh: float | None  # estimated always; negative when the battery gained
    battery_consumed_quality: str | None
    battery_consumed_error_kwh: float | None
    battery_kwh_per_100km: float | None
    battery_kwh_per_100km_quality: str | None
    battery_kwh_per_100km_error_pct: float | None
    refined_by: dict                   # {"start": role | None, "end": role | None}
    movements_while_plugged: int       # contradictions, reported (ADR-0012, point 5)
    version: str


def _moved(a: Fix, b: Fix, min_km: float) -> bool:
    return geo.distance_km(a.latitude, a.longitude, b.latitude, b.longitude) >= min_km


def movements(s: Stream) -> list[Movement]:
    """Every sample that says the vehicle moved, in time order.

    An odometer only counts going up; a trip counter going up counts, going
    down is a reset (FAH-04); a fix counts when it is at least the
    vehicle's ``min_move_m`` from the previous fix, so GPS jitter at rest
    is not a trip (ADR-0030); a span in use counts at its first and its
    last report (ADR-0021).
    """
    min_move_km = float(s.thresholds()["min_move_m"]) / 1000
    out: list[Movement] = []
    for role in ("odometer", "trip_distance"):
        prev: Sample | None = None
        for x in s.series.get(role, []):
            if prev is not None and x.value > prev.value:
                out.append(Movement(x.t, role, _interval(x.t, x.reported_before)))
            prev = x
    prev_fix: Fix | None = None
    for f in s.fixes:
        if prev_fix is not None and _moved(prev_fix, f, min_move_km):
            out.append(Movement(f.t, "position", _interval(f.t, f.reported_before)))
        prev_fix = f
    for first, last, interval in in_use_spans(s):
        out.append(Movement(first, "in_use", interval))
        if last != first:
            out.append(Movement(last, "in_use", interval))
    out.sort(key=lambda m: clock.parse(m.t))
    return out


def _spans(moves: list[Movement], t_still_s: float, gaps: list[l0.Gap],
           in_use: list[tuple[str, str, float | None]] = ()) -> list[tuple[str, str, bool]]:
    """(first movement, last movement, crosses a gap) per trip. No standstill
    lies inside a span the vehicle was reported in use (ADR-0021)."""
    if not moves:
        return []
    still = timedelta(seconds=t_still_s)
    gap_ranges = [(clock.parse(g.start), clock.parse(g.end)) for g in gaps]
    use_ranges = [(clock.parse(a), clock.parse(b)) for a, b, _ in in_use]

    def gap_between(ta, tb) -> bool:
        return any(gs < tb and ge > ta for gs, ge in gap_ranges)

    def in_use_throughout(ta, tb) -> bool:
        return any(ua <= ta and tb <= ub for ua, ub in use_ranges)

    spans: list[tuple[str, str, bool]] = []
    start = last = moves[0].t
    incomplete = False
    for m in moves[1:]:
        tl, tm = clock.parse(last), clock.parse(m.t)
        crossed = gap_between(tl, tm)
        if (tm - tl >= still and not in_use_throughout(tl, tm)) or crossed:
            spans.append((start, last, incomplete or gap_between(tl, min(tm, tl + still))))
            start, incomplete = m.t, crossed
        last = m.t
    # A gap inside a trip's standstill leaves its end unknown; one after T_still
    # had elapsed comes after the completed trip, and only the next trip, which
    # began in it, is incomplete. Either way the answer is fixed the moment the
    # trip completes, whatever the stream holds later (ISSUE-0014).
    tl = clock.parse(last)
    spans.append((start, last, incomplete or gap_between(tl, tl + still)))
    return spans


#: The not-driving markers (ADR-0012): a change into one of these domain
#: states ends driving, a change into one of the start states begins it. The
#: lock and the engine only ever start a trip: the car locks itself on
#: driving off, and an engine stops while the car drives on (ADR-0021).
END_MARKERS = {"ignition": "off", "plug_state": "plugged", "charging_state": "charging"}
START_MARKERS = {"ignition": "on", "plug_state": "unplugged", "engine": "running",
                 "lock": "unlocked"}

#: The domain states in which the vehicle cannot drive.
NOT_DRIVING = {"plug_state": "plugged", "charging_state": "charging"}


@dataclass(frozen=True)
class Marker:
    t: str
    role: str


def markers(s: Stream, wanted: dict[str, str]) -> list[Marker]:
    """Every change into a wanted domain state, in time order. A change is
    judged against the last known state: unavailable and unknown hold, so a
    sensor dropping out and coming back says nothing (ADR-0008)."""
    out: list[Marker] = []
    for role, target in wanted.items():
        known = None
        for x in s.domain.get(role, []):
            if x.state is None:
                continue
            if x.state != known and x.state == target:
                out.append(Marker(x.t, role))
            known = x.state
    out.sort(key=lambda m: clock.parse(m.t))
    return out


def _refine(start: str, end: str, t_still_s: float, starts: list[Marker],
            ends: list[Marker], floor: str | None) -> tuple[str, str, dict]:
    """The latest start marker within T_still before the first movement
    starts the trip; the earliest end marker within T_still after the last
    ends it (ADR-0012). Each is a time the vehicle was not driving, so each
    bounds the true boundary and the closest is the best. Never the sole
    criterion — without movement there is no trip — and never across the
    previous trip's end, ``floor``.

    A marker written in the same poll as the boundary movement, within
    :data:`POLL_GRACE` on its far side, bounds the trip too (ISSUE-0031):
    it moves no boundary, since the movement is the outer line, but
    ``refined_by`` names it."""
    still = timedelta(seconds=t_still_s)
    ts, te = clock.parse(start), clock.parse(end)
    lo = clock.parse(floor) if floor else None
    by = {"start": None, "end": None}
    for m in reversed(starts):
        tm = clock.parse(m.t)
        if tm > ts + POLL_GRACE:
            continue
        if ts - tm <= still and (lo is None or tm > lo):
            start, by["start"] = min(start, m.t, key=clock.parse), m.role
        break
    for m in ends:
        tm = clock.parse(m.t)
        if tm < te - POLL_GRACE:
            continue
        if tm - te <= still:
            end, by["end"] = max(end, m.t, key=clock.parse), m.role
        break
    return start, end, by


def _role_intervals(s: Stream, lo: str | None, hi: str) -> dict[str, float]:
    """Per movement role, the median of the sampling intervals the stream
    measures for it (ADR-0011) from ``lo`` (the previous trip's end, or the
    stream's start) to ``hi``. The window is the same whether the stream is
    derived at once or from the last trip on, so the answer is too."""
    tlo, thi = (clock.parse(lo) if lo else None), clock.parse(hi)

    def inside(t: str) -> bool:
        tt = clock.parse(t)
        return (tlo is None or tt >= tlo) and tt <= thi

    found: dict[str, list[float]] = {}
    for role in ("odometer", "trip_distance"):
        for x in s.series.get(role, []):
            if x.reported_before and inside(x.t):
                found.setdefault(role, []).append(_interval(x.t, x.reported_before))
    for f in s.fixes:
        if f.reported_before and inside(f.t):
            found.setdefault("position", []).append(_interval(f.t, f.reported_before))
    for first, _, interval in in_use_spans(s):
        if interval is not None and inside(first):
            found.setdefault("in_use", []).append(interval)
    return {role: statistics.median(v) for role, v in found.items()}


def in_use_now(s: Stream) -> bool:
    """Whether the stream ends with the vehicle reported in use: a span
    with no line leaving it yet, so the movement it is goes on (ADR-0021)."""
    known = [x.state for x in s.domain.get("in_use", []) if x.state is not None]
    return bool(known) and known[-1] == "in_use"


def _moving_until(moves: list[Movement], intervals: dict[str, float]) -> str:
    """The time the vehicle stopped moving: the last movement, unless it is a
    slower role's late report (ADR-0021, point 4). A role is slower when its
    measured interval is longer; a report is late when it follows the last
    movement of a faster role by no more than its own interval — the old
    value was last heard no later than that movement (:data:`POLL_GRACE`
    for lines of one poll). The faster role stopped changing first, so the
    vehicle had stopped by then."""
    ms = sorted(moves, key=lambda m: clock.parse(m.t))
    i = len(ms) - 1
    while i > 0:
        m = ms[i]
        if m.interval_s is None or m.role not in intervals:
            break
        faster = [f for f in ms[:i] if f.role != m.role
                  and intervals.get(f.role, intervals[m.role]) < intervals[m.role]]
        if not faster or clock.parse(m.t) - clock.parse(faster[-1].t) > timedelta(seconds=m.interval_s) + POLL_GRACE:
            break
        i -= 1
    return ms[i].t


def _while_plugged(s: Stream, moves: list[Movement]) -> int:
    """How many movements fall where plug or charging state says the vehicle
    could not drive — sample timing, or a wrong mapping (ADR-0012, point 5)."""
    known = {role: [(clock.parse(x.t), x.state) for x in s.domain.get(role, [])
                    if x.state is not None]
             for role in NOT_DRIVING}
    n = 0
    for m in moves:
        tm = clock.parse(m.t)
        for role, state in NOT_DRIVING.items():
            i = bisect_right(known[role], tm, key=lambda k: k[0])
            if i and known[role][i - 1][1] == state:
                n += 1
                break
    return n


def _start_value(s: Stream, samples: list, start: str):
    """The value a trip starts from: the last sample before its first
    movement — unless a capture gap lies between the two, or there is none,
    in which case the sample at the start itself. Nothing is read across a
    gap (ABL-04)."""
    before = series.last_before(samples, start)
    if before is None or series.gap_between(s, before.t, start):
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
    after = series.last_at_or_before(s.series.get(role, []), _settled(end, settle_s))
    if before is None or after is None or clock.parse(after.t) < clock.parse(start):
        return None
    return round(after.value - before.value, 3)


def _settled(end: str, settle_s: float) -> str:
    return clock.to_text(clock.parse(end) + timedelta(seconds=settle_s))


def _trip_computer(s: Stream, start: str, end: str, settle_s: float
                   ) -> tuple[float, float] | None:
    """(fuel used, its error) as the trip computer measures them (ADR-0025,
    point 2): the fuel since the counter's reset is trip_distance × the
    average / 100 at a reading, and the trip's fuel is that at the settled
    end minus that before the start — zero where the counter went down in
    between, its reset. The error is half the average's display step over
    the distance at each reading. ``None`` without both roles, or without
    a reading on either side of the trip."""
    dist, avg = s.series.get("trip_distance", []), s.series.get("trip_consumption", [])
    if not dist or not avg:
        return None
    settled = _settled(end, settle_s)
    d0, a0 = _start_value(s, dist, start), _start_value(s, avg, start)
    d1, a1 = series.last_at_or_before(dist, settled), series.last_at_or_before(avg, settled)
    if (d0 is None or a0 is None or d1 is None or a1 is None
            or clock.parse(d1.t) < clock.parse(start) or clock.parse(a1.t) < clock.parse(start)):
        return None
    reset = any(b.value < a.value for a, b in pairwise(series.between(dist, d0.t, d1.t)))
    before = 0.0 if reset else d0.value * a0.value / 100
    half_step = float(s.thresholds()["trip_consumption_step_l_per_100km"]) / 2
    error = half_step * ((0.0 if reset else d0.value) + d1.value) / 100
    return d1.value * a1.value / 100 - before, error


def _rate(used: float | None, error: float | None, km: float | None
          ) -> tuple[float | None, float | None]:
    """(the rate per 100 km, its error as a share of it): the rate over a
    distance where the quantity exceeds its own error, or where that error
    over the distance is within one step of the rate (ADR-0026, point 1);
    the share whenever the quantity is not zero."""
    if used is None or error is None:
        return None, None
    share = round(error / abs(used) * 100, 1) if used else None
    if not km or (abs(used) <= error and error / km * 100 > RATE_STEP):
        return None, share
    return round(used / km * 100, 3) + 0.0, share   # + 0.0: no negative zero


def _consumption(s: Stream, start: str, end: str, settle_s: float, crossed: bool,
                 km: float | None, km_quality: str | None, delta_soc: float | None,
                 delta_fuel: float | None) -> dict:
    """The consumption keys of a trip (ADR-0025): fuel from the trip
    computer, else from the level with the resolution known; the battery
    side from the SoC delta; a trip across a gap is incomplete in all."""
    p = s.parameters()
    gap = INCOMPLETE if crossed else None
    fuel = fuel_q = source = fuel_err = None
    computed = _trip_computer(s, start, end, settle_s)
    res = p.get("fuel_level_resolution_l")
    if computed is not None:
        (fuel, fuel_err), fuel_q, source = computed, periods.weakest(MEASURED, gap), TRIP_COMPUTER
    elif delta_fuel is not None and res:
        fuel, fuel_err = -delta_fuel, 2 * float(res)
        fuel_q, source = periods.weakest(ESTIMATED, gap), FUEL_LEVEL
    fuel_rate, fuel_share = _rate(fuel, fuel_err, km)
    battery = battery_q = battery_err = None
    capacity = p.get("battery_net_kwh")
    if delta_soc is not None and capacity:
        battery = -delta_soc / 100 * float(capacity)
        battery_err = 2 * float(p.get("soc_resolution_pct") or 0) / 100 * float(capacity)
        battery_q = periods.weakest(ESTIMATED, gap)
    battery_rate, battery_share = _rate(battery, battery_err, km)

    def r(x, digits=3):
        return None if x is None else round(x, digits) + 0.0   # no negative zero

    return {
        "fuel_consumed_l": r(fuel), "fuel_consumed_quality": fuel_q,
        "fuel_consumed_source": source, "fuel_consumed_error_l": r(fuel_err),
        "fuel_l_per_100km": fuel_rate,
        "fuel_l_per_100km_quality": periods.weakest(fuel_q, km_quality) if fuel_rate is not None else None,
        "fuel_l_per_100km_error_pct": fuel_share if fuel_rate is not None else None,
        "battery_consumed_kwh": r(battery), "battery_consumed_quality": battery_q,
        "battery_consumed_error_kwh": r(battery_err),
        "battery_kwh_per_100km": battery_rate,
        "battery_kwh_per_100km_quality": (periods.weakest(battery_q, km_quality)
                                          if battery_rate is not None else None),
        "battery_kwh_per_100km_error_pct": battery_share if battery_rate is not None else None,
    }


def _trip(s: Stream, start: str, end: str, values_until: str, crossed: bool,
          refined_by: dict, moves: list[Movement], settle: float) -> Trip:
    """A trip from its boundaries: its time runs from ``start`` to ``end``,
    its values are read up to ``values_until`` — a late report still
    belongs to it."""
    # Where the vehicle was before it moved, then every fix while moving.
    start_fix = series.last_before(s.fixes, start) or series.last_at_or_before(s.fixes, start)
    inside = series.between(s.fixes, start, values_until)
    waypoints = ([start_fix] if start_fix and start_fix not in inside else []) + inside
    end_fix = waypoints[-1] if waypoints else None
    km, kq, ksrc = _distance(s, start, values_until, waypoints)
    temp = series.mean(series.between(s.series.get("outside_temperature", []), start, end))
    delta_soc = _delta(s, "soc", start, values_until, settle)
    delta_fuel = _delta(s, "fuel_level", start, values_until, settle)
    return Trip(
        kind="trip", subject=s.subject.id, start=start, end=end,
        quality=INCOMPLETE if crossed else MEASURED,
        distance_km=km, distance_quality=kq, distance_source=ksrc,
        start_position=series.fix_dict(start_fix), end_position=series.fix_dict(end_fix),
        start_zone=start_fix.zone if start_fix else None,
        end_zone=end_fix.zone if end_fix else None,
        waypoints=[series.fix_dict(f) for f in waypoints],
        outside_temperature_c=temp,
        delta_soc_pct=delta_soc,
        delta_fuel_l=delta_fuel,
        **_consumption(s, start, values_until, settle, crossed, km, kq, delta_soc, delta_fuel),
        refined_by=refined_by,
        movements_while_plugged=_while_plugged(s, moves),
        version=__version__,
    )


def derive(s: Stream, *, completed_only: bool = False) -> list[Trip]:
    """Every trip in the stream, in order (FAH-01 to FAH-07).

    With ``completed_only``, only trips whose standstill has elapsed —
    T_still after the last movement, judged by the stream's last line, not
    by the clock, so the answer is the same whenever it is asked (ABL-01)
    — which is what L1 holds (ADR-0009). A vehicle that reports once per
    driving cycle is read as legs instead (ADR-0024).
    """
    if s.parameters().get("movement_reporting") == "per_cycle":
        return _derive_per_cycle(s, completed_only=completed_only)
    thr = s.thresholds()
    t_still = float(thr["t_still_s"])
    settle = float(thr["t_settle_s"])
    out: list[Trip] = []
    moves = movements(s)
    use = in_use_spans(s)
    if in_use_now(s) and use and s.last_t:
        # Still in use as far as the stream knows: movement up to its end.
        use[-1] = (use[-1][0], s.last_t, use[-1][2])
    spans = _spans(moves, t_still, s.gaps, use)
    starts, ends = markers(s, START_MARKERS), markers(s, END_MARKERS)
    if completed_only and spans and s.last_t:
        first, last, crossed = spans[-1]
        if (clock.parse(s.last_t) - clock.parse(last) < timedelta(seconds=t_still)
                or in_use_now(s)):
            spans = spans[:-1]
    floor = None
    for first, last, crossed in spans:
        inside_moves = [m for m in moves
                        if clock.parse(first) <= clock.parse(m.t) <= clock.parse(last)]
        until = _moving_until(inside_moves, _role_intervals(s, floor, last))
        start, end, refined_by = _refine(first, until, t_still, starts, ends, floor)
        floor = end
        out.append(_trip(s, start, end, max(end, last, key=clock.parse), crossed,
                         refined_by, inside_moves, settle))
    return out


# --- vehicles that report once per driving cycle (ADR-0024) -----------------

#: What says a leg has begun: the start markers of ADR-0012 and ADR-0021 —
#: the trip counter's reset joins them below.
DEPARTURE_MARKERS = {"lock": "unlocked", "engine": "running", "ignition": "on",
                     "plug_state": "unplugged"}


@dataclass(frozen=True)
class Leg:
    departure: str | None      # None: the leg's start is unknown
    departed_by: str | None    # the role whose marker set it
    arrival: str               # the arrival's first sample
    last: str                  # its last sample: a slower role's late report
    moves: tuple[Movement, ...]


def _departure_markers(s: Stream) -> list[Marker]:
    """Every departure marker, in time order: the start markers, and the
    trip counter going down — its reset comes with a departure (FAH-04)."""
    out = markers(s, DEPARTURE_MARKERS)
    prev = None
    for x in s.series.get("trip_distance", []):
        if prev is not None and x.value < prev.value:
            out.append(Marker(x.t, "trip_distance"))
        prev = x
    out.sort(key=lambda m: clock.parse(m.t))
    return out


def _after_before(t: str, lo: str | None, hi: str) -> bool:
    tt = clock.parse(t)
    return (lo is None or tt > clock.parse(lo)) and tt < clock.parse(hi)


def _measured(s: Stream) -> list[tuple[str, str, float]]:
    """(time, role, interval) of every movement-role sample whose interval
    the stream measures (ADR-0011), in time order."""
    out = []
    for role in ("odometer", "trip_distance"):
        out += [(x.t, role, _interval(x.t, x.reported_before))
                for x in s.series.get(role, []) if x.reported_before]
    out += [(f.t, "position", _interval(f.t, f.reported_before))
            for f in s.fixes if f.reported_before]
    out.sort(key=lambda x: clock.parse(x[0]))
    return out


def _fastest(measured: list[tuple[str, str, float]], lo: str | None, hi: str) -> str | None:
    """The movement role with the shortest median interval measured after
    ``lo`` and up to ``hi`` — the previous trip's end and the sample
    (ADR-0024, point 2) — or ``None`` where none is measured."""
    found: dict[str, list[float]] = {}
    tlo, thi = (clock.parse(lo) if lo else None), clock.parse(hi)
    for t, role, interval in measured:
        tt = clock.parse(t)
        if (tlo is None or tt > tlo) and tt <= thi:
            found.setdefault(role, []).append(interval)
    if not found:
        return None
    return min(found, key=lambda r: (statistics.median(found[r]), r))


def _legs(s: Stream, anchor: str | None, exit_window_s: float
          ) -> tuple[list[Leg], list[Marker], list[str]]:
    """The legs of a per-cycle stream after ``anchor`` — the previous trip's
    end, an arrival — the departure markers that are not exits, and the
    first report of every span in use after it (ADR-0024).

    Only a sample of the fastest movement role opens an arrival, joining
    the current one while no departure marker lies between. A slower
    role's sample belongs to the latest arrival before it when no
    departure lies between, else to the next one after it when none lies
    between those, else it opens its own. A sample with no departure
    between it and the anchor belongs to the arrival the anchor is, which
    is already read. An unlock within ``exit_window_s`` of an arrival is
    getting out."""
    after = (lambda t: True) if anchor is None else (lambda t: clock.parse(t) > clock.parse(anchor))
    moves = [m for m in movements(s) if m.role != "in_use" and after(m.t)]
    marks = [k for k in _departure_markers(s) if after(k.t)]
    measured = _measured(s)
    window = timedelta(seconds=exit_window_s)
    fast = {}
    for m in moves:
        role = _fastest(measured, anchor, m.t)
        fast[m] = role is None or m.role == role

    def near(a: str, b: str) -> bool:
        return abs(clock.parse(a) - clock.parse(b)) <= window

    anchors = [m.t for m in moves if fast[m]] + ([anchor] if anchor else [])
    departures = [k for k in marks
                  if not (k.role == "lock" and any(near(k.t, a) for a in anchors))]

    def departs_between(a: str | None, b: str) -> bool:
        return any(_after_before(k.t, a, b) or k.t == b for k in departures)

    # Arrivals opened and joined by the fastest role.
    arrivals: list[list[Movement]] = []
    for m in (m for m in moves if fast[m]):
        if not arrivals:
            if anchor is not None and not departs_between(anchor, m.t):
                continue                     # the anchor's own, already read
            arrivals.append([m])
        elif departs_between(arrivals[-1][-1].t, m.t):
            arrivals.append([m])
        else:
            arrivals[-1].append(m)
    # Each slower sample by the departures around it.
    for m in (m for m in moves if not fast[m]):
        before = [a for a in arrivals if clock.parse(a[0].t) <= clock.parse(m.t)]
        prev_t = before[-1][0].t if before else anchor
        if prev_t is not None and not departs_between(prev_t, m.t):
            if before:
                before[-1].append(m)
            continue                         # the anchor's: already read
        later = [a for a in arrivals if clock.parse(a[0].t) > clock.parse(m.t)]
        if later and not departs_between(m.t, later[0][0].t):
            later[0].append(m)
            continue
        arrivals.append([m])
        arrivals.sort(key=lambda a: clock.parse(a[0].t))
    for a in arrivals:
        a.sort(key=lambda m: clock.parse(m.t))
    in_use_first = [first for first, _, _ in in_use_spans(s) if after(first)]
    legs: list[Leg] = []
    previous = anchor
    for a in arrivals:
        arrival = next((m.t for m in a if fast[m]), a[0].t)
        unlocks = [k for k in departures if k.role == "lock" and _after_before(k.t, previous, arrival)]
        if unlocks:
            dep, by = unlocks[-1].t, "lock"
        else:
            others = [(k.t, k.role) for k in departures
                      if k.role != "lock" and _after_before(k.t, previous, arrival)]
            others += [(t, "in_use") for t in in_use_first if _after_before(t, previous, arrival)]
            dep, by = min(others, key=lambda x: clock.parse(x[0])) if others else (None, None)
        legs.append(Leg(dep, by, arrival, a[-1].t, tuple(a)))
        previous = arrival
    return legs, departures, in_use_first


def _derive_per_cycle(s: Stream, *, completed_only: bool) -> list[Trip]:
    """Trips of a vehicle that reports once per driving cycle (ADR-0024):
    legs from a departure to an arrival, one trip while the stops between
    them — from an arrival to the next departure — are shorter than
    T_still. Read trip by trip, each from the previous trip's end, as the
    live path reads them from its cursor. Nothing is read across a capture
    gap: a gap in a stop ends the trip, one inside it makes it incomplete."""
    thr = s.thresholds()
    still = timedelta(seconds=float(thr["t_still_s"]))
    settle = float(thr["t_settle_s"])
    window = float(thr["exit_window_s"])
    out: list[Trip] = []
    anchor = s.since
    while True:
        legs, departures, in_use_first = _legs(s, anchor, window)
        if not legs:
            break
        group = [legs[0]]
        complete = False
        for leg in legs[1:]:
            begin = leg.departure or leg.arrival
            if (clock.parse(begin) - clock.parse(group[-1].arrival) < still
                    and not series.gap_between(s, group[-1].arrival, begin)):
                group.append(leg)
            else:
                complete = True
                break
        last = group[-1].arrival
        if not complete:
            nxt = [k.t for k in departures if clock.parse(k.t) > clock.parse(last)]
            nxt += [t for t in in_use_first if clock.parse(t) > clock.parse(last)]
            pending = min(nxt, key=clock.parse) if nxt else None
            complete = bool(s.last_t) and not (
                clock.parse(s.last_t) - clock.parse(last) < still or in_use_now(s)
                or (pending and clock.parse(pending) - clock.parse(last) < still))
        if completed_only and not complete:
            break
        first = group[0]
        start = first.departure or first.arrival
        crossed = series.gap_between(s, start, group[-1].last)
        moves = [m for leg in group for m in leg.moves]
        out.append(_trip(s, start, last, group[-1].last, crossed,
                         {"start": first.departed_by, "end": None}, moves, settle))
        if not complete:
            break
        anchor = last
    return out


def derive_from(base: Path, subject: Subject, *, since: str | None = None,
                until: str | None = None, completed_only: bool = False) -> list[Trip]:
    return derive(series.load(base, subject, since=since, until=until), completed_only=completed_only)


def to_dict(trip: Trip) -> dict:
    return asdict(trip)


def _completed_dicts(base: Path, subject: Subject, since: str | None) -> list[dict]:
    return [to_dict(t) for t in derive_from(base, subject, since=since, completed_only=True)]


l1.DERIVATIONS["trip"] = _completed_dicts
