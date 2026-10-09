# SPDX-License-Identifier: BSD-3-Clause
"""The capture status sensor (ADR-0008, 6) and, next to it, the diagnostic
sensors about the raw log (TASK-0008): what the stream holds and how it is
filling, without opening a file browser."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from vledger import clock

from . import VledgerConfigEntry
from .capture import Capture
from .const import STATUS_RECOMPUTING, STATUS_RUNNING, STATUS_STOPPED
from .entity import device_info


def _time(t: str | None) -> datetime | None:
    return clock.parse(t) if t else None


def _latest_gap(c: Capture) -> dict:
    return c.gaps[-1].__dict__ if c.gaps else {}


@dataclass(frozen=True, kw_only=True)
class LogSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Capture], object]
    attrs_fn: Callable[[Capture], dict] | None = None


LOG_SENSORS: tuple[LogSensorDescription, ...] = (
    LogSensorDescription(
        key="month_file_size", translation_key="month_file_size",
        device_class=SensorDeviceClass.DATA_SIZE, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        value_fn=lambda c: c.month_file_bytes),
    LogSensorDescription(
        key="stream_size", translation_key="stream_size",
        device_class=SensorDeviceClass.DATA_SIZE, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        value_fn=lambda c: c.stream_bytes),
    LogSensorDescription(
        key="month_files", translation_key="month_files",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.month_files),
    LogSensorDescription(
        key="last_line", translation_key="last_line",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: _time(c.last_line_at)),
    LogSensorDescription(
        key="last_heartbeat", translation_key="last_heartbeat",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: _time(c.last_heartbeat_at)),
    LogSensorDescription(
        key="last_state_line", translation_key="last_state_line",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: _time(c.last_state_at),
        attrs_fn=lambda c: {"role": c.last_state_role}),
    LogSensorDescription(
        key="lines_since_start", translation_key="lines_since_start",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.lines_since_start),
    LogSensorDescription(
        key="lines_today", translation_key="lines_today",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.lines_today),
    # A stream is never rewritten, so its gaps only ever grow in number.
    LogSensorDescription(
        key="capture_gaps", translation_key="capture_gaps",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: len(c.gaps)),
    LogSensorDescription(
        key="latest_capture_gap", translation_key="latest_capture_gap",
        device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS, suggested_display_precision=0,
        value_fn=lambda c: round(c.gaps[-1].seconds) if c.gaps else None,
        attrs_fn=lambda c: {k: _latest_gap(c).get(k) for k in ("reason", "start", "end")}),
)


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    capture = entry.runtime_data.capture
    add_entities([CaptureStatusSensor(capture),
                  *(LogSensor(capture, d) for d in LOG_SENSORS)])


class _CaptureSensor(SensorEntity):
    """A sensor on the subject's device that redraws whenever the capture moves."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, capture: Capture, key: str) -> None:
        self._capture = capture
        self._attr_unique_id = f"{capture.subject.id}_{key}"
        self._attr_device_info = device_info(capture.subject, capture.config)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._capture.listen(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()


class CaptureStatusSensor(_CaptureSensor):
    _attr_translation_key = "capture_status"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, capture: Capture) -> None:
        super().__init__(capture, "capture_status")
        self._attr_options = [STATUS_RUNNING, STATUS_STOPPED, STATUS_RECOMPUTING]

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


class LogSensor(_CaptureSensor):
    """One number about the raw log, out of the way in the diagnostic category."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: LogSensorDescription

    def __init__(self, capture: Capture, description: LogSensorDescription) -> None:
        super().__init__(capture, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self._capture)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        return fn(self._capture) if fn else None
