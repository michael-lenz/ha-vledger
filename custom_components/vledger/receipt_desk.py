# SPDX-License-Identifier: BSD-3-Clause
"""Entering receipts from Home Assistant (ADR-0015): one desk per vehicle,
which the actions and the dashboard form both go through.

The desk calls the functions the ``vledger receipt`` verbs call and nothing
else (ADR-0005; ADR-0013, point 6): the anchor and candidate check, the line
builders, the append that checks against the file. A check and its append
run as one executor job behind one lock, so two entries arriving together
cannot both find the same receipt current.

After a write the desk asks the vehicle's L1 writer to derive, and after
every run of the writer it reads the candidates again, so the form's event
select follows L1 (ADR-0015, point 2 and consequence 2).

The form is what a person is typing, per kind of receipt. It lives in
memory only: never restored, never written anywhere until the button enters
it, and read by nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from vledger import clock, l1, receipts

from .capture import Capture
from .const import DOMAIN, EVENT_MANUAL, FORM_CANDIDATES
from .l1writer import L1Writer

_LOGGER = logging.getLogger(__name__)

REFUELLING, CHARGING = receipts.EVENT_KINDS

#: The sensor's quantity a candidate's label shows, and its unit.
_SENSOR_QUANTITY = {REFUELLING: ("sensor_delta_l", "L"), CHARGING: ("grid_kwh", "kWh")}


def forms_of(config: dict) -> tuple[str, ...]:
    """The forms a vehicle gets: refuelling with a fuel, charging with a
    battery (ADR-0015, point 3)."""
    p = config.get("parameters") or {}
    out = []
    if p.get("fuel"):
        out.append(REFUELLING)
    if p.get("battery_net_kwh") is not None:
        out.append(CHARGING)
    return tuple(out)


def refused(e: Exception) -> ServiceValidationError:
    return ServiceValidationError(
        str(e), translation_domain=DOMAIN, translation_key="refused",
        translation_placeholders={"reason": str(e)})


def _label(kind: str, event: dict) -> str:
    start = dt_util.as_local(clock.parse(event["start"])).strftime("%Y-%m-%d %H:%M")
    key, unit = _SENSOR_QUANTITY[kind]
    value = event.get(key)
    return f"{start} · {value:.1f} {unit}" if isinstance(value, int | float) else start


class Form:
    """One kind's form: what has been typed so far."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.clear()

    def clear(self) -> None:
        self.event = EVENT_MANUAL
        self.time: datetime | None = None
        self.quantity: float | None = None  # litres or kWh, by kind
        self.total_price: float | None = None
        self.unit_price: float | None = None
        self.full = True
        self.place = ""
        self.note = ""


class ReceiptDesk:
    def __init__(self, hass: HomeAssistant, capture: Capture, writer: L1Writer) -> None:
        self.hass = hass
        self.subject = capture.subject
        self.base = capture.base
        self.config = capture.config
        self._writer = writer
        self.forms = {kind: Form(kind) for kind in forms_of(self.config)}
        #: Per kind, the event select's candidates: label -> start.
        self.candidates: dict[str, dict[str, str]] = {kind: {} for kind in receipts.EVENT_KINDS}
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unlisten: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        self._unlisten = self._writer.listen_runs(self._on_run)
        await self.async_refresh()

    @callback
    def async_stop(self) -> None:
        if self._unlisten:
            self._unlisten()
            self._unlisten = None

    @callback
    def _on_run(self) -> None:
        self.hass.async_create_background_task(
            self.async_refresh(), name=f"vledger candidates {self.subject.id}")

    # --- entering, as the actions do ---------------------------------------

    async def async_add(self, kind: str, *, anchor: datetime | None = None,
                        from_candidate: datetime | None = None, **values) -> dict:
        """Enter a refuelling or charging receipt; the line written."""
        t = clock.to_text(dt_util.utcnow())
        when = {"anchor": anchor and clock.to_text(dt_util.as_utc(anchor)),
                "from_candidate": from_candidate and clock.to_text(dt_util.as_utc(from_candidate))}
        line = await self._write(self._add, kind, t, when, values)
        await self._written()
        return line

    async def async_cancel(self, receipt: str, note: str | None = None) -> dict:
        t = clock.to_text(dt_util.utcnow())
        line = await self._write(
            lambda: receipts.cancel(t, self.subject, cancels=receipt, note=note))
        await self._written()
        return line

    async def _written(self) -> None:
        self._writer.async_derive_soon()
        await self.async_refresh()

    async def _write(self, build: Callable[..., dict], *args) -> dict:
        def job() -> dict:
            line = build(*args)
            receipts.append(self.base, self.subject, line)
            return line

        async with self._lock:
            try:
                return await self.hass.async_add_executor_job(job)
            except receipts.Refused as e:
                raise refused(e) from e

    def _add(self, kind: str, t: str, when: dict, values: dict) -> dict:
        anchor, exact = receipts.anchor_of(
            kind, **when, detected=lambda: l1.detected(self.base, self.subject, kind))
        build = receipts.refuelling if kind == REFUELLING else receipts.charging
        return build(t, self.subject, anchor=anchor, exact=exact, **values)

    # --- entering, as the form does ----------------------------------------

    async def async_submit(self, kind: str) -> dict:
        """Enter what the form holds. On success the form clears; on a
        refusal it keeps what was typed."""
        f = self.forms[kind]
        values = {"total_price": f.total_price, "place": f.place or None, "note": f.note or None}
        if kind == REFUELLING:
            values.update(quantity_l=f.quantity, unit_price=f.unit_price, full=f.full)
        else:
            values.update(energy_kwh=f.quantity)
        if f.event == EVENT_MANUAL:
            when = {"anchor": f.time}
        else:
            start = self.candidates[kind].get(f.event)
            when = {"from_candidate": start and clock.parse(start)}
        line = await self.async_add(kind, **when, **values)
        f.clear()
        self._notify()
        return line

    # --- the candidates ----------------------------------------------------

    async def async_refresh(self) -> None:
        """Read the unconfirmed candidates from L1 again (ARC-05)."""
        try:
            self.candidates = await self.hass.async_add_executor_job(self._candidates)
        except (ValueError, OSError):
            _LOGGER.exception("vledger: could not read the candidates of %s", self.subject.id)
        for kind, form in self.forms.items():
            if form.event != EVENT_MANUAL and form.event not in self.candidates[kind]:
                form.event = EVENT_MANUAL
        self._notify()

    def _candidates(self) -> dict[str, dict[str, str]]:
        """Newest first. A candidate a current receipt was entered from is
        left out even before L1 is rebuilt with it."""
        led = receipts.ledger(self.base, self.subject)
        out = {}
        for kind in receipts.EVENT_KINDS:
            taken = {clock.parse(r["anchor"]) for r in led.current(kind) if r.get("exact")}
            waiting = sorted(
                (e for e in l1.read(self.base, self.subject, kind)
                 if e.get("confirmation") == "unconfirmed" and clock.parse(e["start"]) not in taken),
                key=lambda e: clock.parse(e["start"]), reverse=True)
            labels: dict[str, str] = {}
            for e in waiting[:FORM_CANDIDATES]:
                label = first = _label(kind, e)
                n = 2
                while label in labels:
                    label, n = f"{first} ({n})", n + 1
                labels[label] = e["start"]
            out[kind] = labels
        return out

    # --- for the entities --------------------------------------------------

    def listen(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(cb)

        def remove() -> None:
            self._listeners.remove(cb)

        return remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()
