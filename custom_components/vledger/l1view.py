# SPDX-License-Identifier: BSD-3-Clause
"""What the event entities display (ADR-0016): per vehicle, the last event
of every kind it has and the count of events waiting for a receipt.

The view reads L1 through the library's functions only (``l1.last``,
``l1.waiting``; ADR-0016, point 8), in one executor job that all of the
vehicle's event entities share. It reads once at start, so the entities
show what is on disk before the writer's first run, and again after every
run of the writer (point 7). L1 is authoritative; this only displays it
(ARC-05).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from vledger import l1, receipts

from .capture import Capture
from .l1writer import L1Writer
from .receipt_desk import forms_of

_LOGGER = logging.getLogger(__name__)

TRIP = "trip"


def kinds_of(config: dict) -> tuple[str, ...]:
    """The event kinds a vehicle shows: trips always, refuellings and
    charging sessions as the receipt forms gate them (ADR-0016, point 1)."""
    return (TRIP, *forms_of(config))


class L1View:
    def __init__(self, hass: HomeAssistant, capture: Capture, writer: L1Writer) -> None:
        self.hass = hass
        self.subject = capture.subject
        self.base = capture.base
        self._writer = writer
        self.kinds = kinds_of(capture.config)
        #: Per kind, the last completed event, or None.
        self.last: dict[str, dict | None] = dict.fromkeys(self.kinds)
        #: Per kind that takes a receipt, the count per waiting confirmation.
        self.waiting: dict[str, dict[str, int]] = {}
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unlisten: CALLBACK_TYPE | None = None

    @property
    def receipt_kinds(self) -> tuple[str, ...]:
        return tuple(k for k in self.kinds if k in receipts.EVENT_KINDS)

    @callback
    def async_start(self) -> None:
        self._unlisten = self._writer.listen_runs(self._on_run)
        self._on_run()

    @callback
    def async_stop(self) -> None:
        if self._unlisten:
            self._unlisten()
            self._unlisten = None

    @callback
    def _on_run(self) -> None:
        self.hass.async_create_background_task(
            self.async_refresh(), name=f"vledger L1 view {self.subject.id}")

    async def async_refresh(self) -> None:
        # Serialised, so an earlier read never lands after a later one.
        async with self._lock:
            try:
                self.last, self.waiting = await self.hass.async_add_executor_job(self._read)
            except Exception:
                _LOGGER.exception("vledger: could not read L1 of %s", self.subject.id)
                return
        self._notify()

    def _read(self) -> tuple[dict[str, dict | None], dict[str, dict[str, int]]]:
        last = {}
        for kind in self.kinds:
            events = l1.last(self.base, self.subject, kind)
            last[kind] = events[-1] if events else None
        waiting = {}
        if self.receipt_kinds:
            counts = l1.waiting(self.base, self.subject)
            waiting = {k: counts[k] for k in self.receipt_kinds}
        return last, waiting

    def listen(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(cb)

        def remove() -> None:
            self._listeners.remove(cb)

        return remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()
