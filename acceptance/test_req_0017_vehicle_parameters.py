# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0017: vehicle parameters are the tank capacity (L), the net battery
capacity (kWh), the fuel type, and the starting values for charge cycles and
tank-fill equivalents before capture began (default 0); a parameter is
mandatory only when an enabled derivation needs it, e.g. the tank capacity
when the fuel level is reported in %.

Derived from the requirement and from the user guide's *In Home Assistant*: a
vehicle's fourth step, **Parameters**, asks for "Fuel, tank capacity, net
battery capacity — only what a derivation needs; the tank capacity is
required when the fuel level is reported in percent. Everything else keeps
its default"; the entry's *Configure* holds "the remaining parameters"; the
glossary's *Vehicle parameters* names the two counters' starting values among
them; **Charge cycles in total** and **Tank fills in total** include "the
starting values configured"; and docs/l0-format.md's `config` line carries
the vehicle's `parameters`.

Home Assistant starts with nothing of the ledger configured, and the tests do
what the frontend does: they call the config and options flow endpoints over
HTTP and choose every field by the label Home Assistant serves the frontend,
never by an internal key. They import nothing of ``vledger`` or of the
integration."""

import asyncio
import json
import pathlib

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from browser import open_frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

DOMAIN = "vledger"

pytestmark = [pytest.mark.req("REQ-0017")]


@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client):
    """A Home Assistant with its frontend's API and nothing configured for the
    ledger. The configuration directory is a temporary one, so the raw log
    lands there."""
    return UI(hass, await open_frontend(hass, tmp_path, hass_client))


class UI:
    """What a person's browser does: start a flow, read the form it is shown
    with its labels, fill it in by label, submit."""

    def __init__(self, hass, client):
        self.hass, self.client = hass, client

    async def labels(self, category):
        return await async_get_translations(self.hass, "en", category, [DOMAIN])

    async def start(self, path, handler):
        resp = await self.client.post(path, json={"handler": handler})
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def submit(self, path, flow_id, data):
        resp = await self.client.post(f"{path}/{flow_id}", json=data)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def config_flow(self):
        return Flow(self, "/api/config/config_entries/flow", "config",
                    await self.start("/api/config/config_entries/flow", DOMAIN),
                    await self.labels("config"))

    async def options_flow(self, entry_id):
        return Flow(self, "/api/config/config_entries/options/flow", "options",
                    await self.start("/api/config/config_entries/options/flow", entry_id),
                    await self.labels("options"))

    async def entries(self):
        resp = await self.client.get(f"/api/config/config_entries/entry?domain={DOMAIN}")
        assert resp.status == 200, await resp.text()
        return await resp.json()


class Flow:
    def __init__(self, ui, path, category, step, labels):
        self.ui, self.path, self.category, self.step, self.labels = ui, path, category, step, labels

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, self.category, *parts)))

    @property
    def title(self):
        return self._label("step", self.step["step_id"], "title")

    def options(self):
        assert self.step["type"] == "menu", self.step
        return {self._label("step", self.step["step_id"], "menu_options", o): o
                for o in self.step["menu_options"]}

    def fields(self):
        assert self.step["type"] == "form", self.step
        return {self._label("step", self.step["step_id"], "data", f["name"]): f
                for f in self.step["data_schema"]}

    def shown(self, label):
        """What a field shows when the form opens: its suggested or default value."""
        field = self.fields()[label]
        return field.get("description", {}).get("suggested_value", field.get("default"))

    def errors(self):
        """The errors the form shows, by the label of the field each stands
        under, as the messages the frontend shows."""
        fields = {f["name"]: label for label, f in self.fields().items()}
        return {fields.get(name, name): self._label("error", key)
                for name, key in (self.step.get("errors") or {}).items()}

    async def choose(self, label):
        options = self.options()
        assert label in options, f"{label!r} not among {sorted(options, key=str)}"
        self.step = await self.ui.submit(self.path, self.step["flow_id"],
                                         {"next_step_id": options[label]})
        return self

    async def fill(self, values):
        """Fill in by label and submit; a field left alone is sent with what
        the form showed in it, and an empty one is not sent."""
        fields = self.fields()
        missing = set(values) - set(fields)
        assert not missing, f"{sorted(missing)} not among {sorted(fields, key=str)}"
        data = {}
        for label, field in fields.items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        self.step = await self.ui.submit(self.path, self.step["flow_id"], data)
        return self


def odometer(hass, entity="sensor.car_odometer"):
    hass.states.async_set(entity, "12345", {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})
    return entity


def fuel_level(hass, unit, entity="sensor.car_fuel_level"):
    hass.states.async_set(entity, "50" if unit == "%" else "35",
                          {"unit_of_measurement": unit, "state_class": "measurement"})
    return entity


async def to_parameters(ui, roles, name="Family car"):
    """Add integration → Vehicle Ledger → A vehicle, up to its Parameters step."""
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    assert flow.title == "Roles"
    await flow.fill(roles)
    assert flow.title == "Parameters", flow.step
    return flow


async def settled(ui, configs=0):
    """Wait until the raw log holds more than `configs` config lines, as an
    entry started or reloaded writes one (docs/l0-format.md), and every
    vehicle's Capture status shows `running`."""
    status = None
    for _ in range(200):
        await ui.hass.async_block_till_done()
        status = [s.state for s in ui.hass.states.async_all("sensor")
                  if s.attributes.get("friendly_name", "").endswith("Capture status")]
        if status and set(status) == {"running"} and len(config_lines(ui)) > configs:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Capture status {status}, {len(config_lines(ui))} config lines")


def entity(ui, name):
    """The state of the vehicle's entity shown under that name."""
    found = [s for s in ui.hass.states.async_all("sensor")
             if s.attributes.get("friendly_name", "").endswith(" " + name)]
    assert len(found) == 1, f"{name!r}: {[s.entity_id for s in found]}"
    return found[0].state


async def value(ui, name, expected):
    """The number the vehicle's entity shown under that name shows, waited
    for until it is the one expected: right after a start or reload it may
    still be unavailable, or show the figure from before the reload. One
    that never becomes the number expected is returned as it last stood."""
    state = None
    for _ in range(200):
        await ui.hass.async_block_till_done()
        state = entity(ui, name)
        if state not in ("unavailable", "unknown") and float(state) == expected:
            break
        await asyncio.sleep(0.05)
    return state if state in ("unavailable", "unknown") else float(state)


def config_lines(ui):
    """The `config` lines of the vehicle's raw log (docs/l0-format.md)."""
    lines = []
    for f in sorted(pathlib.Path(ui.hass.config.path("vledger")).glob("vehicle-*/l0/*.jsonl")):
        lines += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [line for line in lines if line.get("kind") == "config"]


def parameters_in_l0(ui):
    """The `parameters` of the latest `config` line in the vehicle's raw log."""
    configs = config_lines(ui)
    assert configs, "no config line in the raw log"
    return configs[-1]["config"]["parameters"]


async def test_the_parameters_step_asks_for_fuel_tank_and_battery(ui):
    """The Parameters step asks for the fuel, the tank capacity in litres and
    the net battery capacity in kWh, and requires none of them for a vehicle
    whose derivations need none: an odometer alone sets up with all three
    left empty."""
    flow = await to_parameters(ui, {"Odometer": odometer(ui.hass)})
    fields = flow.fields()
    assert {"Fuel", "Tank capacity", "Net battery capacity"} <= set(fields), sorted(fields, key=str)
    assert fields["Tank capacity"]["selector"]["number"]["unit_of_measurement"] == "L"
    assert fields["Net battery capacity"]["selector"]["number"]["unit_of_measurement"] == "kWh"
    assert {"petrol", "diesel"} <= set(fields["Fuel"]["selector"]["select"]["options"])
    assert not any(fields[label].get("required") for label in
                   ("Fuel", "Tank capacity", "Net battery capacity"))

    await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()
    assert [e["state"] for e in await ui.entries()] == ["loaded"]


async def test_the_tank_capacity_is_required_when_the_fuel_level_is_in_percent(ui):
    """A fuel level reported in % needs the tank capacity to become litres:
    the step refuses to finish without it, under that field, and with it the
    vehicle is set up and its raw log records the capacity entered."""
    flow = await to_parameters(ui, {"Odometer": odometer(ui.hass),
                                    "Fuel level": fuel_level(ui.hass, "%")})
    await flow.fill({"Fuel": "petrol"})
    assert flow.step["type"] == "form" and flow.title == "Parameters", flow.step
    errors = flow.errors()
    assert list(errors) == ["Tank capacity"], errors
    assert "tank capacity" in errors["Tank capacity"].lower(), errors

    await flow.fill({"Fuel": "petrol", "Tank capacity": 50})
    assert flow.step["type"] == "create_entry", flow.step
    await settled(ui)
    assert [e["state"] for e in await ui.entries()] == ["loaded"]
    parameters = parameters_in_l0(ui)
    assert parameters["tank_capacity_l"] == 50
    assert parameters["fuel"] == "petrol"


async def test_a_fuel_level_in_litres_needs_no_tank_capacity(ui):
    """The same vehicle reporting its fuel level in litres needs no tank
    capacity for it: the step finishes with the capacity left empty."""
    flow = await to_parameters(ui, {"Odometer": odometer(ui.hass),
                                    "Fuel level": fuel_level(ui.hass, "L")})
    await flow.fill({"Fuel": "petrol"})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()
    assert [e["state"] for e in await ui.entries()] == ["loaded"]


async def test_the_parameters_entered_are_the_vehicles(ui):
    """Fuel, tank capacity and net battery capacity entered at setup are the
    vehicle's: the raw log's configuration carries them, and Configure →
    Parameters shows them again."""
    flow = await to_parameters(ui, {"Odometer": odometer(ui.hass)}, name="Hybrid")
    await flow.fill({"Fuel": "diesel", "Tank capacity": 42, "Net battery capacity": 10.5})
    assert flow.step["type"] == "create_entry", flow.step
    await settled(ui)

    parameters = parameters_in_l0(ui)
    assert (parameters["fuel"], parameters["tank_capacity_l"], parameters["battery_net_kwh"]) \
        == ("diesel", 42, 10.5)

    [entry] = await ui.entries()
    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Parameters")
    assert (flow.shown("Fuel"), flow.shown("Tank capacity"), flow.shown("Net battery capacity")) \
        == ("diesel", 42, 10.5)


async def test_the_starting_values_are_zero_unless_configured(ui):
    """The starting values for charge cycles and tank fills before capture
    began are among the parameters under Configure, 0 unless set; the
    vehicle's totals start from them: 0 at first, and the values set once
    they are saved."""
    flow = await to_parameters(ui, {"Odometer": odometer(ui.hass)})
    await flow.fill({"Fuel": "petrol", "Tank capacity": 50, "Net battery capacity": 10.5})
    assert flow.step["type"] == "create_entry", flow.step
    await settled(ui)
    [entry] = await ui.entries()

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Parameters")
    assert flow.shown("Charge cycles before capture began") == 0
    assert flow.shown("Tank fills before capture began") == 0
    parameters = parameters_in_l0(ui)
    assert (parameters["charge_cycles_start"], parameters["tank_fills_start"]) == (0, 0)
    assert await value(ui, "Charge cycles in total", 0) == 0
    assert await value(ui, "Tank fills in total", 0) == 0

    before = len(config_lines(ui))
    await flow.fill({"Charge cycles before capture began": 120,
                     "Tank fills before capture began": 35})
    assert flow.step["type"] == "create_entry", flow.step
    await settled(ui, before)

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Parameters")
    assert flow.shown("Charge cycles before capture began") == 120
    assert flow.shown("Tank fills before capture began") == 35
    parameters = parameters_in_l0(ui)
    assert (parameters["charge_cycles_start"], parameters["tank_fills_start"]) == (120, 35)
    assert await value(ui, "Charge cycles in total", 120) == 120
    assert await value(ui, "Tank fills in total", 35) == 35


async def test_a_starting_value_needs_no_other_parameter(ui):
    """The starting values are optional in the options too: Parameters saved
    with only a changed tank capacity finishes, the counters keeping 0."""
    flow = await to_parameters(ui, {"Odometer": odometer(ui.hass)})
    await flow.fill({})
    await settled(ui)
    [entry] = await ui.entries()

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Parameters")
    before = len(config_lines(ui))
    await flow.fill({"Tank capacity": 60})
    assert flow.step["type"] == "create_entry", flow.step
    await settled(ui, before)
    parameters = parameters_in_l0(ui)
    assert parameters["tank_capacity_l"] == 60
    assert (parameters["charge_cycles_start"], parameters["tank_fills_start"]) == (0, 0)
