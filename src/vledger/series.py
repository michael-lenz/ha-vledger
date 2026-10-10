# SPDX-License-Identifier: BSD-3-Clause
"""An L0 stream as the series a derivation reads: one numeric series per
measuring role in L1 units, the position fixes, the enumerated roles as
domain states, and the gaps.

Everything a derivation needs from a stream comes through here, so no
derivation parses a line itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vledger import clock, geo, l0, layout, units
from vledger import config as vconfig
from vledger.layout import Subject


@dataclass(frozen=True)
class Sample:
    t: str
    value: float


@dataclass(frozen=True)
class Fix:
    t: str
    latitude: float
    longitude: float
    accuracy_m: float | None  # None: unknown
    zone: str | None          # the tracker's state: home, a zone, not_home


@dataclass(frozen=True)
class DomainSample:
    t: str
    state: str | None  # None: hold (unavailable, unknown)
    raw: str


@dataclass
class Stream:
    """A subject's L0, read once, as series."""

    subject: Subject
    config: dict | None = None
    series: dict[str, list[Sample]] = field(default_factory=dict)
    fixes: list[Fix] = field(default_factory=list)
    domain: dict[str, list[DomainSample]] = field(default_factory=dict)
    unmapped: dict[str, set[str]] = field(default_factory=dict)
    #: Per measuring role, the times it reported something that is not a
    #: number (unavailable, unknown): a sensor dropping out, not a value.
    dropouts: dict[str, list[str]] = field(default_factory=dict)
    #: Per measuring role whose lines could not become a series, why.
    unconverted: dict[str, str] = field(default_factory=dict)
    gaps: list[l0.Gap] = field(default_factory=list)
    first_t: str | None = None
    last_t: str | None = None

    def thresholds(self) -> dict:
        thr = dict(vconfig.DEFAULT_THRESHOLDS)
        if self.config:
            thr.update(self.config.get("thresholds") or {})
        return thr

    def parameters(self) -> dict:
        p = dict(vconfig.DEFAULT_PARAMETERS)
        if self.config:
            p.update(self.config.get("parameters") or {})
        return p


def _position_from(line: dict) -> Fix | None:
    attrs = line.get("attrs") or {}
    lat, lon = attrs.get("latitude"), attrs.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    state = line.get("state")
    zone = state if state not in (None, "unavailable", "unknown") else None
    return Fix(line["t"], lat, lon, geo.accuracy_m(attrs.get("gps_accuracy")), zone)


def _seed_lines(base: Path, subject: Subject, since: str) -> list[dict]:
    """The last value of every role — from a state line or a start line's
    snapshot, whichever came later — and the last config line, before
    ``since``; read backwards from the month file ``since`` falls in, so a
    derivation from a cursor starts from the value the vehicle had, not
    from its first change (ADR-0009, 3; ISSUE-0011). Stops at the first
    month with nothing new to find."""
    limit = clock.parse(since)
    found: dict[str, dict] = {}
    config = None
    files = [p for p in layout.l0_files(base, subject) if p.stem <= clock.month_of(since)]
    for path in reversed(files):
        lines = [r.line for r in l0.read_file(path) if clock.parse(r.line["t"]) < limit]
        new = False
        for line in reversed(lines):
            kind = line.get("kind")
            if kind == "config" and config is None:
                config, new = line, True
            elif kind == "state" and line.get("role") not in found:
                found[line["role"]], new = line, True
            elif kind == "start":
                for entry in line.get("snapshot") or []:
                    if entry["role"] not in found:
                        found[entry["role"]] = dict(entry, kind="state", t=entry.get("since") or line["t"])
                        new = True
        if not new and config is not None:
            break
    out = list(found.values())
    if config is not None:
        out.append(config)
    out.sort(key=lambda x: clock.parse(x["t"]))
    return out


def load(base: Path, subject: Subject, *, since: str | None = None,
         until: str | None = None) -> Stream:
    """Read a stream into series. The latest config line in range wins; the
    snapshot of a start line seeds every series with the value before the
    first change, at the snapshot's ``since`` time; a ``since`` is seeded
    with the last value of every role before it."""
    s = Stream(subject)
    pending_lat: dict[str, float] = {}
    fuel_pct: list[Sample] = []
    seeding = False

    def add(items: list, item) -> None:
        # A restart's snapshot repeats the last value at the time it was
        # set; read twice, it would land again inside whatever event
        # already holds it (ISSUE-0014).
        if not (seeding and item in items):
            items.append(item)

    def take(role: str, line: dict, t: str) -> None:
        q = units.quantity_of(role)
        if q is not None:
            n = units.number(line.get("state"))
            if n is None:
                add(s.dropouts.setdefault(role, []), t)
                return
            if role == "fuel_level" and line.get("unit") == "%":
                add(fuel_pct, Sample(t, n))
                return
            try:
                v = units.convert(n, line.get("unit"), q)
            except ValueError:
                return
            add(s.series.setdefault(role, []), Sample(t, v))
        elif role == "position":
            fix = _position_from(dict(line, t=t))
            if fix:
                add(s.fixes, fix)
        elif role in ("position_latitude", "position_longitude"):
            n = units.number(line.get("state"))
            if n is None:
                return
            key = "lat" if role == "position_latitude" else "lon"
            pending_lat[key] = n
            if "lat" in pending_lat and "lon" in pending_lat:
                add(s.fixes, Fix(t, pending_lat["lat"], pending_lat["lon"], None, None))
        elif role in vconfig.DOMAIN_STATES:
            mapping = ((s.config or {}).get("roles", {}).get(role) or {}).get("map") or {}
            raw = line.get("state")
            state = vconfig.domain_state(role, raw, mapping)
            if vconfig.unlisted(role, raw, mapping):
                s.unmapped.setdefault(role, set()).add(raw)
            add(s.domain.setdefault(role, []), DomainSample(t, state, raw))

    seeds = _seed_lines(base, subject, since) if since else []
    for line in seeds + [r.line for r in l0.read(base, subject, since=since, until=until)]:
        t = line["t"]
        if line not in seeds:
            s.first_t = s.first_t or t
            s.last_t = t
        kind = line["kind"]
        if kind == "config":
            s.config = line["config"]
        elif kind == "start":
            seeding = True
            for entry in line.get("snapshot") or []:
                take(entry["role"], entry, entry.get("since") or t)
            seeding = False
        elif kind == "state":
            take(line["role"], line, t)
    if fuel_pct:
        _fuel_from_percent(s, fuel_pct)
    # Snapshot seeds may predate lines read before them: keep every series
    # in time order, stable so equal times keep their file order.
    for role in s.series:
        s.series[role].sort(key=lambda x: clock.parse(x.t))
    s.fixes.sort(key=lambda x: clock.parse(x.t))
    for role in s.domain:
        s.domain[role].sort(key=lambda x: clock.parse(x.t))
    for role in s.dropouts:
        s.dropouts[role].sort(key=clock.parse)
    s.gaps = l0.gaps(base, subject, now=s.last_t) if s.last_t else []
    return s


def _fuel_from_percent(s: Stream, samples: list[Sample]) -> None:
    """A fuel level reported in % is litres of the tank capacity (FZG-08,
    TNK-01) — the latest configuration's, since the capacity is the
    vehicle's and not the moment's. Without it there is no fuel series at
    all, and ``unconverted`` says why: a guessed capacity would be
    invention (ISSUE-0009)."""
    capacity = s.parameters().get("tank_capacity_l")
    if not capacity:
        s.unconverted["fuel_level"] = "fuel_level reports % and tank_capacity_l is not set"
        return
    s.series.setdefault("fuel_level", []).extend(
        Sample(x.t, x.value / 100 * float(capacity)) for x in samples)


def gap_between(s: Stream, a: str, b: str) -> bool:
    """Whether a capture gap lies between ``a`` and ``b``."""
    ta, tb = clock.parse(a), clock.parse(b)
    return any(clock.parse(g.start) < tb and clock.parse(g.end) > ta for g in s.gaps)


def fix_dict(f: Fix | None) -> dict | None:
    """A fix as an event holds it; an unknown accuracy is left out."""
    if f is None:
        return None
    d = {"t": f.t, "latitude": f.latitude, "longitude": f.longitude}
    if f.accuracy_m is not None:
        d["accuracy_m"] = f.accuracy_m
    return d


def in_effect(s: Stream, role: str, t: str) -> Sample | None:
    """The value a measuring role held at ``t``: its latest sample not
    after ``t`` — or ``None`` when its latest report by then was a dropout,
    or a capture gap lies between that sample and ``t``. A dropout says
    nothing, and nothing is read across a gap (ABL-04)."""
    x = last_at_or_before(s.series.get(role, []), t)
    if x is None:
        return None
    lo, hi = clock.parse(x.t), clock.parse(t)
    if any(lo < clock.parse(d) <= hi for d in s.dropouts.get(role, [])):
        return None
    return None if gap_between(s, x.t, t) else x


def last_at_or_before(samples: list, t: str):
    """The latest sample not after ``t``, or ``None``."""
    limit = clock.parse(t)
    best = None
    for x in samples:
        if clock.parse(x.t) <= limit:
            best = x
        else:
            break
    return best


def last_before(samples: list, t: str):
    """The latest sample strictly before ``t``, or ``None``."""
    limit = clock.parse(t)
    best = None
    for x in samples:
        if clock.parse(x.t) < limit:
            best = x
        else:
            break
    return best


def first_at_or_after(samples: list, t: str):
    limit = clock.parse(t)
    for x in samples:
        if clock.parse(x.t) >= limit:
            return x
    return None


def between(samples: list, start: str, end: str) -> list:
    lo, hi = clock.parse(start), clock.parse(end)
    return [x for x in samples if lo <= clock.parse(x.t) <= hi]


def mean(samples: list[Sample]) -> float | None:
    """The plain mean of the samples' values to a tenth — what a trip and a
    consumption interval report as their outside temperature — or ``None``
    for none."""
    return round(sum(x.value for x in samples) / len(samples), 1) if samples else None
