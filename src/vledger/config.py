# SPDX-License-Identifier: BSD-3-Clause
"""The configuration of a vehicle or a charge point, as the ``config`` line
holds it (ADR-0007): the defaults, the vocabulary, and the two questions
asked of it — is it complete, and which tariff holds at a time.

The integration edits this structure in its options flow and writes it to
L0 verbatim; the derivation reads it back from L0. Both import what is
here rather than restating it (ADR-0005, consequence 2).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date

from vledger import clock
from vledger.l0 import CHARGEPOINT_ROLES, VEHICLE_ROLES

#: A role whose change means the vehicle moved; one is mandatory (FZG-02).
MOVEMENT_ROLES = ("odometer", "position", "position_latitude",
                  "position_longitude", "trip_distance")

#: The enumerated roles and the one positive domain state each maps to
#: (FZG-05). A source value not mapped, and unavailable or unknown, holds
#: the last known domain state.
DOMAIN_STATES = {"charging_state": "charging", "plug_state": "plugged", "ignition": "on"}

FUELS = ("petrol", "diesel")

#: The thresholds and time constants of FZG-06 with the requirements'
#: defaults, units in the key. Always written out in full (ADR-0007).
DEFAULT_THRESHOLDS = {
    "t_still_s": 1800,              # FAH-01
    "refuel_threshold_l": 3,        # TNK-01
    "t_settle_s": 360,              # TNK-02
    "charging_threshold_pct": 2,    # LAD-04
    "matching_tolerance_s": 21600,  # BEL-05
    "plausibility_pct": 15,         # BEL-08
    "heartbeat_s": 3600,            # ERF-04
    "outage_s": 86400,              # HAI-08
    "rolling_period_d": 30,         # VER-06
    "consumption_error_pct": 5,     # VER-10
    "heating_value_kwh_per_l": 8.9, # VER-05, petrol; diesel 9.8
    "beta_per_k": 9.5e-4,           # VER-08, petrol; diesel 8.0e-4
    "temperature_tau_s": 10800,     # VER-08
}

#: Per fuel, the thresholds whose default depends on it.
FUEL_THRESHOLDS = {
    "petrol": {"heating_value_kwh_per_l": 8.9, "beta_per_k": 9.5e-4},
    "diesel": {"heating_value_kwh_per_l": 9.8, "beta_per_k": 8.0e-4},
}

#: The vehicle parameters (FZG-04 and later), ``None`` when not set.
DEFAULT_PARAMETERS = {
    "fuel": None,
    "tank_capacity_l": None,
    "battery_net_kwh": None,
    "fuel_level_resolution_l": None,   # measured on the reference vehicle, TASK-0002
    "charging_loss_factor": 1.12,      # LAD-06
    "eta_el": 0.85,                    # VER-05
    "eta_ice": 0.28,                   # VER-05, petrol; diesel 0.33
    "charge_cycles_start": 0,          # VER-11
    "tank_fills_start": 0,             # VER-11
}

FUEL_PARAMETERS = {"petrol": {"eta_ice": 0.28}, "diesel": {"eta_ice": 0.33}}


def vehicle(name: str, roles: dict, parameters: dict | None = None,
            thresholds: dict | None = None) -> dict:
    """A vehicle's configuration, complete: what is given over the defaults.

    ``roles`` maps a role to ``{"entity": ..., "map"?: ..., "measured_at"?: ...}``.
    Fuel-dependent defaults follow the fuel given; an explicit value always
    wins. Refuses what the requirements refuse (FZG-02, FZG-05).
    """
    for role, spec in roles.items():
        if role not in VEHICLE_ROLES:
            raise ValueError(f"unknown vehicle role {role!r}")
        if not isinstance(spec, dict) or not spec.get("entity"):
            raise ValueError(f"role {role!r} names no entity")
        if role in DOMAIN_STATES:
            positive = DOMAIN_STATES[role]
            m = spec.get("map") or {}
            if set(m) - {positive}:
                raise ValueError(f"role {role!r} maps states other than {positive!r}")
        elif "map" in spec:
            raise ValueError(f"role {role!r} is not enumerated and takes no map")
    if not any(r in MOVEMENT_ROLES for r in roles):
        raise ValueError(f"a vehicle needs a movement role: one of {MOVEMENT_ROLES}")
    params = dict(DEFAULT_PARAMETERS)
    given = parameters or {}
    fuel = given.get("fuel", params["fuel"])
    if fuel is not None and fuel not in FUELS:
        raise ValueError(f"fuel must be one of {FUELS}, not {fuel!r}")
    if fuel:
        params.update(FUEL_PARAMETERS[fuel])
    unknown = set(given) - set(params)
    if unknown:
        raise ValueError(f"unknown parameters: {sorted(unknown)}")
    params.update(given)
    thr = dict(DEFAULT_THRESHOLDS)
    if fuel:
        thr.update(FUEL_THRESHOLDS[fuel])
    given_thr = thresholds or {}
    unknown = set(given_thr) - set(thr)
    if unknown:
        raise ValueError(f"unknown thresholds: {sorted(unknown)}")
    thr.update(given_thr)
    return {"name": name, "roles": deepcopy(roles), "parameters": params, "thresholds": thr}


def chargepoint(name: str, latitude: float, longitude: float, radius_m: float,
                tariffs: list[dict], meter: dict | None = None) -> dict:
    """A charge point's configuration (LAD-01, LAD-02)."""
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    if meter is not None and (not isinstance(meter, dict) or not meter.get("entity")):
        raise ValueError("meter names no entity")
    for t in tariffs:
        date.fromisoformat(t["from"])
        if t["eur_per_kwh"] < 0:
            raise ValueError("a tariff is not negative")
    return {"name": name, "latitude": float(latitude), "longitude": float(longitude),
            "radius_m": float(radius_m), "meter": deepcopy(meter), "tariffs": deepcopy(tariffs)}


def missing(config: dict) -> list[str]:
    """What an enabled derivation would need and the vehicle does not set.

    The configuration never refuses an incomplete vehicle (ADR-0007); this
    names what will come out flagged or empty, so the flow can say so.
    """
    out = []
    roles, p = config["roles"], config["parameters"]
    if "fuel_level" in roles and p["tank_capacity_l"] is None:
        out.append("tank_capacity_l: fuel_level needs it to convert % and to count tank fills")
    if "fuel_level" in roles and p["fuel"] is None:
        out.append("fuel: consumption metrics need the heating value")
    if "soc" in roles and p["battery_net_kwh"] is None:
        out.append("battery_net_kwh: battery-side energy and charging loss need it")
    if "fuel_level" in roles and p["fuel_level_resolution_l"] is None:
        out.append("fuel_level_resolution_l: the consumption error threshold needs it")
    return out


def tariff_at(tariffs: list[dict], t: str) -> dict | None:
    """The tariff valid at time ``t`` (ADR-0007, point 2; LAD-02, VER-07).

    The entry with the greatest ``from`` not after ``t``'s date; among
    equal ``from``, the one appended last — which is how a wrongly entered
    tariff is corrected. ``None`` before the first tariff.
    """
    day = clock.parse(t).date()
    best = None
    for entry in tariffs:
        start = date.fromisoformat(entry["from"])
        if start <= day and (best is None or start >= date.fromisoformat(best["from"])):
            best = entry
    return best


def domain_state(role: str, value: str, mapping: dict) -> str | None:
    """The domain state a source value means, or ``None`` to hold (FZG-05)."""
    positive = DOMAIN_STATES[role]
    if value in ("unavailable", "unknown"):
        return None
    if value in (mapping.get(positive) or []):
        return positive
    if value in (mapping.get("not") or []):
        return f"not_{positive}"
    return None


def is_chargepoint(config: dict) -> bool:
    return "tariffs" in config


__all__ = [
    "CHARGEPOINT_ROLES", "DEFAULT_PARAMETERS", "DEFAULT_THRESHOLDS", "DOMAIN_STATES",
    "FUELS", "MOVEMENT_ROLES", "VEHICLE_ROLES", "chargepoint", "domain_state",
    "is_chargepoint", "missing", "tariff_at", "vehicle",
]
