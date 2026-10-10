# SPDX-License-Identifier: BSD-3-Clause
"""The receipt actions (HAI-03, ADR-0015) and the export action (ADR-0017):
registered once for the domain, each naming its vehicle by config entry.
The receipt actions' fields are the ``vledger receipt`` verbs' options
under the same names (ADR-0013, point 6), and they answer with the line
they wrote; the work is the vehicle's receipt desk's. The export action's
fields are ``vledger export``'s, and it answers with the file it wrote; the
work is :mod:`exporter`'s."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from vledger import export, l1

from .const import (
    DATA_KIND,
    DOMAIN,
    KIND_VEHICLE,
    SERVICE_ADD_CHARGING,
    SERVICE_ADD_REFUELLING,
    SERVICE_CANCEL,
    SERVICE_EXPORT,
)
from .exporter import async_export
from .receipt_desk import CHARGING, REFUELLING, ReceiptDesk

_amount = vol.Coerce(float)

_WHEN = {
    vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional("anchor"): cv.datetime,
    vol.Optional("from_candidate"): cv.datetime,
    vol.Optional("total_price"): _amount,
    vol.Optional("place"): cv.string,
    vol.Optional("note"): cv.string,
    vol.Optional("replaces"): cv.string,
}

REFUELLING_SCHEMA = vol.Schema({
    **_WHEN,
    vol.Required("quantity_l"): _amount,
    vol.Required("full"): cv.boolean,
    vol.Optional("unit_price"): _amount,
    vol.Optional("fuel"): cv.string,
})

CHARGING_SCHEMA = vol.Schema({
    **_WHEN,
    vol.Required("energy_kwh"): _amount,
    vol.Required("total_price"): _amount,
    vol.Optional("provider"): cv.string,
})

CANCEL_SCHEMA = vol.Schema({
    vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required("receipt"): cv.string,
    vol.Optional("note"): cv.string,
})

EXPORT_SCHEMA = vol.Schema({
    vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required("format"): vol.In(export.FORMATS),
    vol.Optional("kind"): vol.In(list(l1.FILES)),
    vol.Optional("since"): cv.datetime,
    vol.Optional("until"): cv.datetime,
    vol.Optional("filename"): cv.string,
})


def _error(key: str, entry_id: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key,
                                  translation_placeholders={"entry": entry_id})


def vehicle_of(hass: HomeAssistant, entry_id: str):
    """A loaded vehicle entry; refused for anything else."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        raise _error("entry_not_loaded", entry_id)
    if entry.data.get(DATA_KIND) != KIND_VEHICLE:
        raise _error("not_a_vehicle", entry_id)
    return entry


def desk_of(hass: HomeAssistant, entry_id: str) -> ReceiptDesk:
    """The receipt desk of a loaded vehicle entry."""
    return vehicle_of(hass, entry_id).runtime_data.desk


def async_register(hass: HomeAssistant) -> None:
    def adder(kind: str):
        async def handle(call: ServiceCall) -> ServiceResponse:
            fields = dict(call.data)
            desk = desk_of(hass, fields.pop(ATTR_CONFIG_ENTRY_ID))
            return await desk.async_add(kind, **fields)
        return handle

    async def cancel(call: ServiceCall) -> ServiceResponse:
        desk = desk_of(hass, call.data[ATTR_CONFIG_ENTRY_ID])
        return await desk.async_cancel(call.data["receipt"], call.data.get("note"))

    async def do_export(call: ServiceCall) -> ServiceResponse:
        fields = dict(call.data)
        entry = vehicle_of(hass, fields.pop(ATTR_CONFIG_ENTRY_ID))
        return await async_export(hass, entry, fmt=fields.pop("format"), **fields)

    for name, handler, schema in (
            (SERVICE_ADD_REFUELLING, adder(REFUELLING), REFUELLING_SCHEMA),
            (SERVICE_ADD_CHARGING, adder(CHARGING), CHARGING_SCHEMA),
            (SERVICE_CANCEL, cancel, CANCEL_SCHEMA),
            (SERVICE_EXPORT, do_export, EXPORT_SCHEMA)):
        hass.services.async_register(DOMAIN, name, handler, schema=schema,
                                     supports_response=SupportsResponse.OPTIONAL)
