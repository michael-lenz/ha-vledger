# SPDX-License-Identifier: BSD-3-Clause
"""Capture: ``state_changed`` events become L0 lines (ADR-0008, point 5).
An update that repeats a value writes nothing; the next line carries when
it was last heard, ``reported_before`` (ADR-0011).

One :class:`Capture` per subject. Every line goes through one writer: a
queue the event loop puts lines on, drained by one task that appends them
in the executor, one at a time, in order. On stop the queue is drained
before the stop line goes, so a stop is always the last line.

The writer also keeps the numbers the diagnostic entities show (TASK-0008):
before its first line it counts the stream once with the library
(``vledger l0 stats``), then carries every count forward from the lines it
writes — only the size of the file it just appended to is read from disk.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from vledger import __version__, clock, l0, stats
from vledger.layout import Subject

from .const import DOMAIN, ISSUE_ENTITY_REMOVED, STATUS_RUNNING, STATUS_STOPPED

_LOGGER = logging.getLogger(__name__)


def roles_of(config: dict) -> dict[str, dict]:
    """role -> {"entity": ..., ...} for a vehicle or a charge point."""
    if "tariffs" in config:
        meter = config.get("meter")
        return {"energy_meter": meter} if meter else {}
    return config["roles"]


def _measured_at(spec: dict, state: State) -> str | None:
    attr = spec.get("measured_at")
    if not attr or attr not in state.attributes:
        return None
    value = state.attributes[attr]
    try:
        if isinstance(value, datetime):
            return clock.to_text(value)
        return clock.to_text(clock.parse(str(value)))
    except ValueError:
        return None


def _reported_before(old: State | None, t: str) -> str | None:
    """When the value a new line at ``t`` replaces was last reported
    (ADR-0011): the old state's ``last_reported``. None without an old
    state, and for a clock that ran backwards: the line matters more than
    its measurement."""
    if old is None:
        return None
    before = clock.to_text(old.last_reported)
    return before if clock.parse(before) <= clock.parse(t) else None


def _append(base: Path, subject: Subject, line: l0.Line) -> tuple[str, int]:
    """Append a line; the month it went to and that file's size now. Blocking."""
    path = l0.append(base, subject, line)
    return path.stem, path.stat().st_size


def _local_date(t: str):
    return dt_util.as_local(clock.parse(t)).date()


def _changed(role: str, old: State | None, new: State) -> bool:
    """A line is due when the state string, the unit or a role-relevant
    attribute changed — not for an update of anything else (ERF-01)."""
    if old is None:
        return True
    if old.state != new.state:
        return True
    unit = "unit_of_measurement"
    if old.attributes.get(unit) != new.attributes.get(unit):
        return True
    return l0.relevant_attrs(role, old.attributes) != l0.relevant_attrs(role, new.attributes)


class Capture:
    def __init__(self, hass: HomeAssistant, subject: Subject, base: Path, config: dict) -> None:
        self.hass = hass
        self.subject = subject
        self.base = base
        self.config = config
        self.roles = roles_of(config)
        self.entities = {spec["entity"]: role for role, spec in self.roles.items()}
        self.specs = {role: spec for role, spec in self.roles.items()}
        self.heartbeat_s = int((config.get("thresholds") or {}).get("heartbeat_s", l0.DEFAULT_HEARTBEAT_S))

        self.status = STATUS_STOPPED
        self.stop_reason: str | None = None

        # What the stream holds, counted once on start and carried forward
        # by the writer: the diagnostic entities read these.
        self.month_bytes: dict[str, int] = {}
        self.lines_since_start = 0
        self.lines_today = 0
        self._today = dt_util.now().date()
        self.last_line_at: str | None = None
        self.last_heartbeat_at: str | None = None
        self.last_state_role: str | None = None
        self.last_state_at: str | None = None
        self.gap_finder = l0.GapFinder()

        self._queue: asyncio.Queue[l0.Line] = asyncio.Queue()
        self._writer: asyncio.Task | None = None
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []

    # --- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        self._writer = self.hass.loop.create_task(self._write_forever())
        t = clock.to_text(clock.now())
        self._put(l0.start(t, self.subject, vledger=__version__, homeassistant=HA_VERSION,
                           snapshot=self._snapshot(t)))
        self._put(l0.config(t, self.subject, config=self.config))
        if self.entities:
            self._unsubscribe.append(
                async_track_state_change_event(self.hass, list(self.entities), self._on_state))
        self._unsubscribe.append(
            async_track_time_interval(self.hass, self._on_heartbeat, timedelta(seconds=self.heartbeat_s)))
        self._unsubscribe.append(
            async_track_time_change(self.hass, self._on_midnight, hour=0, minute=0, second=0))
        self.status = STATUS_RUNNING
        self._notify()

    async def async_stop(self, reason: str) -> None:
        if self.status != STATUS_RUNNING:
            return
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()
        self._put(l0.stop(clock.to_text(clock.now()), self.subject, reason=reason))
        await self._queue.join()
        if self._writer:
            self._writer.cancel()
            self._writer = None
        self.status = STATUS_STOPPED
        self._notify()

    # --- lines -------------------------------------------------------------

    def _snapshot(self, t: str) -> list[dict]:
        out = []
        for role, spec in self.roles.items():
            state = self.hass.states.get(spec["entity"])
            entry = {"role": role, "entity": spec["entity"]}
            if state is None:
                entry.update(state="unavailable", since=t)
            else:
                entry.update(state=state.state, since=clock.to_text(state.last_updated))
                unit = state.attributes.get("unit_of_measurement")
                if unit is not None:
                    entry["unit"] = unit
                attrs = l0.relevant_attrs(role, state.attributes)
                if attrs:
                    entry["attrs"] = attrs
            out.append(entry)
        return out

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        role = self.entities.get(entity_id)
        if role is None:
            return
        old, new = event.data["old_state"], event.data["new_state"]
        t = clock.to_text(event.time_fired)
        if new is None:
            # Removed from the registry: what Home Assistant itself shows for it.
            self._put(l0.state(t, self.subject, role, entity_id, "unavailable"))
            ir.async_create_issue(
                self.hass, DOMAIN, f"{ISSUE_ENTITY_REMOVED}_{self.subject.id}_{entity_id}",
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ENTITY_REMOVED,
                translation_placeholders={"entity": entity_id, "role": role,
                                          "name": self.config.get("name", self.subject.id)},
            )
            return
        if not _changed(role, old, new):
            return
        self._put(l0.state(
            t, self.subject, role, entity_id, new.state,
            unit=new.attributes.get("unit_of_measurement"),
            attrs=new.attributes, measured_at=_measured_at(self.specs[role], new),
            reported_before=_reported_before(old, t)))

    @callback
    def _on_heartbeat(self, now: datetime) -> None:
        self._put(l0.heartbeat(clock.to_text(now), self.subject, lines=self.lines_since_start))

    @callback
    def _on_midnight(self, now: datetime) -> None:
        self._roll(dt_util.as_local(now).date())
        self._notify()

    def _roll(self, day) -> None:
        if day != self._today:
            self._today = day
            self.lines_today = 0

    # --- the one writer ----------------------------------------------------

    def _put(self, line: l0.Line) -> None:
        self._queue.put_nowait(line)

    async def _write_forever(self) -> None:
        await self._count_stream()
        while True:
            line = await self._queue.get()
            try:
                month, size = await self.hass.async_add_executor_job(
                    _append, self.base, self.subject, line)
                self._wrote(line, month, size)
                self._notify()
            except Exception:
                _LOGGER.exception("vledger: could not write %s line for %s", line["kind"], self.subject.id)
            finally:
                self._queue.task_done()

    async def _count_stream(self) -> None:
        """Count what the stream already holds, before this start's lines."""
        midnight = dt_util.start_of_local_day()
        try:
            s = await self.hass.async_add_executor_job(
                lambda: stats.scan(self.base, self.subject, since=clock.to_text(midnight)))
        except Exception:
            _LOGGER.exception("vledger: could not count the stream of %s; counting from now",
                              self.subject.id)
            return
        self.month_bytes = dict(s.files)
        self.lines_today = s.lines_since
        self._today = midnight.date()
        self.last_line_at = s.last_line_at
        self.last_heartbeat_at = s.last_heartbeat_at
        last = s.last_state
        if last:
            self.last_state_role, self.last_state_at = last[0], last[1]["t"]
        self.gap_finder = s.finder

    def _wrote(self, line: l0.Line, month: str, size: int) -> None:
        t, kind = line["t"], line["kind"]
        self.month_bytes[month] = size
        self.last_line_at = t
        self.gap_finder.feed(line)
        if kind == "start":
            self.lines_since_start = 0
        elif kind == "heartbeat":
            self.last_heartbeat_at = t
        elif kind == "state":
            self.lines_since_start += 1
            self._roll(_local_date(t))
            self.lines_today += 1
            self.last_state_role, self.last_state_at = line["role"], t

    # --- the numbers -------------------------------------------------------

    @property
    def stream_bytes(self) -> int:
        return sum(self.month_bytes.values())

    @property
    def month_file_bytes(self) -> int | None:
        """The size of the current month's file: the newest one there is."""
        return self.month_bytes[max(self.month_bytes)] if self.month_bytes else None

    @property
    def month_files(self) -> int:
        return len(self.month_bytes)

    @property
    def gaps(self) -> list[l0.Gap]:
        return self.gap_finder.found

    def counts(self) -> dict:
        """Every number the diagnostic entities show, for diagnostics."""
        latest = self.gaps[-1] if self.gaps else None
        return {
            "status": self.status,
            "month_file_bytes": self.month_file_bytes,
            "stream_bytes": self.stream_bytes,
            "month_files": self.month_files,
            "lines_since_start": self.lines_since_start,
            "lines_today": self.lines_today,
            "last_line_at": self.last_line_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_state_role": self.last_state_role,
            "last_state_at": self.last_state_at,
            "gaps": len(self.gaps),
            "latest_gap": latest.__dict__ if latest else None,
        }

    # --- for the entities --------------------------------------------------

    def listen(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(cb)

        def remove() -> None:
            self._listeners.remove(cb)

        return remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()
