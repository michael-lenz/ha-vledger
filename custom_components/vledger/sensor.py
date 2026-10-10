# SPDX-License-Identifier: BSD-3-Clause
"""The capture status sensor (ADR-0008, 6) and, next to it, the diagnostic
sensors about the raw log (TASK-0008): what the stream holds and how it is
filling, without opening a file browser. For a vehicle, the event sensors
(ADR-0016): the last trip, refuelling and charging session, and how many
events wait for a receipt (ADR-0016), the last trip's consumption per
100 km, fuel and electricity, each a slice of the same trip line
(ADR-0025, point 5), and the metrics of the current month, year and
rolling period with the lifetime counters (ADR-0018) — read from L1,
never derived here (ARC-05)."""

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
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfInformation,
    UnitOfLength,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from vledger import clock

from . import VledgerConfigEntry
from .capture import Capture
from .const import STATUS_RECOMPUTING, STATUS_RUNNING, STATUS_STOPPED
from .entity import device_info
from .l1view import NOT_ATTRIBUTES, POSITIONS, TRIP, L1View
from .l1writer import L1Writer
from .metrics import (
    ELECTRICITY,
    EVERY,
    FUEL,
    KWH_PER_100KM,
    L_PER_100KM,
    METRICS,
    PERIODS,
    SUM,
    Metric,
    corrected,
    energies,
    unit_of,
    value_of,
)
from .receipt_desk import CHARGING, REFUELLING


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


@dataclass(frozen=True, kw_only=True)
class EventSensorDescription(SensorEntityDescription):
    #: The event kind whose last event this shows.
    kind: str
    #: (value key, its quality key), in order of preference: the first
    #: whose value the event carries is the state (ADR-0016, point 1).
    values: tuple[tuple[str, str], ...]


EVENT_SENSORS: tuple[EventSensorDescription, ...] = (
    EventSensorDescription(
        key="last_trip", translation_key="last_trip", kind=TRIP,
        device_class=SensorDeviceClass.DISTANCE, native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=1,
        values=(("distance_km", "distance_quality"),)),
    EventSensorDescription(
        key="last_refuelling", translation_key="last_refuelling", kind="refuelling",
        device_class=SensorDeviceClass.VOLUME, native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=2,
        values=(("quantity_l", "quantity_quality"), ("sensor_delta_l", "sensor_delta_quality"))),
    EventSensorDescription(
        key="last_charging_session", translation_key="last_charging_session", kind="charging",
        device_class=SensorDeviceClass.ENERGY, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        values=(("grid_kwh", "grid_kwh_quality"),)),
)


@dataclass(frozen=True, kw_only=True)
class ConsumptionSensorDescription(EventSensorDescription):
    #: The receipt kind whose gate this shares (ADR-0016, point 1): a fuel
    #: figure for a vehicle with a fuel, a battery figure with a capacity.
    gate: str
    #: The keys of the trip line this figure rests on, shown as attributes.
    attributes: tuple[str, ...]


CONSUMPTION_SENSORS: tuple[ConsumptionSensorDescription, ...] = (
    ConsumptionSensorDescription(
        key="last_trip_fuel_consumption", translation_key="last_trip_fuel_consumption",
        kind=TRIP, gate=REFUELLING, native_unit_of_measurement=L_PER_100KM,
        suggested_display_precision=1,
        values=(("fuel_l_per_100km", "fuel_l_per_100km_quality"),),
        attributes=("start", "end", "quality", "distance_km", "distance_quality",
                    "fuel_consumed_l", "fuel_consumed_quality", "fuel_consumed_source",
                    "fuel_consumed_error_l", "fuel_l_per_100km_error_pct")),
    ConsumptionSensorDescription(
        key="last_trip_electricity_consumption", translation_key="last_trip_electricity_consumption",
        kind=TRIP, gate=CHARGING, native_unit_of_measurement=KWH_PER_100KM,
        suggested_display_precision=1,
        values=(("battery_kwh_per_100km", "battery_kwh_per_100km_quality"),),
        attributes=("start", "end", "quality", "distance_km", "distance_quality",
                    "battery_consumed_kwh", "battery_consumed_quality",
                    "battery_consumed_error_kwh", "battery_kwh_per_100km_error_pct")),
)


# --- the metric entities (ADR-0018) ------------------------------------------

def metric_sensors(view: L1View, writer: L1Writer, capture: Capture,
                   currency: str) -> list[SensorEntity]:
    has = energies(view.kinds)
    out: list[SensorEntity] = [PeriodMetricSensor(view, writer, capture, m, period, currency)
                               for m in METRICS if m.needs in has for period in PERIODS]
    out += [LifetimeSensor(view, writer, capture, d) for d in LIFETIME_SENSORS if d.needs in has]
    return out


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    capture = entry.runtime_data.capture
    entities: list[SensorEntity] = [CaptureStatusSensor(capture),
                                    *(LogSensor(capture, d) for d in LOG_SENSORS)]
    view = entry.runtime_data.view
    if view:
        writer = entry.runtime_data.l1
        entities += [LastEventSensor(view, writer, capture, d)
                     for d in EVENT_SENSORS if d.kind in view.kinds]
        entities += [ConsumptionSensor(view, writer, capture, d)
                     for d in CONSUMPTION_SENSORS if d.gate in view.receipt_kinds]
        if view.receipt_kinds:
            entities.append(WaitingSensor(view, writer, capture))
        entities += metric_sensors(view, writer, capture, hass.config.currency)
    add_entities(entities)


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


class _L1Sensor(SensorEntity):
    """A sensor on the vehicle's device showing what the view read from L1:
    redrawn after every read, unavailable while L1 is rebuilt (ABL-08)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, view: L1View, writer: L1Writer, capture: Capture, key: str) -> None:
        self._view = view
        self._writer = writer
        self._capture = capture
        self._attr_unique_id = f"{capture.subject.id}_{key}"
        self._attr_device_info = device_info(capture.subject, capture.config)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._view.listen(self._changed))
        # The capture says when a rebuild begins and ends.
        self.async_on_remove(self._capture.listen(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._writer.available


class LastEventSensor(_L1Sensor):
    """The last completed event of one kind: its quantity as the state, its
    L1 line as the attributes (ADR-0016, points 1 to 3)."""

    entity_description: EventSensorDescription
    _unrecorded_attributes = POSITIONS

    def __init__(self, view: L1View, writer: L1Writer, capture: Capture,
                 description: EventSensorDescription) -> None:
        super().__init__(view, writer, capture, description.key)
        self.entity_description = description

    @property
    def _event(self) -> dict | None:
        return self._view.last.get(self.entity_description.kind)

    def _value(self) -> tuple[float | None, str | None]:
        e = self._event or {}
        for key, quality in self.entity_description.values:
            if e.get(key) is not None:
                return e[key], e.get(quality)
        return None, None

    @property
    def native_value(self) -> float | None:
        return self._value()[0]

    @property
    def extra_state_attributes(self) -> dict | None:
        e = self._event
        if e is None:
            return None
        attrs = {k: v for k, v in e.items() if k not in NOT_ATTRIBUTES}
        attrs["state_quality"] = self._value()[1]
        return attrs


class ConsumptionSensor(LastEventSensor):
    """The last trip's consumption per 100 km (ADR-0025, point 5): the rate
    as the state — unknown where the trip was too short for one — and the
    slice of the trip line it rests on as the attributes. No state class:
    one trip is not a statistic (ADR-0016, point 5)."""

    entity_description: ConsumptionSensorDescription

    @property
    def extra_state_attributes(self) -> dict | None:
        e = self._event
        if e is None:
            return None
        attrs = {k: e.get(k) for k in self.entity_description.attributes}
        attrs["state_quality"] = self._value()[1]
        return attrs


class WaitingSensor(_L1Sensor):
    """How many refuellings and charging sessions wait for a person to
    enter or settle a receipt (ADR-0016, point 4)."""

    _attr_translation_key = "unconfirmed_candidates"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, view: L1View, writer: L1Writer, capture: Capture) -> None:
        super().__init__(view, writer, capture, "unconfirmed_candidates")

    @property
    def native_value(self) -> int | None:
        if not self._view.waiting:
            return None       # not read yet
        return sum(sum(counts.values()) for counts in self._view.waiting.values())

    @property
    def extra_state_attributes(self) -> dict:
        return {f"{kind}_{state}": n
                for kind, counts in self._view.waiting.items() for state, n in counts.items()}


#: The attributes every metric entity shows of its line (ADR-0018, point 5).
LINE_ATTRIBUTES = ("start", "end", "open", "gaps")


class PeriodMetricSensor(_L1Sensor):
    """One metric of the current month, year or rolling period (ADR-0018):
    a sum restarts with its period, a rate or share is a level."""

    def __init__(self, view: L1View, writer: L1Writer, capture: Capture, metric: Metric,
                 period: str, currency: str) -> None:
        key = f"{metric.key}_{period}"
        super().__init__(view, writer, capture, key)
        self._metric = metric
        self._period = period
        self._attr_translation_key = key
        self._attr_entity_registry_enabled_default = period == "month"
        self._attr_device_class = metric.device_class
        self._attr_native_unit_of_measurement = unit_of(metric.unit, currency)
        self._attr_suggested_display_precision = metric.precision
        if metric.what != SUM:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif period != "rolling":
            self._attr_state_class = SensorStateClass.TOTAL
        # A rolling sum is a sliding window: no state_class (point 3).

    @property
    def _line(self) -> dict | None:
        return self._view.periods.get(self._period)

    @property
    def native_value(self) -> float | None:
        return value_of(self._metric, self._line)

    @property
    def last_reset(self) -> datetime | None:
        # A new period's start is how Home Assistant learns the sum restarted.
        if self.state_class is not SensorStateClass.TOTAL or self._line is None:
            return None
        return dt_util.parse_datetime(self._line["start"])

    @property
    def extra_state_attributes(self) -> dict | None:
        line = self._line
        if line is None:
            return None
        attrs = {k: line.get(k) for k in LINE_ATTRIBUTES}
        attrs["state_quality"] = line.get(f"{self._metric.key}_quality")
        attrs.update({k: line.get(k) for k in corrected(self._metric.needs)})
        return attrs


@dataclass(frozen=True, kw_only=True)
class LifetimeSensorDescription(SensorEntityDescription):
    needs: str
    #: The lifetime line's value key and its quality key.
    value: str
    quality: str
    attributes: tuple[str, ...] = ()


LIFETIME_SENSORS: tuple[LifetimeSensorDescription, ...] = (
    # total, not total_increasing: a rebuild can lower a counter (point 3).
    LifetimeSensorDescription(
        key="charge_cycles_total", translation_key="charge_cycles_total", needs=ELECTRICITY,
        state_class=SensorStateClass.TOTAL, suggested_display_precision=2,
        value="charge_cycles", quality="charge_cycles_quality"),
    LifetimeSensorDescription(
        key="tank_fills_total", translation_key="tank_fills_total", needs=FUEL,
        state_class=SensorStateClass.TOTAL, suggested_display_precision=2,
        value="tank_fills", quality="tank_fills_quality"),
    LifetimeSensorDescription(
        key="fuel_consumption", translation_key="fuel_consumption", needs=FUEL,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=L_PER_100KM,
        suggested_display_precision=1,
        value="consumption_l_per_100km", quality="consumption_quality",
        attributes=("consumption_from", "consumption_to", "consumption_receipts",
                    "consumption_error_pct")),
    # The overall consumption and totals (ADR-0025, point 6): the rates a
    # level, the sums total without last_reset, as the counters above.
    LifetimeSensorDescription(
        key="grid_kwh_per_100km_total", translation_key="grid_kwh_per_100km_total", needs=ELECTRICITY,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=KWH_PER_100KM,
        suggested_display_precision=1,
        value="grid_kwh_per_100km", quality="grid_kwh_per_100km_quality", attributes=("soc_corrected",)),
    LifetimeSensorDescription(
        key="battery_kwh_per_100km_total", translation_key="battery_kwh_per_100km_total", needs=ELECTRICITY,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=KWH_PER_100KM,
        suggested_display_precision=1,
        value="battery_kwh_per_100km", quality="battery_kwh_per_100km_quality", attributes=("soc_corrected",)),
    LifetimeSensorDescription(
        key="distance_km_total", translation_key="distance_km_total", needs=EVERY,
        state_class=SensorStateClass.TOTAL, device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS, suggested_display_precision=1,
        value="distance_km", quality="distance_km_quality"),
    LifetimeSensorDescription(
        key="fuel_consumed_l_total", translation_key="fuel_consumed_l_total", needs=FUEL,
        state_class=SensorStateClass.TOTAL, device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.LITERS, suggested_display_precision=2,
        value="fuel_consumed_l", quality="fuel_consumed_l_quality", attributes=("fuel_level_corrected",)),
    LifetimeSensorDescription(
        key="grid_kwh_total", translation_key="grid_kwh_total", needs=ELECTRICITY,
        state_class=SensorStateClass.TOTAL, device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2,
        value="grid_kwh", quality="grid_kwh_quality"),
    LifetimeSensorDescription(
        key="battery_kwh_total", translation_key="battery_kwh_total", needs=ELECTRICITY,
        state_class=SensorStateClass.TOTAL, device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2,
        value="battery_kwh", quality="battery_kwh_quality", attributes=("soc_corrected",)),
)


class LifetimeSensor(_L1Sensor):
    """A value of the lifetime line: the cumulative counters, starting
    values included (VER-11), the tank-to-tank consumption (VER-01), and
    the overall electricity rates and totals (ADR-0025, point 6)."""

    entity_description: LifetimeSensorDescription

    def __init__(self, view: L1View, writer: L1Writer, capture: Capture,
                 description: LifetimeSensorDescription) -> None:
        super().__init__(view, writer, capture, description.key)
        self.entity_description = description

    @property
    def _line(self) -> dict | None:
        return self._view.periods.get("lifetime")

    @property
    def native_value(self) -> float | None:
        return (self._line or {}).get(self.entity_description.value)

    @property
    def extra_state_attributes(self) -> dict | None:
        line = self._line
        if line is None:
            return None
        d = self.entity_description
        attrs = {k: line.get(k) for k in ("start", "end", "gaps")}
        attrs["state_quality"] = line.get(d.quality)
        attrs.update({k: line.get(k) for k in d.attributes})
        return attrs
