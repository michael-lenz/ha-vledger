# SPDX-License-Identifier: BSD-3-Clause
"""The flows, step by step, refusals included."""

from custom_components.vledger.const import (
    DATA_KIND,
    DATA_SUBJECT,
    DOMAIN,
    OPT_BASE_PATH,
)
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType


async def _start(hass, kind):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": kind})


async def test_a_vehicle_in_four_steps(hass):
    hass.states.async_set("sensor.volvo_fuel", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.volvo_charging", "Idle", {"options": ["Idle", "Charging", "Done"]})
    result = await _start(hass, "vehicle")
    assert result["step_id"] == "vehicle"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Volvo"})
    assert result["step_id"] == "roles"

    # No movement role: refused, same step again.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fuel_level": "sensor.volvo_fuel"})
    assert result["step_id"] == "roles" and result["errors"] == {"base": "no_movement_role"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "odometer": "sensor.volvo_odometer", "fuel_level": "sensor.volvo_fuel",
        "charging_state": "sensor.volvo_charging"})
    assert result["step_id"] == "mapping"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"charging_state": ["Charging"]})
    assert result["step_id"] == "parameters"

    # Fuel level in %: the tank capacity is demanded.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"fuel": "petrol"})
    assert result["step_id"] == "parameters"
    assert result["errors"] == {"tank_capacity_l": "tank_capacity_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fuel": "petrol", "tank_capacity_l": 71})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[DATA_KIND] == "vehicle" and len(entry.data[DATA_SUBJECT]) == 32
    assert entry.unique_id == entry.data[DATA_SUBJECT]
    opts = entry.options
    assert opts["name"] == "Volvo"
    assert opts["roles"]["charging_state"] == {"entity": "sensor.volvo_charging", "map": {"charging": ["Charging"]}}
    assert opts["parameters"]["tank_capacity_l"] == 71 and opts["parameters"]["eta_ice"] == 0.28
    assert opts["thresholds"]["t_still_s"] == 1800
    assert opts[OPT_BASE_PATH].endswith("/vledger")


async def test_a_vehicle_without_enumerated_roles_skips_the_mapping(hass):
    result = await _start(hass, "vehicle")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Bike"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"position": "device_tracker.bike"})
    assert result["step_id"] == "parameters"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["parameters"]["fuel"] is None


async def test_a_chargepoint_in_one_step(hass):
    result = await _start(hass, "chargepoint")
    assert result["step_id"] == "chargepoint"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "Home", "location": {"latitude": 48.1, "longitude": 11.5, "radius": 40},
        "meter": "sensor.wallbox_energy", "eur_per_kwh": 0.30, "from": "2026-01-01"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    opts = result["result"].options
    assert opts["radius_m"] == 40 and opts["meter"] == {"entity": "sensor.wallbox_energy"}
    assert opts["tariffs"] == [{"from": "2026-01-01", "eur_per_kwh": 0.30}]
    assert result["result"].data[DATA_KIND] == "chargepoint"


async def test_options_add_a_tariff_appends(hass):
    result = await _start(hass, "chargepoint")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "Home", "location": {"latitude": 48.1, "longitude": 11.5, "radius": 40},
        "eur_per_kwh": 0.30, "from": "2026-01-01"})
    entry = result["result"]          # created and set up by the flow
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_tariff"})
    assert result["step_id"] == "add_tariff"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"eur_per_kwh": 0.34, "from": "2026-07-01"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["tariffs"] == [{"from": "2026-01-01", "eur_per_kwh": 0.30},
                                        {"from": "2026-07-01", "eur_per_kwh": 0.34}]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_thresholds_are_prefilled_and_kept_complete(hass, vehicle_entry):
    hass.states.async_set("sensor.volvo_odometer", "100")
    vehicle_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(vehicle_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "thresholds"})
    assert result["step_id"] == "thresholds"
    suggested = {k.schema: k.description["suggested_value"] for k in result["data_schema"].schema}
    assert suggested["heartbeat_s"] == 600 and suggested["t_still_s"] == 1800
    submitted = dict(suggested, t_still_s=2400)
    result = await hass.config_entries.options.async_configure(result["flow_id"], submitted)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert vehicle_entry.options["thresholds"]["t_still_s"] == 2400
    assert vehicle_entry.options["thresholds"]["heartbeat_s"] == 600
    await hass.config_entries.async_unload(vehicle_entry.entry_id)
