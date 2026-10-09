# SPDX-License-Identifier: BSD-3-Clause
"""A stream counted: its files and their sizes, its lines, the last of each
kind, its gaps, the sampling and change intervals per role and the values
the state mapping does not list — what ``vledger l0 stats`` prints and what
the integration's diagnostic entities and diagnostics show.

Two intervals, named apart (ADR-0010). The *sampling interval* is the time
between two updates of a role's entity: a line's ``t`` minus its
``reported_before``. The *change interval* is the time between two of its
lines — a change of value, which happens at an update but not at every
one, so it is an upper bound on the sampling interval and never stands in
for it.

One pass over the stream. Nothing here interprets a state beyond what the
state mapping already does (ADR-0008).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from vledger import clock, l0, layout
from vledger import config as vconfig
from vledger.layout import Subject


@dataclass(frozen=True)
class Intervals:
    """One measured interval of a role, summarised: how many, median and
    95th percentile. Never measured across a capture gap or a start."""

    count: int
    median_s: float
    p95_s: float


def intervals_of(seconds: list[float]) -> Intervals | None:
    """Median and 95th percentile of a list of intervals, or ``None`` for none."""
    if not seconds:
        return None
    if len(seconds) == 1:
        return Intervals(1, seconds[0], seconds[0])
    p95 = statistics.quantiles(seconds, n=20, method="inclusive")[-1]
    return Intervals(len(seconds), statistics.median(seconds), p95)


@dataclass
class Stats:
    subject: Subject
    #: month (``YYYY-MM``) -> bytes, oldest first.
    files: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    by_role: dict[str, int] = field(default_factory=dict)
    #: State lines since the last start marker.
    lines_since_start: int = 0
    #: State lines at or after ``since``, when a ``since`` was given.
    lines_since: int = 0
    last_line_at: str | None = None
    last_heartbeat_at: str | None = None
    #: The last state line per role: t, entity, state, unit and attrs as written.
    last_states: dict[str, dict] = field(default_factory=dict)
    #: Per role, the time between two updates (``reported_before``).
    sampling: dict[str, Intervals] = field(default_factory=dict)
    #: Per role, the time between two of its lines.
    changes: dict[str, Intervals] = field(default_factory=dict)
    #: Per enumerated role, the values met that its map does not list.
    unlisted: dict[str, set[str]] = field(default_factory=dict)
    finder: l0.GapFinder = field(default_factory=l0.GapFinder)

    @property
    def bytes(self) -> int:
        return sum(self.files.values())

    @property
    def current_month(self) -> str | None:
        return max(self.files) if self.files else None

    @property
    def last_state(self) -> tuple[str, dict] | None:
        """The role and line of the latest state line of any role."""
        if not self.last_states:
            return None
        return max(self.last_states.items(), key=lambda kv: clock.parse(kv[1]["t"]))

    @property
    def gaps(self) -> list[l0.Gap]:
        return self.finder.found


def scan(base: Path, subject: Subject, *, since: str | None = None,
         tolerance_s: float = 300) -> Stats:
    """Count a stream in one pass. An ``open`` gap at the end is not judged
    here — that needs a ``now``; :meth:`l0.GapFinder.close` on ``finder``
    adds it."""
    s = Stats(subject, finder=l0.GapFinder(tolerance_s=tolerance_s))
    for path in layout.l0_files(base, subject):
        s.files[path.stem] = path.stat().st_size
    lo = clock.parse(since) if since else None
    config: dict = {}
    previous: dict[str, str] = {}   # role -> t of its last state line in this run
    run_began = None                # when this run of capture began, as a datetime
    changes: dict[str, list[float]] = {}
    sampling: dict[str, list[float]] = {}
    for r in l0.read(base, subject):
        line = r.line
        t, kind = line["t"], line["kind"]
        if s.finder.feed(line) or run_began is None:
            previous.clear()
            run_began = clock.parse(t)
        s.by_kind[kind] = s.by_kind.get(kind, 0) + 1
        s.last_line_at = t
        if kind == "start":
            s.lines_since_start = 0
            previous.clear()
            run_began = clock.parse(t)
        elif kind == "config":
            config = line["config"]
        elif kind == "heartbeat":
            s.last_heartbeat_at = t
        elif kind == "state":
            role = line["role"]
            s.by_role[role] = s.by_role.get(role, 0) + 1
            s.lines_since_start += 1
            if lo and clock.parse(t) >= lo:
                s.lines_since += 1
            if role in previous:
                changes.setdefault(role, []).append(
                    (clock.parse(t) - clock.parse(previous[role])).total_seconds())
            previous[role] = t
            if "reported_before" in line:
                before = clock.parse(line["reported_before"])
                # A report from before this run was not seen by capture.
                if before >= run_began:
                    sampling.setdefault(role, []).append(
                        (clock.parse(t) - before).total_seconds())
            s.last_states[role] = {k: line[k] for k in ("t", "entity", "state", "unit", "attrs")
                                   if k in line}
            if role in vconfig.DOMAIN_STATES:
                mapping = ((config.get("roles") or {}).get(role) or {}).get("map") or {}
                if vconfig.unlisted(role, line["state"], mapping):
                    s.unlisted.setdefault(role, set()).add(line["state"])
    for role, seconds in sampling.items():
        s.sampling[role] = intervals_of(seconds)
    for role, seconds in changes.items():
        s.changes[role] = intervals_of(seconds)
    return s


def to_dict(s: Stats) -> dict:
    """The one JSON spelling of a :class:`Stats`, for the CLI and diagnostics."""
    last = s.last_state
    return {
        "files": dict(s.files),
        "bytes": s.bytes,
        "current_month": s.current_month,
        "lines_by_kind": dict(s.by_kind),
        "state_lines_by_role": dict(s.by_role),
        "lines_since_start": s.lines_since_start,
        "lines_since": s.lines_since,
        "last_line_at": s.last_line_at,
        "last_heartbeat_at": s.last_heartbeat_at,
        "last_state": {"role": last[0], "t": last[1]["t"]} if last else None,
        "last_states": {role: dict(v) for role, v in s.last_states.items()},
        "sampling_intervals": {role: i.__dict__ for role, i in s.sampling.items()},
        "change_intervals": {role: i.__dict__ for role, i in s.changes.items()},
        "unlisted": {role: sorted(v) for role, v in s.unlisted.items()},
        "gaps": [g.__dict__ for g in s.gaps],
    }
