# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0015: source entities are assigned to roles; every role is optional;
at least one movement role (odometer or position) is mandatory.

Derived from the requirement, README.md ("its source entities are assigned to
roles (``odometer``, ``position``, ``fuel_level``, ``soc``,
``charging_state``, …)"; "Every change of a source entity you assign to a role
becomes one record in a raw log"), the user guide's *In Home Assistant* (the
**Roles** step: "Pick the entity that reports each thing the vehicle reports
— odometer, position, fuel level, state of charge, charging state and so on.
Every role is optional; at least one movement role ... is required"; the
entry's *Configure* is "a menu over the same steps, pre-filled") and *Where
the data is* (a stream read with ``vledger l0 read --vehicle ID``), with the
``state`` and ``config`` lines l0-format.md specifies (a state line's ``role``
is "the configured role").

Which roles besides odometer and position count as movement roles is left
out: REQ-0015 names those two, the user guide adds the trip counter, and
ISSUE-0116 holds that question open. Every test here assigns, or withholds,
odometer and position alone.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config flow, options flow and config entry
endpoints over HTTP, choosing every field by the label the frontend shows —
and read the raw log back with the ``vledger`` command in a process of its
own. Nothing of ``vledger`` or of the integration is imported."""

import asyncio
import json
import os
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"
ENTRIES = "/api/config/config_entries/entry"

pytestmark = [pytest.mark.req("REQ-0015")]


# --- what the browser does ---------------------------------------------------

@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client):
    """A Home Assistant with its frontend's API and nothing configured for the
    ledger. The configuration directory is a temporary one, so the raw log
    lands under it, in ``<config>/vledger/``."""
    hass.config.config_dir = str(tmp_path)
    hass.config.latitude, hass.config.longitude = 52.52, 13.40
    assert await async_setup_component(hass, "config", {})
    # The frontend's own package is not part of the test stack; it is stood in
    # for as set up, with nowhere to serve from.
    hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(lambda *_: None, [])
    hass.config.components.add("frontend")
    await hass.async_block_till_done()
    # Home Assistant keeps its router open after start, since an integration
    # set up later in the UI adds paths to it; the test client starts the
    # server before any entry exists, so the router is kept open likewise.
    hass.http.app.router.freeze = lambda: None
    return UI(hass, await hass_client())


class UI:
    def __init__(self, hass, client):
        self.hass, self.client = hass, client

    async def post(self, path, data=None):
        resp = await self.client.post(path, json=data)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def entries(self):
        resp = await self.client.get(f"{ENTRIES}?domain={DOMAIN}")
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def config_flow(self):
        labels = await async_get_translations(self.hass, "en", "config", [DOMAIN])
        return Flow(self, FLOW, "config", await self.post(FLOW, {"handler": DOMAIN}), labels)

    async def options_flow(self, entry_id):
        labels = await async_get_translations(self.hass, "en", "options", [DOMAIN])
        return Flow(self, OPTIONS, "options", await self.post(OPTIONS, {"handler": entry_id}),
                    labels)


class Flow:
    """One flow as the browser shows it: a menu or a form, by its labels."""

    def __init__(self, ui, path, category, step, labels):
        self.ui, self.path, self.category, self.step, self.labels = (
            ui, path, category, step, labels)

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, self.category, "step",
                                         self.step["step_id"], *parts)))

    @property
    def title(self):
        return self._label("title")

    def fields(self):
        assert self.step["type"] == "form", self.step
        return {self._label("data", f["name"]): f for f in self.step["data_schema"]}

    def field(self, name):
        """A field by the name the user guide gives it, whatever its case:
        the label that reads so, or else the one label that reads so and adds
        a parenthesis ("Position (device tracker)")."""
        labels = [lab for lab in self.fields() if lab]
        found = [lab for lab in labels if lab.casefold() == name.casefold()]
        found = found or [lab for lab in labels
                          if lab.casefold().startswith(name.casefold() + " (")]
        assert len(found) == 1, f"{name!r} not among {sorted(self.fields(), key=str)}"
        return found[0]

    def shown(self, label):
        field = self.fields()[label]
        return field.get("description", {}).get("suggested_value", field.get("default"))

    async def choose(self, label):
        assert self.step["type"] == "menu", self.step
        menu = {self._label("menu_options", o): o for o in self.step["menu_options"]}
        assert label in menu, f"{label!r} not among {sorted(menu, key=str)}"
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}",
                                       {"next_step_id": menu[label]})
        return self

    async def fill(self, values):
        """Fill in by label and submit. As in the browser, a field left alone
        is sent with what the form showed in it, and a field emptied (None)
        is not sent."""
        fields = self.fields()
        values = {self.field(k): v for k, v in values.items()}
        data = {}
        for label, field in fields.items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}", data)
        return self

    async def through_mapping_and_parameters(self):
        """Mapping, if shown, ticked as shown; Parameters, with the
        capacities a derivation may ask for."""
        if self.step["type"] == "form" and self.title == "Mapping":
            await self.fill({})
        assert self.step["type"] == "form" and self.title == "Parameters", self.step
        given = {}
        for name, value in (("Tank capacity", 50), ("Net battery capacity", 10.5)):
            if any(lab and lab.casefold() == name.casefold() for lab in self.fields()):
                given[name] = value
        await self.fill(given)
        return self


async def start_vehicle(ui, name):
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    assert flow.title == "Roles", flow.step
    return flow


# --- the source entities -----------------------------------------------------

def odometer(hass, km=12345, entity="sensor.car_odometer"):
    hass.states.async_set(entity, str(km), {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})
    return entity


def position(hass, lat=52.52, lon=13.40, entity="device_tracker.car"):
    hass.states.async_set(entity, "not_home", {"latitude": lat, "longitude": lon,
                                               "gps_accuracy": 10, "source_type": "gps"})
    return entity


def fuel_level(hass, litres=30, entity="sensor.car_fuel_level"):
    hass.states.async_set(entity, str(litres), {"unit_of_measurement": "L",
                                                "device_class": "volume"})
    return entity


def soc(hass, percent=80, entity="sensor.car_soc"):
    hass.states.async_set(entity, str(percent), {"unit_of_measurement": "%",
                                                 "device_class": "battery"})
    return entity


def charging_state(hass, state="off", entity="binary_sensor.car_charging"):
    hass.states.async_set(entity, state, {"device_class": "battery_charging"})
    return entity


# --- what the user reads back ------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def stream(hass):
    """The one vehicle's raw log under ``<config>/vledger/``, as
    ``vledger l0 read`` prints it."""
    base = pathlib.Path(hass.config.path("vledger"))
    dirs = sorted(base.glob("vehicle-*"))
    assert len(dirs) == 1, dirs
    subject = dirs[0].name.removeprefix("vehicle-")
    return [json.loads(x) for x in vledger(base, "l0", "read", "--vehicle", subject).splitlines()
            if x.strip()]


def configured_roles(lines):
    """The roles of the latest complete configuration the stream records."""
    configs = [ln["config"] for ln in lines if ln["kind"] == "config"]
    assert configs, lines
    return configs[-1]["roles"]


def roles_of_states(lines):
    return {(ln["role"], ln["entity"]) for ln in lines if ln["kind"] == "state"}


async def settle(hass):
    """Let the entry come up and capture write what it was handed: its writes
    run off the event loop."""
    for _ in range(10):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


# --- the tests ----------------------------------------------------------------

async def test_the_roles_step_offers_the_roles_and_requires_none_of_them(ui):
    """The Roles step lists odometer, position, fuel level, state of charge
    and charging state, and the form itself marks none of its fields as
    required: every role is optional."""
    flow = await start_vehicle(ui, "Family car")
    for name in ("Odometer", "Position", "Fuel level", "State of charge", "Charging state"):
        flow.field(name)
    required = [lab for lab, f in flow.fields().items() if f.get("required")]
    assert required == [], required


async def test_an_odometer_alone_is_enough(ui):
    """A vehicle with the odometer and no other role is set up and runs; its
    configuration holds that one role, and the odometer's changes are logged
    under it."""
    flow = await start_vehicle(ui, "Odometer only")
    await flow.fill({"Odometer": odometer(ui.hass)})
    await flow.through_mapping_and_parameters()
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    assert [e["state"] for e in await ui.entries()] == ["loaded"]

    odometer(ui.hass, 12346)
    await settle(ui.hass)
    lines = stream(ui.hass)
    assert set(configured_roles(lines)) == {"odometer"}
    assert roles_of_states(lines) == {("odometer", "sensor.car_odometer")}


async def test_a_position_alone_is_enough(ui):
    """A vehicle with the position and no other role is set up and runs; its
    configuration holds that one role, and the position's changes are logged
    under it."""
    flow = await start_vehicle(ui, "Position only")
    await flow.fill({"Position": position(ui.hass)})
    await flow.through_mapping_and_parameters()
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    assert [e["state"] for e in await ui.entries()] == ["loaded"]

    position(ui.hass, 52.53, 13.41)
    await settle(ui.hass)
    lines = stream(ui.hass)
    assert set(configured_roles(lines)) == {"position"}
    assert roles_of_states(lines) == {("position", "device_tracker.car")}


async def test_each_source_entity_is_logged_under_the_role_it_was_assigned(ui):
    """Several entities assigned side by side: each change lands in the raw
    log under the role its entity was given, and the configuration names
    every assigned role with its entity."""
    assigned = {"Odometer": ("odometer", odometer(ui.hass)),
                "Position": ("position", position(ui.hass)),
                "Fuel level": ("fuel_level", fuel_level(ui.hass)),
                "State of charge": ("soc", soc(ui.hass))}
    flow = await start_vehicle(ui, "Hybrid")
    await flow.fill({label: entity for label, (_, entity) in assigned.items()})
    await flow.through_mapping_and_parameters()
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)

    odometer(ui.hass, 12350)
    position(ui.hass, 52.54, 13.42)
    fuel_level(ui.hass, 29)
    soc(ui.hass, 75)
    await settle(ui.hass)
    lines = stream(ui.hass)
    roles = configured_roles(lines)
    assert {role: roles[role]["entity"] for role in roles} == dict(assigned.values())
    assert roles_of_states(lines) >= set(assigned.values())


@pytest.mark.parametrize("others", [
    {},
    {"Fuel level": fuel_level, "State of charge": soc, "Charging state": charging_state},
], ids=["nothing-assigned", "no-movement-role"])
async def test_a_vehicle_without_odometer_or_position_is_refused(ui, others):
    """Without odometer or position, the Roles step is shown again with an
    error, however many other roles are assigned, and no entry is made; the
    same form then takes an odometer and goes on."""
    flow = await start_vehicle(ui, "No movement")
    await flow.fill({label: make(ui.hass) for label, make in others.items()})
    assert flow.step["type"] == "form" and flow.title == "Roles", flow.step
    assert flow.step.get("errors"), flow.step
    assert await ui.entries() == []

    await flow.fill({"Odometer": odometer(ui.hass)})
    assert flow.step["type"] == "form" and flow.title in ("Mapping", "Parameters"), flow.step


async def test_the_options_refuse_to_take_the_last_movement_role_away(ui):
    """The entry's Configure → Roles, pre-filled: emptying the odometer when
    it is the only movement role is refused with an error, and the vehicle
    keeps its odometer."""
    flow = await start_vehicle(ui, "Family car")
    await flow.fill({"Odometer": odometer(ui.hass), "State of charge": soc(ui.hass)})
    await flow.through_mapping_and_parameters()
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = await ui.entries()

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Roles")
    assert flow.shown(flow.field("Odometer")) == "sensor.car_odometer"
    await flow.fill({"Odometer": None})
    assert flow.step["type"] == "form", flow.step
    assert flow.step.get("errors"), flow.step
    await settle(ui.hass)
    assert set(configured_roles(stream(ui.hass))) == {"odometer", "soc"}


async def test_the_options_change_the_movement_role_from_odometer_to_position(ui):
    """Every role stays optional after setup: the odometer may go once the
    position takes its place, and the configuration then holds the position
    alone."""
    flow = await start_vehicle(ui, "Family car")
    await flow.fill({"Odometer": odometer(ui.hass)})
    await flow.through_mapping_and_parameters()
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = await ui.entries()

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Roles")
    await flow.fill({"Odometer": None, "Position": position(ui.hass)})
    if flow.step["type"] == "form":
        await flow.through_mapping_and_parameters()
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    assert set(configured_roles(stream(ui.hass))) == {"position"}
