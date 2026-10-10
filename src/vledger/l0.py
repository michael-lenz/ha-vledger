# SPDX-License-Identifier: BSD-3-Clause
"""L0, the raw log: writing it, reading it back, checking it, and the
capture gaps its markers reveal (ADR-0004).

A line is a plain ``dict`` — the JSON object itself — and this module is
the only place that knows which keys it has. Nothing here interprets a
state: values stay the strings Home Assistant reported (ERF-11).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from vledger import clock, layout
from vledger.layout import Subject

#: The schema version this module writes, and the highest it reads. Version
#: 2 adds ``reported_before`` to the state line (ADR-0011), version 3 the
#: roles of :data:`ROLES_SINCE_3` (ADR-0021, ADR-0022), version 4 the role
#: of :data:`ROLES_SINCE_4` (ADR-0025).
VERSION = 4

KINDS = ("state", "start", "stop", "heartbeat", "config")
STOP_REASONS = ("shutdown", "unload", "reload")

#: The roles a vehicle's entities may be assigned to, and a charge point's.
#: The sensor-pair variant of position logs under two roles of its own.
VEHICLE_ROLES = (
    "odometer", "position", "position_latitude", "position_longitude",
    "trip_distance", "fuel_level", "soc", "charging_state", "plug_state",
    "ignition", "outside_temperature", "fuel_price",
    "engine", "lock", "in_use", "fuel_flap", "trip_consumption",
)
#: The roles schema version 3 added; an earlier line cannot carry them.
ROLES_SINCE_3 = ("engine", "lock", "in_use", "fuel_flap")
#: The role schema version 4 added (ADR-0025), likewise.
ROLES_SINCE_4 = ("trip_consumption",)
#: Per role added later than version 1, the version that added it.
ROLE_SINCE = {**dict.fromkeys(ROLES_SINCE_3, 3), **dict.fromkeys(ROLES_SINCE_4, 4)}
CHARGEPOINT_ROLES = ("energy_meter", "power")
ROLES = VEHICLE_ROLES + CHARGEPOINT_ROLES

#: The role-relevant attributes, per role: what a state line carries in
#: ``attrs`` and what counts as a change worth a line (ERF-01). Fixed for
#: schema versions 1 and 2 — adding to it is a version bump (ADR-0004, consequence 1).
RELEVANT_ATTRS: dict[str, tuple[str, ...]] = {
    "position": ("latitude", "longitude", "gps_accuracy", "source_type"),
}

#: The heartbeat interval a stream is read with when no config line says
#: otherwise (ERF-04); seconds.
DEFAULT_HEARTBEAT_S = 3600

Line = dict


def relevant_attrs(role: str, attributes: dict) -> dict:
    """The subset of an entity's attributes a state line of ``role`` carries."""
    keep = RELEVANT_ATTRS.get(role, ())
    return {k: attributes[k] for k in keep if k in attributes}


# --- building lines --------------------------------------------------------

def _envelope(t: str, kind: str, subject: Subject) -> Line:
    clock.parse(t)  # refuse a timestamp L0 could not spell
    return {"v": VERSION, "t": t, "kind": kind, "subject": subject.id}


def state(t: str, subject: Subject, role: str, entity: str, value: str, *,
          unit: str | None = None, attrs: dict | None = None,
          measured_at: str | None = None, reported_before: str | None = None) -> Line:
    """One change of state or of a role-relevant attribute (ERF-01, ERF-02).

    ``reported_before`` is when Home Assistant last heard the value this line
    replaces (ADR-0011); ``t`` minus it is one sampling interval of the role.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    if not isinstance(value, str):
        raise TypeError("a state is the string Home Assistant holds, not a number")
    line = _envelope(t, "state", subject)
    line.update(role=role, entity=entity, state=value)
    if unit is not None:
        line["unit"] = unit
    kept = relevant_attrs(role, attrs or {})
    if kept:
        line["attrs"] = kept
    if measured_at is not None:
        clock.parse(measured_at)
        line["measured_at"] = measured_at
    if reported_before is not None:
        if clock.parse(reported_before) > clock.parse(t):
            raise ValueError("reported_before is later than the line it belongs to")
        line["reported_before"] = reported_before
    return line


def start(t: str, subject: Subject, *, vledger: str, homeassistant: str,
          snapshot: list[dict]) -> Line:
    """Capture begins: a snapshot of every assigned role as it stands (ERF-04)."""
    for entry in snapshot:
        missing = {"role", "entity", "state", "since"} - set(entry)
        if missing:
            raise ValueError(f"snapshot entry lacks {sorted(missing)}: {entry}")
        clock.parse(entry["since"])
    line = _envelope(t, "start", subject)
    line.update(vledger=vledger, homeassistant=homeassistant, snapshot=list(snapshot))
    return line


def stop(t: str, subject: Subject, *, reason: str) -> Line:
    """Orderly end of capture (ERF-04)."""
    if reason not in STOP_REASONS:
        raise ValueError(f"stop reason must be one of {STOP_REASONS}, not {reason!r}")
    line = _envelope(t, "stop", subject)
    line["reason"] = reason
    return line


def heartbeat(t: str, subject: Subject, *, lines: int) -> Line:
    """Proof of life at a fixed interval; ``lines`` counts state lines since start."""
    line = _envelope(t, "heartbeat", subject)
    line["lines"] = int(lines)
    return line


def config(t: str, subject: Subject, *, config: dict) -> Line:
    """The subject's complete configuration, at start and on every change (ERF-05)."""
    if not isinstance(config, dict):
        raise TypeError("a config line carries the configuration as an object")
    line = _envelope(t, "config", subject)
    line["config"] = config
    return line


# --- writing ---------------------------------------------------------------

def encode(line: Line) -> str:
    """One line of JSON, compact, UTF-8 as is, no trailing newline."""
    return json.dumps(line, ensure_ascii=False, separators=(",", ":"))


def append(base: Path, subject: Subject, line: Line) -> Path:
    """Append one line to the month file its own time names (ADR-0004, point 3).

    Written whole and flushed to disk before returning: a line is on disk or
    it is not, and the reader's torn-line rule (ERF-07) covers the crash in
    between. Blocking — the integration calls this off the event loop.
    """
    if line.get("subject") != subject.id:
        raise ValueError("line and stream disagree on the subject")
    path = layout.l0_file(base, subject, clock.month_of(line["t"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(encode(line) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


# --- reading ---------------------------------------------------------------

@dataclass(frozen=True)
class Read:
    """One line read back, with where it came from."""

    line: Line
    path: Path
    number: int  # 1-based line number in the file


class TornLine(Exception):
    """A line that is not complete JSON and is not the file's last line."""


def _raw_lines(path: Path) -> Iterator[tuple[int, str, bool]]:
    """(number, text, is_last) for every line, including a torn last one."""
    data = path.read_bytes()
    if not data:
        return
    text = data.decode("utf-8", errors="replace")
    parts = text.split("\n")
    # A file the writer closed ends in "\n", so the split leaves an empty
    # tail; a torn file does not.
    torn_tail = parts[-1] != ""
    if not torn_tail:
        parts.pop()
    for i, part in enumerate(parts, start=1):
        yield i, part, i == len(parts) and torn_tail


def read_file(path: Path) -> Iterator[Read]:
    """Every line of one month file, in order; a torn last line is skipped (ERF-07)."""
    for number, text, torn in _raw_lines(path):
        try:
            line = json.loads(text)
        except json.JSONDecodeError:
            if torn:
                return
            raise TornLine(f"{path}:{number}: not JSON and not the last line") from None
        if torn:
            # Complete JSON without its newline: the crash came after the
            # text and before the newline. The line is whole; keep it.
            pass
        if not isinstance(line, dict):
            raise TornLine(f"{path}:{number}: not a JSON object")
        if line.get("v", 0) > VERSION:
            raise ValueError(
                f"{path}:{number}: schema version {line.get('v')} is newer than "
                f"this reader ({VERSION})"
            )
        yield Read(line, path, number)


def read(base: Path, subject: Subject, *, kind: str | None = None,
         role: str | None = None, since: str | None = None,
         until: str | None = None) -> Iterator[Read]:
    """A stream in order, across its month files, optionally narrowed.

    Lines of a kind this version does not know are skipped here (ADR-0004,
    point 1); :func:`validate` reports them.
    """
    lo = clock.parse(since) if since else None
    hi = clock.parse(until) if until else None
    for path in layout.l0_files(base, subject):
        for r in read_file(path):
            if r.line.get("kind") not in KINDS:
                continue
            if kind and r.line["kind"] != kind:
                continue
            if role and r.line.get("role") != role:
                continue
            if lo or hi:
                t = clock.parse(r.line["t"])
                if lo and t < lo:
                    continue
                if hi and t > hi:
                    continue
            yield r


# --- validating ------------------------------------------------------------

@dataclass(frozen=True)
class Problem:
    severity: str  # "error" or "warning"
    where: str     # "<file>:<line>" or the file
    what: str


@dataclass
class Report:
    files: int = 0
    lines: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    versions: set[int] = field(default_factory=set)
    problems: list[Problem] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for p in self.problems if p.severity == "error")

    def problem(self, severity: str, where: str, what: str) -> None:
        self.problems.append(Problem(severity, where, what))


_REQUIRED: dict[str, tuple[str, ...]] = {
    "state": ("role", "entity", "state"),
    "start": ("vledger", "homeassistant", "snapshot"),
    "stop": ("reason",),
    "heartbeat": ("lines",),
    "config": ("config",),
}


def _check(line: Line, where: str, subject: Subject, month: str, report: Report) -> None:
    for key in ("v", "t", "kind", "subject"):
        if key not in line:
            report.problem("error", where, f"missing envelope key {key!r}")
            return
    try:
        clock.parse(line["t"])
    except ValueError:
        report.problem("error", where, f"unreadable t {line['t']!r}")
        return
    if clock.month_of(line["t"]) != month:
        report.problem("error", where, f"t {line['t']} is not in month file {month}")
    if line["subject"] != subject.id:
        report.problem("error", where, f"subject {line['subject']!r} is not {subject.id!r}")
    kind = line["kind"]
    if kind not in KINDS:
        report.problem("warning", where, f"unknown kind {kind!r}, skipped by readers")
        return
    for key in _REQUIRED[kind]:
        if key not in line:
            report.problem("error", where, f"{kind} line lacks {key!r}")
    if kind == "state":
        if line.get("role") not in ROLES:
            report.problem("error", where, f"unknown role {line.get('role')!r}")
        if not isinstance(line.get("state"), str):
            report.problem("error", where, "state is not a string")
        extra = set(line.get("attrs") or {}) - set(RELEVANT_ATTRS.get(line.get("role", ""), ()))
        if extra:
            report.problem("error", where, f"attrs not relevant to the role: {sorted(extra)}")
        if "reported_before" in line:
            _check_reported_before(line, where, report)
        since = ROLE_SINCE.get(line.get("role"))
        if since and isinstance(line["v"], int) and line["v"] < since:
            report.problem("error", where, f"role {line['role']!r} in a version {line['v']} line")
    elif kind == "stop" and line.get("reason") not in STOP_REASONS:
        report.problem("error", where, f"unknown stop reason {line.get('reason')!r}")
    elif kind == "config" and not isinstance(line.get("config"), dict):
        report.problem("error", where, "config is not an object")


def _check_reported_before(line: Line, where: str, report: Report) -> None:
    if isinstance(line["v"], int) and line["v"] < 2:
        report.problem("error", where, "reported_before in a version 1 line")
    try:
        before = clock.parse(line["reported_before"])
    except (TypeError, ValueError):
        report.problem("error", where, f"unreadable reported_before {line['reported_before']!r}")
        return
    if before > clock.parse(line["t"]):
        report.problem("error", where, "reported_before is later than t")


def validate(base: Path, subject: Subject) -> Report:
    """Check a stream against the schema and report (CLI-02).

    Errors are lines the writer could not have written; warnings are lines
    a reader will skip or that look odd (time running backwards).
    """
    report = Report()
    last_t = None
    for path in layout.l0_files(base, subject):
        report.files += 1
        month = path.stem
        for number, text, torn in _raw_lines(path):
            where = f"{path.name}:{number}"
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                if torn:
                    report.problem("warning", where, "torn last line, skipped by readers")
                else:
                    report.problem("error", where, "not JSON")
                continue
            if not isinstance(line, dict):
                report.problem("error", where, "not a JSON object")
                continue
            report.lines += 1
            v = line.get("v")
            if isinstance(v, int):
                report.versions.add(v)
                if v > VERSION:
                    report.problem("error", where, f"schema version {v} is newer than this reader")
            _check(line, where, subject, month, report)
            kind = line.get("kind")
            if kind in KINDS:
                report.by_kind[kind] = report.by_kind.get(kind, 0) + 1
            try:
                t = clock.parse(line["t"])
            except (KeyError, ValueError):
                continue
            if last_t and t < last_t:
                report.problem("warning", where, "t runs backwards")
            last_t = t
    return report


# --- gaps ------------------------------------------------------------------

@dataclass(frozen=True)
class Gap:
    """A span in which nothing was captured (ERF-04)."""

    start: str
    end: str
    seconds: float
    reason: str  # "crash", "stopped", "silence" or "open"


class GapFinder:
    """The gap rules of :func:`gaps`, fed one line at a time, so a writer
    that already knows every line it wrote can keep finding gaps without
    reading its stream back."""

    def __init__(self, *, tolerance_s: float = 300, min_s: float = 0) -> None:
        self.tolerance_s = tolerance_s
        self.min_s = min_s
        self.found: list[Gap] = []
        self.last: str | None = None
        self.running = False
        self.interval: float = DEFAULT_HEARTBEAT_S

    def _add(self, a: str, b: str, reason: str) -> Gap | None:
        seconds = (clock.parse(b) - clock.parse(a)).total_seconds()
        if seconds < self.min_s:
            return None
        gap = Gap(a, b, seconds, reason)
        self.found.append(gap)
        return gap

    def feed(self, line: Line) -> Gap | None:
        """Take the next line of the stream; the gap it closes, if any."""
        t, kind = line["t"], line["kind"]
        gap = None
        if kind == "start":
            if self.last is not None:
                gap = self._add(self.last, t, "crash" if self.running else "stopped")
            self.running = True
        elif self.last is not None and self.running:
            if (clock.parse(t) - clock.parse(self.last)).total_seconds() > self.interval + self.tolerance_s:
                gap = self._add(self.last, t, "silence")
        if kind == "config":
            self.interval = (line.get("config", {}).get("thresholds") or {}).get(
                "heartbeat_s", self.interval)
        if kind == "stop":
            self.running = False
        self.last = t
        return gap

    def close(self, now: str) -> Gap | None:
        """The stream ends here: an ``open`` gap if it is still running and
        ``now`` is further from its last line than a heartbeat allows."""
        if not self.running or self.last is None:
            return None
        if (clock.parse(now) - clock.parse(self.last)).total_seconds() <= self.interval + self.tolerance_s:
            return None
        return self._add(self.last, now, "open")


def gaps(base: Path, subject: Subject, *, tolerance_s: float = 300,
         now: str | None = None, min_s: float = 0) -> list[Gap]:
    """The capture gaps a stream's markers reveal (ADR-0004).

    ``crash``: a start without a stop before it — nothing from the last line
    to the start. ``stopped``: an orderly stop and the next start — capture
    was off. ``silence``: longer than the heartbeat interval plus tolerance
    between two lines while running. ``open``: the stream ends without a
    stop and ``now`` is further away than that — capture may have died.
    The heartbeat interval comes from the latest config line
    (``thresholds.heartbeat_s``), else :data:`DEFAULT_HEARTBEAT_S`.
    """
    finder = GapFinder(tolerance_s=tolerance_s, min_s=min_s)
    for r in read(base, subject):
        finder.feed(r.line)
    finder.close(now or clock.to_text(clock.now()))
    return finder.found
