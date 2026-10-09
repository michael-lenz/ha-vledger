# SPDX-License-Identifier: BSD-3-Clause
"""The refuelling form's full tank, on by default (ADR-0015)."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VledgerConfigEntry
from .entity import FormEntity
from .receipt_desk import REFUELLING


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    desk = entry.runtime_data.desk
    if desk and REFUELLING in desk.forms:
        add_entities([FullTankSwitch(desk, REFUELLING)])


class FullTankSwitch(FormEntity, SwitchEntity):
    def __init__(self, desk, kind: str) -> None:
        super().__init__(desk, kind, "full")

    @property
    def is_on(self) -> bool:
        return self._form.full

    async def async_turn_on(self, **kwargs) -> None:
        self._form.full = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._form.full = False
        self.async_write_ha_state()
