# SPDX-License-Identifier: BSD-3-Clause
"""What every vledger entity shares: the subject's device, and for the
receipt form (ADR-0015, point 3) a base that redraws when the desk moves."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from vledger.layout import Subject

from .const import DOMAIN
from .receipt_desk import ReceiptDesk


def device_info(subject: Subject, config: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, subject.id)},
        name=config.get("name", subject.id),
        manufacturer="vledger",
        model=subject.kind,
    )


class FormEntity(Entity):
    """One field of one kind's receipt form: reads and writes the desk's
    form, which lives in memory only — nothing here is restored."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, desk: ReceiptDesk, kind: str, key: str) -> None:
        self._desk = desk
        self._kind = kind
        self._form = desk.forms[kind]
        self._attr_unique_id = f"{desk.subject.id}_{kind}_receipt_{key}"
        self._attr_translation_key = f"{kind}_receipt_{key}"
        self._attr_device_info = device_info(desk.subject, desk.config)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._desk.listen(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()
