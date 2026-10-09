# SPDX-License-Identifier: BSD-3-Clause
"""The receipt form's free text: place and note (ADR-0015)."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VledgerConfigEntry
from .entity import FormEntity


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    desk = entry.runtime_data.desk
    if desk:
        add_entities(FormText(desk, kind, key) for kind in desk.forms for key in ("place", "note"))


class FormText(FormEntity, TextEntity):
    _attr_native_max = 255

    def __init__(self, desk, kind: str, key: str) -> None:
        super().__init__(desk, kind, key)
        self._field = key

    @property
    def native_value(self) -> str:
        return getattr(self._form, self._field)

    async def async_set_value(self, value: str) -> None:
        setattr(self._form, self._field, value)
        self.async_write_ha_state()
