# SPDX-License-Identifier: BSD-3-Clause
"""The receipt form's event: enter the time by hand, or take a candidate's
start exactly (BEL-04, ADR-0015)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VledgerConfigEntry
from .const import EVENT_MANUAL
from .entity import FormEntity


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    desk = entry.runtime_data.desk
    if desk:
        add_entities(EventSelect(desk, kind) for kind in desk.forms)


class EventSelect(FormEntity, SelectEntity):
    def __init__(self, desk, kind: str) -> None:
        super().__init__(desk, kind, "event")

    @property
    def options(self) -> list[str]:
        return [EVENT_MANUAL, *self._desk.candidates[self._kind]]

    @property
    def current_option(self) -> str:
        return self._form.event

    async def async_select_option(self, option: str) -> None:
        self._form.event = option
        self.async_write_ha_state()
