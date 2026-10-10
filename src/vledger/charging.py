# SPDX-License-Identifier: BSD-3-Clause
"""Charging sessions (LAD-03 to LAD-10).

A session runs from the charging state turning to ``charging`` until it
turns away (LAD-03); unavailable and unknown hold, so a sensor dropping out
mid-charge does not end it (ADR-0008). A vehicle with no charging state
assigned gets sessions from its SoC instead: a rise at standstill above the
charging threshold (LAD-04).

A session is placed at the charge point whose radius holds its position, or
at ``foreign``. Its grid-side energy comes, in this order, from the charge
point's meter where exactly one vehicle charged there (LAD-07),
battery-side energy times the charging loss factor, or nowhere (LAD-06);
its cost from the tariff valid at its start (LAD-08, VER-07). A receipt
that meets the session comes before both, and :mod:`vledger.receipts`
puts it there when L1 is derived (ADR-0013). The meter and the tariffs
are read from the charge point's own stream and config line (ERF-06,
ADR-0009), and the other vehicles' streams say whether anyone else
charged there meanwhile.

A session is complete only once every stream it reads has reached its
end (ADR-0027, point 2) — the charge point's where it has a meter, and
every other vehicle's — each judged by the streams, never by the clock:
a line at or after the end, a stop, or a silence an open gap names. Those
streams are read against the vehicle's own last line, so the live path
and a rebuild judge them alike.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

from vledger import __version__, clock, geo, l1, layout, series, trips
from vledger import config as vconfig
from vledger.layout import Subject
from vledger.series import Stream

MEASURED, ESTIMATED, INCOMPLETE = "measured", "estimated", "incomplete"
FOREIGN = "foreign"


@dataclass(frozen=True)
class Span:
    """A session's boundaries, before anything is read at them."""

    start: str
    end: str
    source: str        # charging_state or soc (LAD-04)
    crossed: bool      # spans a capture gap


@dataclass
class Session:
    kind: str
    subject: str
    start: str
    end: str
    quality: str                       # measured, or incomplete across a gap
    source: str                        # charging_state or soc
    soc_start_pct: float | None
    soc_end_pct: float | None
    delta_soc_pct: float | None
    position: dict | None
    chargepoint: str                   # the charge point's subject, or "foreign"
    chargepoint_name: str | None
    battery_kwh: float | None          # estimated: ΔSoC × net capacity (LAD-05)
    grid_kwh: float | None
    grid_kwh_quality: str | None       # measured or estimated; receipt once one meets it
    grid_kwh_source: str | None        # meter or loss_factor; receipt likewise
    meter_attributable: bool | None    # None: no meter to attribute (LAD-07)
    tariff_eur_per_kwh: float | None
    cost_eur: float | None
    cost_quality: str | None
    kwh_per_pct: float | None          # grid-side kWh per % SoC, from a meter (LAD-09)
    charging_loss_kwh: float | None    # grid − battery, only with a capacity (LAD-09)
    movements_while_charging: int      # contradictions, reported (ADR-0012, point 5)
    version: str


# --- the boundaries ----------------------------------------------------------

def has_charging_state(s: Stream) -> bool:
    return "charging_state" in ((s.config or {}).get("roles") or {}) or bool(s.domain.get("charging_state"))


def _by_charging_state(s: Stream) -> tuple[list[Span], str | None]:
    """The completed sessions, and the start of one still charging."""
    out: list[Span] = []
    known, start = None, None
    for x in s.domain.get("charging_state", []):
        if x.state is None:
            continue
        if x.state == "charging" and known != "charging":
            start = x.t
        elif x.state != "charging" and known == "charging" and start is not None:
            out.append(Span(start, x.t, "charging_state", series.gap_between(s, start, x.t)))
            start = None
        known = x.state
    return out, start


def by_charging_state(s: Stream) -> list[Span]:
    """Every completed session the charging state marks (LAD-03): from a
    change into ``charging`` to the next change out of it. Unavailable and
    unknown hold the last known state; a session still charging at the
    stream's end is not over and is not returned."""
    return _by_charging_state(s)[0]


def by_soc(s: Stream) -> list[Span]:
    """Without a charging state: every run of SoC rises at standstill whose
    total exceeds the charging threshold (LAD-04)."""
    threshold = float(s.thresholds()["charging_threshold_pct"])
    return [Span(r[0].t, r[-1].t, "soc", False)
            for b, r, over in _soc_runs(s) if over and r[-1].value - b.value > threshold]


def _soc_runs(s: Stream) -> list[tuple]:
    """Every run of SoC rises at standstill: (the sample before the first
    rise, the rises, whether it is over).

    A run is broken by a sample that does not rise, a movement, a capture
    gap, or T_still without a rise — the last because L0 logs only changes,
    so a run that simply stopped rising has no sample to say so. A run is
    over once one of these has happened by the stream's last line, and only
    then a session; the same stream therefore yields the same sessions
    however late it is asked (ABL-01). A run never crosses a gap: nothing is read across
    one (ABL-04).
    """
    still = timedelta(seconds=float(s.thresholds()["t_still_s"]))
    moves = [clock.parse(m.t) for m in trips.movements(s)]
    soc = s.series.get("soc", [])

    def moved(a: str, b: str) -> bool:
        ta, tb = clock.parse(a), clock.parse(b)
        return any(ta < tm <= tb for tm in moves)

    def standing(a, b) -> bool:
        return not moved(a.t, b.t) and not series.gap_between(s, a.t, b.t)

    def joins(a, b) -> bool:
        return clock.parse(b.t) - clock.parse(a.t) < still and standing(a, b)

    runs: list[tuple] = []    # (the sample before the first rise, the rises, over)
    base, rises = None, []
    for prev, x in pairwise(soc):
        rising = x.value > prev.value
        if rises and rising and joins(rises[-1], x):
            rises.append(x)
            continue
        if rises:
            runs.append((base, rises, True))
            base, rises = None, []
        # The value before the first rise may be hours old: it held.
        if rising and standing(prev, x):
            base, rises = prev, [x]
    if rises:
        last = rises[-1].t
        over = (s.last_t is not None and clock.parse(s.last_t) - clock.parse(last) >= still) \
            or any(tm > clock.parse(last) for tm in moves) \
            or any(clock.parse(g.start) >= clock.parse(last) for g in s.gaps)
        runs.append((base, rises, over))
    return runs


def spans(s: Stream) -> list[Span]:
    """The vehicle's completed sessions, by charging state where one is
    assigned, else by SoC."""
    return by_charging_state(s) if has_charging_state(s) else by_soc(s)


def begun(s: Stream) -> list[str]:
    """The start of every session that has begun by the stream's last line,
    in order: the completed spans, and one still under way — still
    charging, or a run of SoC rises not yet over, whatever it has risen so
    far (ADR-0033, point 2). A run of SoC rises starts at its first rise,
    so here it is the sample before, which a stock read at it does not yet
    include. Which of them L1 holds is the caller's to tell: a completed
    span can still wait for another stream (ADR-0027)."""
    if has_charging_state(s):
        done, open_start = _by_charging_state(s)
        return [x.start for x in done] + ([open_start] if open_start is not None else [])
    return [b.t for b, r, over in _soc_runs(s)
            if not over or r[-1].value - b.value > float(s.thresholds()["charging_threshold_pct"])]


# --- where, and what was read at its ends --------------------------------

@dataclass(frozen=True)
class ChargePoint:
    subject: str
    config: dict
    stream: Stream


def chargepoints(base: Path, *, now: str | None = None) -> list[ChargePoint]:
    """Every charge point under ``base`` with a config line, its stream read
    and its gaps judged against ``now``, the vehicle's last line."""
    out = []
    for subject in layout.subjects(base):
        if subject.kind != "chargepoint":
            continue
        s = series.load(base, subject, now=now)
        if s.config and vconfig.is_chargepoint(s.config):
            out.append(ChargePoint(subject.id, s.config, s))
    return out


def other_vehicles(base: Path, subject: Subject, *, now: str | None = None) -> list[Stream]:
    """Every other vehicle's stream under ``base``, its gaps judged against
    ``now``, the vehicle's last line (ADR-0027, point 2)."""
    return [series.load(base, other, now=now) for other in layout.subjects(base)
            if other.kind == "vehicle" and other != subject]


def place(position: dict | None, points: list[ChargePoint]) -> ChargePoint | None:
    """The charge point whose radius holds the position; the nearest, if
    several do. ``None``: foreign, or no position to place."""
    if position is None:
        return None
    best, best_m = None, None
    for cp in points:
        m = 1000 * geo.distance_km(position["latitude"], position["longitude"],
                                   cp.config["latitude"], cp.config["longitude"])
        if m <= cp.config["radius_m"] and (best_m is None or m < best_m):
            best, best_m = cp, m
    return best


def _at_start(s: Stream, samples: list, start: str, end: str):
    """The value a session starts from: the last sample at or before its
    start — unless a gap lies between the two, or there is none, in which
    case the first inside the session. Nothing is read across a gap."""
    before = series.last_at_or_before(samples, start)
    if before is not None and not series.gap_between(s, before.t, start):
        return before
    inside = series.between(samples, start, end)
    return inside[0] if inside else None


def _position(s: Stream, span: Span) -> dict | None:
    return series.fix_dict(_at_start(s, s.fixes, span.start, span.end))


# --- the meter, and who else charged ------------------------------------------

def _meter_kwh(cp: ChargePoint, span: Span) -> float | None:
    """The charge point's meter difference over the session (LAD-06, step 2),
    or ``None``: no meter, no reading, a gap in its stream, or a meter that
    went backwards."""
    if not cp.config.get("meter"):
        return None
    meter = cp.stream.series.get("energy_meter", [])
    a = series.last_at_or_before(meter, span.start)
    b = series.last_at_or_before(meter, span.end)
    if a is None or b is None or series.gap_between(cp.stream, a.t, span.end):
        return None
    if b.value < a.value:
        return None
    return round(b.value - a.value, 3)


def occupancy(others: list[Stream], points: list[ChargePoint]) -> list[tuple[str, str, str | None]]:
    """(charge point, start, end) of every other vehicle's session at a
    configured charge point — ``end`` ``None`` while it is still charging.
    What LAD-07 asks: did anyone else charge there meanwhile?"""
    out: list[tuple[str, str, str | None]] = []
    for s in others:
        if has_charging_state(s):
            found, open_start = _by_charging_state(s)
            if open_start is not None:
                found.append(Span(open_start, None, "charging_state", False))
        else:
            found = by_soc(s)
        for span in found:
            cp = place(_position(s, Span(span.start, span.end or s.last_t, span.source, False)), points)
            if cp is not None:
                out.append((cp.subject, span.start, span.end))
    return out


def _alone(cp: ChargePoint, span: Span, others: list[tuple[str, str, str | None]]) -> bool:
    ts, te = clock.parse(span.start), clock.parse(span.end)
    for where, start, end in others:
        if where != cp.subject:
            continue
        if clock.parse(start) < te and (end is None or clock.parse(end) > ts):
            return False
    return True


# --- the session -------------------------------------------------------------

def _session(s: Stream, span: Span, cp: ChargePoint | None, position: dict | None,
             others: list[tuple[str, str, str | None]], moves: list[trips.Movement]) -> Session:
    """One session at its boundaries, placed at ``cp`` by ``position``."""
    p = s.parameters()
    soc = s.series.get("soc", [])
    if span.source == "soc":
        # A SoC session starts at its first rise, from the value before it.
        a = series.last_before(soc, span.start)
    else:
        a = _at_start(s, soc, span.start, span.end)
    b = series.last_at_or_before(soc, span.end)
    soc_start = a.value if a else None
    soc_end = b.value if b and a and clock.parse(b.t) >= clock.parse(a.t) else None
    delta = round(soc_end - soc_start, 3) if soc_start is not None and soc_end is not None else None
    capacity = p.get("battery_net_kwh")
    battery = round(delta / 100 * capacity, 3) if delta is not None and capacity else None

    grid, grid_q, grid_src, attributable = None, None, None, None
    if cp is not None and cp.config.get("meter"):
        attributable = _alone(cp, span, others)
        kwh = _meter_kwh(cp, span) if attributable else None
        if kwh is not None:
            grid, grid_q, grid_src = kwh, MEASURED, "meter"
    factor = p.get("charging_loss_factor")
    if grid is None and battery is not None and factor:
        grid, grid_q, grid_src = round(battery * factor, 3), ESTIMATED, "loss_factor"

    tariff = vconfig.tariff_at(cp.config.get("tariffs") or [], span.start) if cp else None
    price = tariff["eur_per_kwh"] if tariff else None
    cost, cost_q = None, None
    if price == 0:
        # A tariff of 0 needs no energy: the cost is exactly 0 (LAD-08).
        cost, cost_q = 0.0, grid_q or MEASURED
    elif price is not None and grid is not None:
        cost, cost_q = round(grid * price, 2), grid_q

    measured = grid_q == MEASURED
    per_pct = round(grid / delta, 4) if measured and delta else None
    loss = round(grid - battery, 3) if measured and battery is not None else None

    t0, t1 = clock.parse(span.start), clock.parse(span.end)
    moving = sum(1 for m in moves if t0 < clock.parse(m.t) < t1)

    return Session(
        kind="charging", subject=s.subject.id, start=span.start, end=span.end,
        quality=INCOMPLETE if span.crossed else MEASURED, source=span.source,
        soc_start_pct=soc_start, soc_end_pct=soc_end, delta_soc_pct=delta,
        position=position,
        chargepoint=cp.subject if cp else FOREIGN,
        chargepoint_name=cp.config.get("name") if cp else None,
        battery_kwh=battery,
        grid_kwh=grid, grid_kwh_quality=grid_q, grid_kwh_source=grid_src,
        meter_attributable=attributable,
        tariff_eur_per_kwh=price, cost_eur=cost, cost_quality=cost_q,
        kwh_per_pct=per_pct, charging_loss_kwh=loss,
        movements_while_charging=moving,
        version=__version__,
    )


def derive(s: Stream, points: list[ChargePoint], others: list[Stream]) -> list[Session]:
    """Every completed charging session in the stream, in order of start.

    A session is completed when the charging state went away, or, by SoC,
    when its run was broken by the stream's last line — and once every
    stream it reads has reached its end (ADR-0027, point 2): the charge
    point's where it has a meter, and every other vehicle's. Never by the
    clock (ABL-01). ``points`` are the configured charge points, ``others``
    the other vehicles' streams, both read against this stream's last line.
    """
    moves = trips.movements(s)
    taken = occupancy(others, points)
    out: list[Session] = []
    for span in spans(s):
        position = _position(s, span)
        cp = place(position, points)
        waits = ([cp.stream] if cp is not None and cp.config.get("meter") else []) + others
        if not all(series.reached(x, span.end) for x in waits):
            break       # a stream reaches a later end no sooner: the rest wait too
        out.append(_session(s, span, cp, position, taken, moves))
    return out


def derive_from(base: Path, subject: Subject, *, since: str | None = None,
                until: str | None = None) -> list[Session]:
    s = series.load(base, subject, since=since, until=until)
    return derive(s, chargepoints(base, now=s.last_t), other_vehicles(base, subject, now=s.last_t))


def to_dict(session: Session) -> dict:
    return asdict(session)


def _completed_dicts(base: Path, subject: Subject, since: str | None) -> list[dict]:
    if subject.kind != "vehicle":
        return []
    return [to_dict(x) for x in derive_from(base, subject, since=since)]


l1.DERIVATIONS["charging"] = _completed_dicts
