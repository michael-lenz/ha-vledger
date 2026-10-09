# SPDX-License-Identifier: BSD-3-Clause
"""The receipt form's time: the anchor when no candidate is chosen
(ADR-0015)."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VledgerConfigEntry
from .entity import FormEntity


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    desk = entry.runtime_data.desk
    if desk:
        add_entities(AnchorDateTime(desk, kind) for kind in desk.forms)


class AnchorDateTime(FormEntity, DateTimeEntity):
    def __init__(self, desk, kind: str) -> None:
        super().__init__(desk, kind, "time")

    @property
    def native_value(self) -> datetime | None:
        return self._form.time

    async def async_set_value(self, value: datetime) -> None:
        self._form.time = value
        self.async_write_ha_state()
