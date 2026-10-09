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

from vledger import clock, geo, l0, units
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


def load(base: Path, subject: Subject, *, since: str | None = None,
         until: str | None = None) -> Stream:
    """Read a stream into series. The latest config line in range wins; the
    snapshot of a start line seeds every series with the value before the
    first change, at the snapshot's ``since`` time."""
    s = Stream(subject)
    pending_lat: dict[str, float] = {}

    def take(role: str, line: dict, t: str) -> None:
        q = units.quantity_of(role)
        if q is not None:
            n = units.number(line.get("state"))
            if n is None:
                return
            try:
                v = units.convert(n, line.get("unit"), q)
            except ValueError:
                return
            s.series.setdefault(role, []).append(Sample(t, v))
        elif role == "position":
            fix = _position_from(dict(line, t=t))
            if fix:
                s.fixes.append(fix)
        elif role in ("position_latitude", "position_longitude"):
            n = units.number(line.get("state"))
            if n is None:
                return
            key = "lat" if role == "position_latitude" else "lon"
            pending_lat[key] = n
            if "lat" in pending_lat and "lon" in pending_lat:
                s.fixes.append(Fix(t, pending_lat["lat"], pending_lat["lon"], None, None))
        elif role in vconfig.DOMAIN_STATES:
            mapping = ((s.config or {}).get("roles", {}).get(role) or {}).get("map") or {}
            raw = line.get("state")
            state = vconfig.domain_state(role, raw, mapping)
            if state == vconfig.NEGATIVE_STATES[role]:
                # Met and not listed: negative by ADR-0008, reported so the
                # map can be completed if that was wrong.
                s.unmapped.setdefault(role, set()).add(raw)
            s.domain.setdefault(role, []).append(DomainSample(t, state, raw))

    for r in l0.read(base, subject, since=since, until=until):
        line = r.line
        t = line["t"]
        s.first_t = s.first_t or t
        s.last_t = t
        kind = line["kind"]
        if kind == "config":
            s.config = line["config"]
        elif kind == "start":
            for entry in line.get("snapshot") or []:
                take(entry["role"], entry, entry.get("since") or t)
        elif kind == "state":
            take(line["role"], line, t)
    # Snapshot seeds may predate lines read before them: keep every series
    # in time order, stable so equal times keep their file order.
    for role in s.series:
        s.series[role].sort(key=lambda x: clock.parse(x.t))
    s.fixes.sort(key=lambda x: clock.parse(x.t))
    for role in s.domain:
        s.domain[role].sort(key=lambda x: clock.parse(x.t))
    s.gaps = l0.gaps(base, subject, now=s.last_t) if s.last_t else []
    return s


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


def first_at_or_after(samples: list, t: str):
    limit = clock.parse(t)
    for x in samples:
        if clock.parse(x.t) >= limit:
            return x
    return None


def between(samples: list, start: str, end: str) -> list:
    lo, hi = clock.parse(start), clock.parse(end)
    return [x for x in samples if lo <= clock.parse(x.t) <= hi]
