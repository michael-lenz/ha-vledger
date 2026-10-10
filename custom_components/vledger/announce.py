# SPDX-License-Identifier: BSD-3-Clause
"""Events and notifications (ADR-0020): what an incremental run of the
writer appended to L1, told to Home Assistant's bus and, for a vehicle
with a notify target, to a person.

Every event new to L1 fires ``vledger_event``; one that waits for a
receipt fires ``vledger_candidate`` right after it. A rebuild fires
nothing: the writer hands over only what an incremental run returned,
and the library decides what is new (``l1.incremental``, point 2) and what
waits (``l1.is_waiting``, point 6). A notification is a courtesy: a
target that refuses it is logged, and L1 stays the record (ARC-05).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from vledger import l1

from .capture import Capture
from .const import (
    DOMAIN,
    EVENT_CANDIDATE,
    EVENT_EVENT,
    NOTIFY_COMPANION_PREFIX,
    NOTIFY_SUMMARY_ABOVE,
    OPT_NOTIFY_TARGET,
)
from .l1view import NOT_ATTRIBUTES, POSITIONS
from .l1writer import L1Writer

_LOGGER = logging.getLogger(__name__)

#: The quantity a notification names, per kind: the first key the event
#: carries a value for, as the event entities choose their state (ADR-0016).
QUANTITY = {"refuelling": ("quantity_l", "sensor_delta_l"), "charging": ("grid_kwh",)}


def event_data(entry: ConfigEntry, subject_id: str, event: dict) -> dict:
    """What both bus events carry (ADR-0020, point 1): the entry, the
    vehicle, the subject and the kind, then the L1 line as the event
    entities show it — without positions, which stay out of the recorder."""
    data = {"config_entry_id": entry.entry_id, "vehicle": entry.title,
            "subject": subject_id, "kind": event["kind"]}
    data.update((k, v) for k, v in event.items() if k not in NOT_ATTRIBUTES | POSITIONS)
    return data


def quantity_of(event: dict) -> float | None:
    for key in QUANTITY.get(event["kind"], ()):
        if event.get(key) is not None:
            return event[key]
    return None


class Announcer:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, capture: Capture,
                 writer: L1Writer) -> None:
        self.hass = hass
        self.entry = entry
        self.subject = capture.subject
        self._writer = writer
        self._unlisten: CALLBACK_TYPE | None = None

    @property
    def target(self) -> str | None:
        """The notify action, ``notify.<name>``, or None: no notifications."""
        return self.entry.options.get(OPT_NOTIFY_TARGET) or None

    @callback
    def async_start(self) -> None:
        self._unlisten = self._writer.listen_appends(self._on_appended)

    @callback
    def async_stop(self) -> None:
        if self._unlisten:
            self._unlisten()
            self._unlisten = None

    @callback
    def _on_appended(self, added: dict[str, list[dict]]) -> None:
        events = sorted((e for new in added.values() for e in new), key=lambda e: e["start"])
        candidates = []
        for event in events:
            data = event_data(self.entry, self.subject.id, event)
            self.hass.bus.async_fire(EVENT_EVENT, data)
            if l1.is_waiting(event):
                self.hass.bus.async_fire(EVENT_CANDIDATE, data)
                candidates.append(event)
        if candidates and self.target:
            self.hass.async_create_background_task(
                self._notify(candidates), name=f"vledger notify {self.subject.id}")

    # --- notifications (ADR-0020, points 3 to 5) ----------------------------

    async def _notify(self, candidates: list[dict]) -> None:
        texts = await async_get_translations(self.hass, self.hass.config.language, "common", [DOMAIN])

        def text(key: str, **placeholders) -> str:
            return texts[f"component.{DOMAIN}.common.{key}"].format(
                vehicle=self.entry.title, **placeholders)

        if len(candidates) > NOTIFY_SUMMARY_ABOVE:
            await self._send(text("summary_title", count=len(candidates)), text("summary_message"),
                             f"{DOMAIN}-{self.subject.id}-summary", text("action_enter_receipt"))
            return
        for event in candidates:
            title, message = self._compose(event, text)
            await self._send(title, message,
                             f"{DOMAIN}-{self.subject.id}-{event['kind']}-{event['start']}",
                             text("action_enter_receipt"))

    def _compose(self, event: dict, text) -> tuple[str, str]:
        kind = event["kind"]
        start = dt_util.as_local(dt_util.parse_datetime(event["start"])).strftime("%Y-%m-%d %H:%M")
        q = quantity_of(event)
        if q is None:
            message = text(f"{kind}_message_bare", time=start)
        else:
            message = text(f"{kind}_message", time=start, quantity=f"{q:g}")
        price = event.get("price_suggestion")
        if kind == "refuelling" and price is not None:
            message += " " + text("price_suggestion", price=f"{price:g}")
        return text(f"{kind}_title"), message

    async def _send(self, title: str, message: str, tag: str, action_title: str) -> None:
        target = self.target
        domain, _, service = target.partition(".")
        payload: dict = {"title": title, "message": message, "data": {"tag": tag}}
        if service.startswith(NOTIFY_COMPANION_PREFIX):
            device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, self.subject.id)})
            if device:
                payload["data"]["actions"] = [{"action": "URI", "title": action_title,
                                               "uri": f"/config/devices/device/{device.id}"}]
        try:
            await self.hass.services.async_call(domain, service, payload, blocking=True)
        except Exception:
            _LOGGER.warning("vledger: notify target %s refused a notification for %s",
                            target, self.subject.id, exc_info=True)
