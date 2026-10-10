# SPDX-License-Identifier: BSD-3-Clause
"""The metrics of a period line as Home Assistant shows them (ADR-0018): which
a vehicle has, what each one is, its device class and unit. The metric
entities and the external statistics (ADR-0019) both read this table."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy, UnitOfLength, UnitOfVolume

from .receipt_desk import CHARGING, REFUELLING

#: Which energies a metric needs (ADR-0018, point 2).
EVERY, FUEL, ELECTRICITY, BOTH = "every", "fuel", "electricity", "both"
#: What a metric is, which decides its state_class (point 3).
SUM, RATE, SHARE = "sum", "rate", "share"
#: The periods with entities; month enabled by default, the others not (point 1).
PERIODS = ("month", "year", "rolling")
#: Stands for the instance's currency in a unit (point 4).
CURRENCY = "{currency}"
#: The rates per distance, in Home Assistant's own spelling of the units
#: (ADR-0018, point 4; ISSUE-0028): shown as they are, unconverted, and
#: spelled the one way on every entity that carries one (ISSUE-0038), so
#: that the device class of ISSUE-0028, should it come, does not change
#: the unit the statistics are kept in.
L_PER_100KM, KWH_PER_100KM = "L/100km", "kWh/100km"


@dataclass(frozen=True)
class Metric:
    key: str
    #: The English name, for what Home Assistant does not translate: the
    #: external statistics' names (ADR-0019, point 1). The entities' names
    #: are the translations'.
    label: str
    needs: str
    what: str
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    precision: int | None = None


METRICS: tuple[Metric, ...] = (
    Metric("distance_km", "Distance", EVERY, SUM, SensorDeviceClass.DISTANCE, UnitOfLength.KILOMETERS, 1),
    Metric("eur_per_100km", "Cost per 100 km", EVERY, RATE, None, f"{CURRENCY}/100km", 2),
    Metric("fuel_purchased_l", "Fuel purchased", FUEL, SUM, SensorDeviceClass.VOLUME, UnitOfVolume.LITERS, 2),
    Metric("fuel_consumed_l", "Fuel consumed", FUEL, SUM, SensorDeviceClass.VOLUME, UnitOfVolume.LITERS, 2),
    Metric("fuel_cost_eur", "Fuel cost", FUEL, SUM, SensorDeviceClass.MONETARY, CURRENCY, 2),
    Metric("tank_fills", "Tank fills", FUEL, SUM, None, None, 2),
    Metric("grid_kwh", "Grid energy", ELECTRICITY, SUM, SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, 2),
    Metric("battery_kwh", "Battery energy", ELECTRICITY, SUM, SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, 2),
    Metric("electricity_cost_eur", "Electricity cost", ELECTRICITY, SUM, SensorDeviceClass.MONETARY, CURRENCY, 2),
    Metric("grid_kwh_per_100km", "Grid energy per 100 km", ELECTRICITY, RATE, None, KWH_PER_100KM, 1),
    Metric("battery_kwh_per_100km", "Battery energy per 100 km", ELECTRICITY, RATE, None, KWH_PER_100KM, 1),
    Metric("charge_cycles", "Charge cycles", ELECTRICITY, SUM, None, None, 2),
    Metric("fuel_eur_per_100km", "Fuel cost per 100 km", BOTH, RATE, None, f"{CURRENCY}/100km", 2),
    Metric("electricity_eur_per_100km", "Electricity cost per 100 km", BOTH, RATE, None, f"{CURRENCY}/100km", 2),
    Metric("electric_energy_share", "Electric share of energy", BOTH, SHARE, None, "%", 1),
    Metric("electric_distance_share", "Electric share of distance", BOTH, SHARE, None, "%", 1),
)


def energies(kinds: tuple[str, ...]) -> frozenset[str]:
    """The energies a vehicle has, by the gate of ADR-0016: from the event
    kinds it shows, which are the receipt forms it gets."""
    out = {EVERY}
    if REFUELLING in kinds:
        out.add(FUEL)
    if CHARGING in kinds:
        out.add(ELECTRICITY)
    if {FUEL, ELECTRICITY} <= out:
        out.add(BOTH)
    return frozenset(out)


def unit_of(unit: str | None, currency: str) -> str | None:
    return unit.replace(CURRENCY, currency) if unit else unit


def corrected(needs: str) -> tuple[str, ...]:
    """The stock corrections a metric's attributes show (ADR-0018, point 5)."""
    return {FUEL: ("fuel_level_corrected",), ELECTRICITY: ("soc_corrected",),
            BOTH: ("fuel_level_corrected", "soc_corrected")}.get(needs, ())


def value_of(metric: Metric, line: dict | None) -> float | None:
    """The metric's value as shown: L1's, a share in per cent (ADR-0018, point 4)."""
    value = (line or {}).get(metric.key)
    if value is not None and metric.what == SHARE:
        return round(value * 100, 2)
    return value
