# SPDX-License-Identifier: BSD-3-Clause
"""The capture status sensor — the one entity of this step (ADR-0007, 6)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import VledgerConfigEntry
from .capture import Capture
from .const import DOMAIN, STATUS_RECOMPUTING, STATUS_RUNNING, STATUS_STOPPED


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    add_entities([CaptureStatusSensor(entry.runtime_data)])


class CaptureStatusSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "capture_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_should_poll = False

    def __init__(self, capture: Capture) -> None:
        self._capture = capture
        self._attr_options = [STATUS_RUNNING, STATUS_STOPPED, STATUS_RECOMPUTING]
        self._attr_unique_id = f"{capture.subject.id}_capture_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, capture.subject.id)},
            name=capture.config.get("name", capture.subject.id),
            manufacturer="vledger",
            model=capture.subject.kind,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._capture.listen(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return self._capture.status

    @property
    def extra_state_attributes(self) -> dict:
        c = self._capture
        return {
            "lines_since_start": c.lines_since_start,
            "last_line_at": c.last_line_at,
            "last_heartbeat_at": c.last_heartbeat_at,
            "base_path": str(c.base),
            "subject": c.subject.id,
        }
