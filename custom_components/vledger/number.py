# SPDX-License-Identifier: BSD-3-Clause
"""The receipt form's amounts: quantity and prices, typed as on the
receipt (ADR-0015)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VledgerConfigEntry
from .entity import FormEntity
from .receipt_desk import REFUELLING

#: key -> the form attribute, the largest value, the step.
FIELDS = {"quantity": ("quantity", 1000, 0.01),
          "total_price": ("total_price", 100000, 0.01),
          "unit_price": ("unit_price", 100, 0.001)}


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    desk = entry.runtime_data.desk
    if not desk:
        return
    currency = hass.config.currency
    entities = []
    for kind in desk.forms:
        quantity_unit = UnitOfVolume.LITERS if kind == REFUELLING else UnitOfEnergy.KILO_WATT_HOUR
        entities += [FormNumber(desk, kind, "quantity", quantity_unit),
                     FormNumber(desk, kind, "total_price", currency)]
        if kind == REFUELLING:
            entities.append(FormNumber(desk, kind, "unit_price", f"{currency}/{UnitOfVolume.LITERS}"))
    add_entities(entities)


class FormNumber(FormEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0

    def __init__(self, desk, kind: str, key: str, unit: str) -> None:
        super().__init__(desk, kind, key)
        self._field, self._attr_native_max_value, self._attr_native_step = FIELDS[key]
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        return getattr(self._form, self._field)

    async def async_set_native_value(self, value: float) -> None:
        setattr(self._form, self._field, value)
        self.async_write_ha_state()
