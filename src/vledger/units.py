# SPDX-License-Identifier: BSD-3-Clause
"""Units: from what the source reported to what L1 uses (FZG-08).

L1 speaks km, L, kWh, %, °C and L/100 km. A source unit is whatever Home
Assistant's ``unit_of_measurement`` said at the time, kept in L0;
conversion happens here, once, on the way into a derivation.
"""

from __future__ import annotations

#: The L1 unit per quantity, and the factor from every source unit to it.
_TABLES: dict[str, tuple[str, dict[str, float]]] = {
    "distance": ("km", {"km": 1.0, "m": 0.001, "mi": 1.609344, "ft": 0.0003048, "yd": 0.0009144}),
    "volume": ("L", {"L": 1.0, "mL": 0.001, "gal": 3.785411784, "fl. oz.": 0.0295735296}),
    "energy": ("kWh", {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0, "MJ": 1 / 3.6, "kJ": 1 / 3600}),
    "percent": ("%", {"%": 1.0}),
    # The trip computer's average (ADR-0025). Miles per gallon is not a
    # factor away from it, so a source in mpg stays unconverted.
    "consumption": ("L/100 km", {"L/100 km": 1.0, "L/100km": 1.0, "l/100km": 1.0}),
}


def quantity_of(role: str) -> str | None:
    """What a role measures, or ``None`` when it is not a number."""
    return {
        "odometer": "distance", "trip_distance": "distance", "fuel_level": "volume",
        "soc": "percent", "energy_meter": "energy", "outside_temperature": "temperature",
        "fuel_price": "price", "power": "power", "trip_consumption": "consumption",
    }.get(role)


def normal_unit(quantity: str) -> str:
    if quantity == "temperature":
        return "°C"
    return _TABLES[quantity][0]


def convert(value: float, unit: str | None, quantity: str) -> float:
    """``value`` in ``unit`` as the quantity's L1 unit.

    A missing unit is taken as the L1 unit — the source said nothing, and
    guessing anything else would be invention. An unknown unit is an error:
    a wrong factor is worse than no value.
    """
    if quantity == "temperature":
        if unit in (None, "°C", "C"):
            return float(value)
        if unit in ("°F", "F"):
            return (float(value) - 32) * 5 / 9
        if unit == "K":
            return float(value) - 273.15
        raise ValueError(f"unknown temperature unit {unit!r}")
    if quantity in ("price", "power"):
        return float(value)
    normal, factors = _TABLES[quantity]
    if unit is None:
        return float(value)
    if unit not in factors:
        raise ValueError(f"unknown {quantity} unit {unit!r}; L1 uses {normal}")
    return float(value) * factors[unit]


def number(text: str) -> float | None:
    """The number a state string holds, or ``None`` for unavailable, unknown
    and anything else that is not one."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None
