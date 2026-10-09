# SPDX-License-Identifier: BSD-3-Clause
"""Capture: ``state_changed`` events become L0 lines (ADR-0008, point 5).

One :class:`Capture` per subject. Every line goes through one writer: a
queue the event loop puts lines on, drained by one task that appends them
in the executor, one at a time, in order. On stop the queue is drained
before the stop line goes, so a stop is always the last line.
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
    async_track_time_interval,
)

from vledger import __version__, clock, l0
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
        self.lines_since_start = 0
        self.last_line_at: str | None = None
        self.last_heartbeat_at: str | None = None
        self.stop_reason: str | None = None

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
            attrs=new.attributes, measured_at=_measured_at(self.specs[role], new)))

    @callback
    def _on_heartbeat(self, now: datetime) -> None:
        t = clock.to_text(now)
        self._put(l0.heartbeat(t, self.subject, lines=self.lines_since_start))
        self.last_heartbeat_at = t
        self._notify()

    # --- the one writer ----------------------------------------------------

    def _put(self, line: l0.Line) -> None:
        self._queue.put_nowait(line)

    async def _write_forever(self) -> None:
        while True:
            line = await self._queue.get()
            try:
                await self.hass.async_add_executor_job(l0.append, self.base, self.subject, line)
                if line["kind"] == "state":
                    self.lines_since_start += 1
                self.last_line_at = line["t"]
                self._notify()
            except Exception:
                _LOGGER.exception("vledger: could not write %s line for %s", line["kind"], self.subject.id)
            finally:
                self._queue.task_done()

    # --- for the entities --------------------------------------------------

    def listen(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(cb)

        def remove() -> None:
            self._listeners.remove(cb)

        return remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()
