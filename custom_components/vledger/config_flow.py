# SPDX-License-Identifier: BSD-3-Clause
"""The config flow and the options flow (ADR-0008, point 4).

A vehicle in four steps — name, roles, mapping, parameters — and a charge
point in one. The options flow is a menu over the same steps, pre-filled.
What a step collects is exactly the configuration object the library
defines (``vledger.config``), so the ``config`` line and the options can
never drift.
"""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from vledger import config as vconfig

from .const import (
    DATA_KIND,
    DATA_SUBJECT,
    DOMAIN,
    KIND_CHARGEPOINT,
    KIND_VEHICLE,
    OPT_BASE_PATH,
    OPT_NOTIFY_TARGET,
)

# Which Home Assistant domains fit each role's entity selector.
_ROLE_DOMAINS: dict[str, list[str]] = {
    "odometer": ["sensor"], "position": ["device_tracker", "sensor"],
    "position_latitude": ["sensor"], "position_longitude": ["sensor"],
    "trip_distance": ["sensor"], "fuel_level": ["sensor"], "soc": ["sensor"],
    "charging_state": ["sensor", "binary_sensor"], "plug_state": ["sensor", "binary_sensor"],
    "ignition": ["sensor", "binary_sensor"], "outside_temperature": ["sensor"],
    "fuel_price": ["sensor"], "engine": ["sensor", "binary_sensor"],
    "lock": ["lock", "sensor", "binary_sensor"], "in_use": ["sensor", "binary_sensor"],
    "fuel_flap": ["sensor", "binary_sensor"],
}

_THRESHOLD_UNITS = {
    "t_still_s": "s", "refuel_threshold_l": "L", "t_settle_s": "s",
    "charging_threshold_pct": "%", "matching_tolerance_s": "s", "plausibility_pct": "%",
    "heartbeat_s": "s", "outage_s": "s", "rolling_period_d": "d",
    "consumption_error_pct": "%", "heating_value_kwh_per_l": "kWh/L",
    "beta_per_k": "1/K", "temperature_tau_s": "s",
}


def _number(unit: str | None = None, step: float | str = "any", minimum: float = 0) -> selector.NumberSelector:
    cfg: dict[str, Any] = {"mode": "box", "step": step, "min": minimum}
    if unit:
        cfg["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**cfg))


def _entity(domains: list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains))


def _roles_schema() -> vol.Schema:
    return vol.Schema({vol.Optional(role): _entity(domains) for role, domains in _ROLE_DOMAINS.items()})


def _parameters_schema() -> vol.Schema:
    return vol.Schema({
        vol.Optional("fuel"): selector.SelectSelector(selector.SelectSelectorConfig(
            options=list(vconfig.FUELS), translation_key="fuel")),
        vol.Optional("tank_capacity_l"): _number("L"),
        vol.Optional("battery_net_kwh"): _number("kWh"),
    })


def _all_parameters_schema() -> vol.Schema:
    return vol.Schema({
        vol.Optional("fuel"): selector.SelectSelector(selector.SelectSelectorConfig(
            options=list(vconfig.FUELS), translation_key="fuel")),
        vol.Optional("tank_capacity_l"): _number("L"),
        vol.Optional("battery_net_kwh"): _number("kWh"),
        vol.Optional("fuel_level_resolution_l"): _number("L"),
        vol.Optional("charging_loss_factor"): _number(minimum=1),
        vol.Optional("eta_el"): _number(),
        vol.Optional("eta_ice"): _number(),
        vol.Optional("charge_cycles_start"): _number(),
        vol.Optional("tank_fills_start"): _number(),
    })


def _thresholds_schema() -> vol.Schema:
    return vol.Schema({vol.Required(k): _number(u) for k, u in _THRESHOLD_UNITS.items()})


def _chargepoint_schema() -> vol.Schema:
    return vol.Schema({
        vol.Required("name"): selector.TextSelector(),
        vol.Required("location"): selector.LocationSelector(selector.LocationSelectorConfig(radius=True)),
        vol.Optional("meter"): _entity(["sensor"]),
        vol.Required("eur_per_kwh"): _number("€/kWh"),
        vol.Required("from"): selector.DateSelector(),
    })


def _clean(values: dict) -> dict:
    """Drop what the form left empty."""
    return {k: v for k, v in values.items() if v not in (None, "")}


def _mapping_options(hass, entity_id: str) -> list[str]:
    """What the source can say: its enum options, on/off, or what it says now."""
    state = hass.states.get(entity_id)
    if state is None:
        return []
    if entity_id.startswith("binary_sensor."):
        return ["on", "off"]
    if entity_id.startswith("lock."):
        return ["locked", "unlocked", "locking", "unlocking", "open", "opening", "jammed"]
    options = state.attributes.get("options")
    if options:
        return [str(o) for o in options]
    return [state.state]


def _mapping_schema(hass, roles: dict, current: dict | None = None) -> vol.Schema:
    fields = {}
    for role in vconfig.DOMAIN_STATES:
        if role not in roles:
            continue
        options = _mapping_options(hass, roles[role]["entity"])
        chosen = ((current or {}).get(role, {}).get("map") or {}).get(vconfig.DOMAIN_STATES[role], [])
        options = sorted(set(options) | set(chosen))
        fields[vol.Optional(role, default=list(chosen))] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=options, multiple=True, custom_value=True))
    return vol.Schema(fields)


def _apply_mapping(roles: dict, values: dict) -> dict:
    out = {r: dict(s) for r, s in roles.items()}
    for role, positive in vconfig.DOMAIN_STATES.items():
        if role in out:
            out[role]["map"] = {positive: list(values.get(role) or [])}
    return out


def _needs_tank_capacity(hass, roles: dict) -> bool:
    spec = roles.get("fuel_level")
    if not spec:
        return False
    state = hass.states.get(spec["entity"])
    return bool(state and state.attributes.get("unit_of_measurement") == "%")


class VledgerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._name = ""
        self._roles: dict = {}

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[KIND_VEHICLE, KIND_CHARGEPOINT])

    # --- vehicle, four steps ----------------------------------------------

    async def async_step_vehicle(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            self._name = user_input["name"]
            return await self.async_step_roles()
        return self.async_show_form(step_id="vehicle", data_schema=vol.Schema(
            {vol.Required("name"): selector.TextSelector()}))

    async def async_step_roles(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            roles = {role: {"entity": e} for role, e in _clean(user_input).items()}
            if not any(r in vconfig.MOVEMENT_ROLES for r in roles):
                errors["base"] = "no_movement_role"
            else:
                self._roles = roles
                if any(r in vconfig.DOMAIN_STATES for r in roles):
                    return await self.async_step_mapping()
                return await self.async_step_parameters()
        return self.async_show_form(step_id="roles", data_schema=_roles_schema(), errors=errors,
                                    description_placeholders={"movement": ", ".join(vconfig.MOVEMENT_ROLES)})

    async def async_step_mapping(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            self._roles = _apply_mapping(self._roles, user_input)
            return await self.async_step_parameters()
        return self.async_show_form(step_id="mapping", data_schema=_mapping_schema(self.hass, self._roles))

    async def async_step_parameters(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            params = _clean(user_input)
            if _needs_tank_capacity(self.hass, self._roles) and "tank_capacity_l" not in params:
                errors["tank_capacity_l"] = "tank_capacity_required"
            else:
                subject = uuid.uuid4().hex
                await self.async_set_unique_id(subject)
                options = vconfig.vehicle(self._name, self._roles, params)
                options[OPT_BASE_PATH] = self.hass.config.path(DOMAIN)
                return self.async_create_entry(
                    title=self._name, data={DATA_KIND: KIND_VEHICLE, DATA_SUBJECT: subject},
                    options=options)
        return self.async_show_form(step_id="parameters", data_schema=_parameters_schema(), errors=errors)

    # --- charge point, one step -------------------------------------------

    async def async_step_chargepoint(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            loc = user_input["location"]
            subject = uuid.uuid4().hex
            await self.async_set_unique_id(subject)
            options = vconfig.chargepoint(
                user_input["name"], loc["latitude"], loc["longitude"], loc.get("radius") or 50,
                tariffs=[{"from": user_input["from"], "eur_per_kwh": user_input["eur_per_kwh"]}],
                meter={"entity": user_input["meter"]} if user_input.get("meter") else None)
            options[OPT_BASE_PATH] = self.hass.config.path(DOMAIN)
            return self.async_create_entry(
                title=user_input["name"], data={DATA_KIND: KIND_CHARGEPOINT, DATA_SUBJECT: subject},
                options=options)
        schema = self.add_suggested_values_to_schema(_chargepoint_schema(), {
            "location": {"latitude": self.hass.config.latitude,
                         "longitude": self.hass.config.longitude, "radius": 50}})
        return self.async_show_form(step_id="chargepoint", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> VledgerOptionsFlow:
        return VledgerOptionsFlow()


class VledgerOptionsFlow(OptionsFlow):
    """A menu over the same steps, pre-filled; submitting one updates the
    options, and the integration answers by reloading the entry."""

    @property
    def _opts(self) -> dict:
        return dict(self.config_entry.options)

    def _finish(self, options: dict) -> ConfigFlowResult:
        return self.async_create_entry(data=options)

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if self.config_entry.data[DATA_KIND] == KIND_VEHICLE:
            menu = ["roles", "mapping", "parameters", "thresholds", "notifications", "storage"]
        else:
            menu = ["meter", "add_tariff", "storage"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_roles(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        opts = self._opts
        if user_input is not None:
            old = opts["roles"]
            roles = {}
            for role, entity in _clean(user_input).items():
                roles[role] = dict(old.get(role, {}), entity=entity)
            if not any(r in vconfig.MOVEMENT_ROLES for r in roles):
                errors["base"] = "no_movement_role"
            else:
                opts.update(vconfig.vehicle(opts["name"], roles, opts["parameters"], opts["thresholds"]))
                return self._finish(opts)
        schema = self.add_suggested_values_to_schema(
            _roles_schema(), {r: s["entity"] for r, s in opts["roles"].items()})
        return self.async_show_form(step_id="roles", data_schema=schema, errors=errors,
                                    description_placeholders={"movement": ", ".join(vconfig.MOVEMENT_ROLES)})

    async def async_step_mapping(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts
        if user_input is not None:
            opts["roles"] = _apply_mapping(opts["roles"], user_input)
            return self._finish(opts)
        return self.async_show_form(step_id="mapping",
                                    data_schema=_mapping_schema(self.hass, opts["roles"], opts["roles"]))

    async def async_step_parameters(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts
        if user_input is not None:
            given = _clean(user_input)
            params = {k: given.get(k) for k in vconfig.DEFAULT_PARAMETERS}
            opts.update(vconfig.vehicle(opts["name"], opts["roles"], params, opts["thresholds"]))
            return self._finish(opts)
        schema = self.add_suggested_values_to_schema(
            _all_parameters_schema(), _clean(opts["parameters"]))
        return self.async_show_form(step_id="parameters", data_schema=schema)

    async def async_step_thresholds(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts
        if user_input is not None:
            opts.update(vconfig.vehicle(opts["name"], opts["roles"], opts["parameters"], user_input))
            return self._finish(opts)
        schema = self.add_suggested_values_to_schema(_thresholds_schema(), opts["thresholds"])
        return self.async_show_form(step_id="thresholds", data_schema=schema)

    async def async_step_notifications(self, user_input=None) -> ConfigFlowResult:
        """Where a vehicle's notifications go: one ``notify.*`` action, or
        none. An option outside the ``config`` line (ADR-0020, point 3)."""
        opts = self._opts
        if user_input is not None:
            target = _clean(user_input).get(OPT_NOTIFY_TARGET)
            if target:
                opts[OPT_NOTIFY_TARGET] = target
            else:
                opts.pop(OPT_NOTIFY_TARGET, None)
            return self._finish(opts)
        # notify.send_message addresses notify entities and takes no title
        # or data; every other notify action takes title, message and data.
        targets = sorted(f"notify.{name}" for name in self.hass.services.async_services_for_domain("notify")
                         if name != "send_message")
        schema = self.add_suggested_values_to_schema(
            vol.Schema({vol.Optional(OPT_NOTIFY_TARGET): selector.SelectSelector(
                selector.SelectSelectorConfig(options=targets, mode=selector.SelectSelectorMode.DROPDOWN))}),
            {OPT_NOTIFY_TARGET: opts.get(OPT_NOTIFY_TARGET)})
        return self.async_show_form(step_id="notifications", data_schema=schema)

    async def async_step_meter(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts
        if user_input is not None:
            entity = _clean(user_input).get("meter")
            opts["meter"] = {"entity": entity} if entity else None
            return self._finish(opts)
        schema = self.add_suggested_values_to_schema(
            vol.Schema({vol.Optional("meter"): _entity(["sensor"])}),
            {"meter": (opts.get("meter") or {}).get("entity")})
        return self.async_show_form(step_id="meter", data_schema=schema)

    async def async_step_add_tariff(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts
        if user_input is not None:
            tariffs = list(opts["tariffs"]) + [
                {"from": user_input["from"], "eur_per_kwh": user_input["eur_per_kwh"]}]
            opts.update(vconfig.chargepoint(opts["name"], opts["latitude"], opts["longitude"],
                                            opts["radius_m"], tariffs, opts.get("meter")))
            return self._finish(opts)
        return self.async_show_form(step_id="add_tariff", data_schema=vol.Schema({
            vol.Required("eur_per_kwh"): _number("€/kWh"),
            vol.Required("from"): selector.DateSelector(),
        }), description_placeholders={"count": str(len(opts["tariffs"]))})

    async def async_step_storage(self, user_input=None) -> ConfigFlowResult:
        opts = self._opts
        if user_input is not None:
            opts[OPT_BASE_PATH] = user_input[OPT_BASE_PATH]
            return self._finish(opts)
        schema = self.add_suggested_values_to_schema(
            vol.Schema({vol.Required(OPT_BASE_PATH): selector.TextSelector()}),
            {OPT_BASE_PATH: opts.get(OPT_BASE_PATH)})
        return self.async_show_form(step_id="storage", data_schema=schema)
