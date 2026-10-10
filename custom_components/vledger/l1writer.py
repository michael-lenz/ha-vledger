# SPDX-License-Identifier: BSD-3-Clause
"""The live derivation: an :class:`L1Writer` beside :class:`Capture`
(ADR-0009, consequence 2), writing L1 through the library's verbs only —
the rebuild check (``vledger l1 status``), the rebuild (``vledger derive
all --write``) and the incremental run that appends completed events.

One writer per subject, and every run holds one lock, so an incremental
run is never concurrent with a rebuild. Every run is an executor job in a
task of its own: capture never waits for one (ABL-08). The first run, the
rebuild check, comes once this start's ``config`` line is on disk — a
changed configuration is visible only then. After it, a state line asks
for an incremental run at most once a minute and a heartbeat at once:
completion is judged by the stream's last line, and when nothing moves a
heartbeat is the line that completes a trip.

While a rebuild runs, the capture status reads ``recomputing`` and
:attr:`L1Writer.available` is false, which is what a derived entity shows
(ABL-08). L1 is authoritative; an entity only displays it (ARC-05).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

import vledger.cli  # noqa: F401 — every derivation the verbs see registers itself in l1.DERIVATIONS, the periods in l1.PERIODS
from vledger import l0, l1

from .capture import Capture

_LOGGER = logging.getLogger(__name__)

#: After a state line, at most one incremental run per this many seconds.
DEBOUNCE_S = 60

#: Why a rebuild was run that no check asked for.
REQUESTED = "requested"


class L1Writer:
    def __init__(self, hass: HomeAssistant, capture: Capture) -> None:
        self.hass = hass
        self.capture = capture
        self._lock = asyncio.Lock()
        self._first: asyncio.Task | None = None
        self._timer: CALLBACK_TYPE | None = None
        self._unlisten: CALLBACK_TYPE | None = None
        self._pending = False
        self._stopped = False
        self._last_run: datetime | None = None
        self._after_run: list[Callable[[], None]] = []
        self._after_append: list[Callable[[dict[str, list[dict]]], None]] = []

        # For diagnostics: what the writer last did.
        self.last_run_at: str | None = None
        self.last_rebuild: dict | None = None

    @property
    def available(self) -> bool:
        """Whether a derived entity has anything to show: not while L1 is
        rebuilt (ABL-08)."""
        return not self.capture.recomputing

    # --- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        self._unlisten = self.capture.listen_lines(self._on_line)
        self._first = self._spawn(self._first_run())

    async def async_stop(self) -> None:
        """No run starts after this; one in the executor is waited for, so
        a reload never has two writers on one ``l1/``."""
        self._stopped = True
        self._cancel_timer()
        if self._unlisten:
            self._unlisten()
            self._unlisten = None
        async with self._lock:
            pass
        if self._first and not self._first.done():
            self._first.cancel()

    @callback
    def async_derive_soon(self) -> None:
        """A run as soon as the lock allows: a receipt was entered, and the
        rebuild check will find the receipts hash changed (ADR-0015, 2)."""
        self._request(at_once=True)

    def listen_runs(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``cb`` after every run, so what reads L1 reads it again."""
        self._after_run.append(cb)

        def remove() -> None:
            self._after_run.remove(cb)

        return remove

    def listen_appends(self, cb: Callable[[dict[str, list[dict]]], None]) -> CALLBACK_TYPE:
        """Call ``cb`` after every incremental run with the events new to L1,
        per kind — never after a rebuild, which re-derives history already
        reported (ADR-0020, point 2)."""
        self._after_append.append(cb)

        def remove() -> None:
            self._after_append.remove(cb)

        return remove

    async def async_recompute(self) -> None:
        """A rebuild on demand: the action ``vledger.recompute`` (ABL-02)."""
        await self._run(rebuild=REQUESTED)

    async def async_read(self, job: Callable[[], object]) -> object:
        """Run ``job`` in the executor while no run is under way, so a reader
        of ``l1/`` never meets a directory about to be swapped (ADR-0017,
        point 4). A run requested meanwhile follows it."""
        async with self._lock:
            return await self.hass.async_add_executor_job(job)

    # --- when to run -------------------------------------------------------

    async def _first_run(self) -> None:
        await self.capture.async_flushed()
        await self._run()

    @callback
    def _on_line(self, line: l0.Line) -> None:
        if line["kind"] == "heartbeat":
            self._request(at_once=True)
        elif line["kind"] == "state":
            self._request(at_once=False)

    @callback
    def _request(self, *, at_once: bool) -> None:
        if self._stopped:
            return
        if self._lock.locked():
            # The run under way may have read L0 before this line: one more
            # after it catches what it missed.
            self._pending = True
            return
        if not at_once and self._last_run is not None:
            wait = DEBOUNCE_S - (dt_util.utcnow() - self._last_run).total_seconds()
            if wait > 0:
                if self._timer is None:
                    self._timer = async_call_later(self.hass, wait, self._on_timer)
                return
        self._cancel_timer()
        self._spawn(self._run())

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._timer = None
        self._request(at_once=True)

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    def _spawn(self, coro: Coroutine) -> asyncio.Task:
        return self.hass.async_create_background_task(
            coro, name=f"vledger L1 {self.capture.subject.id}")

    # --- the one writer ----------------------------------------------------

    async def _run(self, *, rebuild: str | None = None) -> None:
        base, subject = self.capture.base, self.capture.subject
        added: dict[str, list[dict]] = {}
        async with self._lock:
            if self._stopped:
                return
            self._pending = False
            self._last_run = dt_util.utcnow()
            self.last_run_at = self._last_run.isoformat()
            try:
                reason = rebuild or await self.hass.async_add_executor_job(l1.rebuild_due, base, subject)
                if reason:
                    await self._rebuild(reason)
                else:
                    added = await self.hass.async_add_executor_job(l1.incremental, base, subject)
            except Exception:
                _LOGGER.exception("vledger: could not derive L1 for %s", subject.id)
        for cb in list(self._after_run):
            cb()
        if any(added.values()):
            for cb in list(self._after_append):
                cb(added)
        if self._pending:
            self._request(at_once=False)

    async def _rebuild(self, reason: str) -> None:
        _LOGGER.info("vledger: rebuilding L1 for %s: %s", self.capture.subject.id, reason)
        self.capture.set_recomputing(True)
        try:
            manifest = await self.hass.async_add_executor_job(
                l1.rebuild, self.capture.base, self.capture.subject)
        finally:
            self.capture.set_recomputing(False)
        self.last_rebuild = {"reason": reason, "derived_at": manifest["derived_at"]}

    def counts(self) -> dict:
        """What diagnostics show of the writer."""
        return {"available": self.available, "last_run_at": self.last_run_at,
                "last_rebuild": self.last_rebuild}
