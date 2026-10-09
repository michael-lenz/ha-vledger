# SPDX-License-Identifier: BSD-3-Clause
"""The receipt form's confirmation: enters what the form holds, exactly as
the action would (ADR-0015). A refusal is raised to the frontend and the
form keeps what was typed."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VledgerConfigEntry
from .entity import FormEntity


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    desk = entry.runtime_data.desk
    if desk:
        add_entities(EnterButton(desk, kind) for kind in desk.forms)


class EnterButton(FormEntity, ButtonEntity):
    def __init__(self, desk, kind: str) -> None:
        super().__init__(desk, kind, "enter")

    async def async_press(self) -> None:
        await self._desk.async_submit(self._kind)
