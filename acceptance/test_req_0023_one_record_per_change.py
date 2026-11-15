# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0023: every change of the state, or of a role-relevant attribute, of an
assigned source entity produces exactly one L0 record.

Derived from the requirement, README.md ("Every change of a source entity you
assign to a role becomes one record in a raw log (L0)"; "without touching
anything else the vehicle reports") and the user guide: *In Home Assistant*
(the **Roles** step; "From the moment an entry is set up its raw log is
written under ``<config>/vledger/``"), *Where the data is* (a stream named by
the vehicle's subject id) and *The raw log: vledger l0* (``vledger l0 read
--vehicle ID --kind state --role odometer`` prints one role's state lines as
JSON Lines with their ``state``; of a position's attributes ``latitude``,
``longitude`` and ``gps_accuracy`` are kept and ``battery`` is dropped, "only
the attributes relevant to the role are kept").

Which attributes are relevant to a role the user guide shows for the position
alone, so the attribute tests change a position's coordinates. Where a state
line keeps its attributes the user guide does not name; a test finds a
coordinate as a value anywhere in the line. A change of an attribute that is
not relevant to its role is left out: REQ-0023 says nothing of it.

Home Assistant starts with nothing of the ledger configured. The tests add a
vehicle as the frontend does, over the config flow's HTTP endpoints, choosing
every field by the label the frontend shows; then they change the source
entities and read the raw log back with the ``vledger`` command in a process
of its own. Nothing of ``vledger`` or of the integration is imported."""

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
ENTRIES = "/api/config/config_entries/entry"

pytestmark = [pytest.mark.req("REQ-0023")]


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
        return Flow(self, FLOW, await self.post(FLOW, {"handler": DOMAIN}), labels)


class Flow:
    """One config flow as the browser shows it: a menu or a form, by its
    labels."""

    def __init__(self, ui, path, step, labels):
        self.ui, self.path, self.step, self.labels = ui, path, step, labels

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, "config", "step",
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

    async def fill(self, values):
        """Fill in by label and submit. As in the browser, a field left alone
        is sent with what the form showed in it."""
        fields = self.fields()
        values = {self.field(k): v for k, v in values.items()}
        data = {}
        for label, field in fields.items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}", data)


async def add_vehicle(ui, roles):
    """A vehicle added in the four steps of the user guide: a name, the roles
    given by label, the Mapping ticked as shown where it is shown, and the
    Parameters with the capacities a derivation may ask for. It comes up
    loaded."""
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": "Family car"})
    assert flow.title == "Roles", flow.step
    await flow.fill(roles)
    if flow.step["type"] == "form" and flow.title == "Mapping":
        await flow.fill({})
    assert flow.step["type"] == "form" and flow.title == "Parameters", flow.step
    given = {}
    for name, value in (("Tank capacity", 50), ("Net battery capacity", 10.5)):
        if any(lab and lab.casefold() == name.casefold() for lab in flow.fields()):
            given[name] = value
    await flow.fill(given)
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    assert [e["state"] for e in await ui.entries()] == ["loaded"]


async def settle(hass):
    """Let the entry come up and capture write what it was handed: its writes
    run off the event loop."""
    for _ in range(10):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


# --- the source entities -----------------------------------------------------

def odometer(hass, km=12345, entity="sensor.car_odometer"):
    hass.states.async_set(entity, str(km), {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})
    return entity


def position(hass, lat=52.52, lon=13.40, state="not_home", entity="device_tracker.car"):
    hass.states.async_set(entity, state, {"latitude": lat, "longitude": lon,
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


def states(hass, role):
    """The one vehicle's state lines of one role, in order, as ``vledger l0
    read --kind state --role <role>`` prints them."""
    base = pathlib.Path(hass.config.path("vledger"))
    dirs = sorted(base.glob("vehicle-*"))
    assert len(dirs) == 1, dirs
    subject = dirs[0].name.removeprefix("vehicle-")
    out = vledger(base, "l0", "read", "--vehicle", subject, "--kind", "state", "--role", role)
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    assert all(ln["kind"] == "state" and ln["role"] == role for ln in lines), lines
    return lines


def all_states(hass):
    """Every state line of the one vehicle, in order, as ``vledger l0 read
    --kind state`` prints them."""
    base = pathlib.Path(hass.config.path("vledger"))
    [d] = sorted(base.glob("vehicle-*"))
    out = vledger(base, "l0", "read", "--vehicle", d.name.removeprefix("vehicle-"),
                  "--kind", "state")
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def holds(value, number):
    """Whether ``number`` stands anywhere in ``value``, nested as it may be."""
    if isinstance(value, dict):
        return any(holds(v, number) for v in value.values())
    if isinstance(value, list):
        return any(holds(v, number) for v in value)
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return abs(value - number) < 1e-9
    if isinstance(value, str):
        try:
            return abs(float(value) - number) < 1e-9
        except ValueError:
            return False
    return False


# --- the tests ----------------------------------------------------------------

ROLES = {
    "odometer": ("Odometer", odometer, [12346, 12347.5, 12350, 12351]),
    "fuel_level": ("Fuel level", fuel_level, [29.5, 29, 45, 44.8]),
    "soc": ("State of charge", soc, [79, 81, 85, 90]),
    "charging_state": ("Charging state", charging_state, ["on", "off", "on", "off"]),
}


@pytest.mark.parametrize("role", list(ROLES))
async def test_each_change_of_a_roles_state_is_one_line(ui, role):
    """Every change of an assigned entity's state, settled one by one, adds
    exactly one state line of its role, with the new state."""
    label, make, values = ROLES[role]
    roles = {label: make(ui.hass)}
    if role != "odometer":
        roles["Odometer"] = odometer(ui.hass)
    await add_vehicle(ui, roles)
    before = len(states(ui.hass, role))

    for i, value in enumerate(values, start=1):
        make(ui.hass, value)
        await settle(ui.hass)
        lines = states(ui.hass, role)
        assert len(lines) == before + i, (value, lines)
        assert lines[-1]["state"] == str(value), (value, lines[-1])


async def test_changes_in_quick_succession_are_one_line_each(ui):
    """Changes that follow each other faster than capture writes are each
    kept: none is merged into another, none is written twice."""
    await add_vehicle(ui, {"Odometer": odometer(ui.hass)})
    before = states(ui.hass, "odometer")

    values = [str(12345 + k) for k in range(1, 21)]
    for value in values:
        odometer(ui.hass, value)
    await settle(ui.hass)
    lines = states(ui.hass, "odometer")
    assert [ln["state"] for ln in lines[len(before):]] == values


async def test_a_change_of_a_positions_coordinates_alone_is_one_line(ui):
    """A position whose state stays ``not_home`` while its latitude and
    longitude move: each move adds exactly one position line, holding the new
    coordinates."""
    await add_vehicle(ui, {"Position": position(ui.hass)})
    before = len(states(ui.hass, "position"))

    moves = [(52.5210, 13.4010), (52.5230, 13.4050), (52.5260, 13.4100)]
    for i, (lat, lon) in enumerate(moves, start=1):
        position(ui.hass, lat, lon)
        await settle(ui.hass)
        lines = states(ui.hass, "position")
        assert len(lines) == before + i, ((lat, lon), lines)
        assert lines[-1]["state"] == "not_home", lines[-1]
        assert holds(lines[-1], lat) and holds(lines[-1], lon), ((lat, lon), lines[-1])


async def test_a_change_of_state_and_attributes_at_once_is_one_line(ui):
    """A position that changes its state and its coordinates in the same
    update adds one line, not one for the state and one for the
    attributes."""
    await add_vehicle(ui, {"Position": position(ui.hass)})
    before = len(states(ui.hass, "position"))

    position(ui.hass, 52.5300, 13.4200, state="home")
    await settle(ui.hass)
    lines = states(ui.hass, "position")
    assert len(lines) == before + 1, lines
    assert lines[-1]["state"] == "home", lines[-1]
    assert holds(lines[-1], 52.53) and holds(lines[-1], 13.42), lines[-1]


async def test_changes_of_several_roles_interleaved_are_one_line_each(ui):
    """Several entities assigned side by side and changed in turn: every
    change adds exactly one state line, under its own role, in the order the
    changes happened."""
    await add_vehicle(ui, {"Odometer": odometer(ui.hass), "Position": position(ui.hass),
                           "Fuel level": fuel_level(ui.hass),
                           "State of charge": soc(ui.hass)})
    before = len(all_states(ui.hass))

    changes = [("odometer", lambda: odometer(ui.hass, 12346)),
               ("position", lambda: position(ui.hass, 52.5250, 13.4080)),
               ("fuel_level", lambda: fuel_level(ui.hass, 29)),
               ("soc", lambda: soc(ui.hass, 78)),
               ("odometer", lambda: odometer(ui.hass, 12348)),
               ("position", lambda: position(ui.hass, 52.5290, 13.4150)),
               ("soc", lambda: soc(ui.hass, 77))]
    for _, change in changes:
        change()
        await settle(ui.hass)
    lines = all_states(ui.hass)[before:]
    assert [ln["role"] for ln in lines] == [role for role, _ in changes], lines


async def test_an_entity_set_again_unchanged_adds_no_line(ui):
    """Exactly one, not at least one: after a change has been logged, the
    entity reporting the same state and attributes again — no change — adds
    nothing."""
    await add_vehicle(ui, {"Odometer": odometer(ui.hass)})
    odometer(ui.hass, 12346)
    await settle(ui.hass)
    before = states(ui.hass, "odometer")

    for _ in range(3):
        odometer(ui.hass, 12346)
        await settle(ui.hass)
    assert states(ui.hass, "odometer") == before
