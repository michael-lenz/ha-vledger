# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0018: for roles with enumerated values (e.g. charging state) it is
configurable which source values correspond to which domain state.

Derived from the requirement, README.md (charging sessions "from the charging
state") and the user guide: *In Home Assistant* — the **Mapping** step, "only
when an enumerated role was assigned: tick the source's values that mean
*charging*, *plugged in*, …; *unavailable* and *unknown* hold the last known
state; any other value means the opposite", the entry's *Configure* as "a menu
over the same steps, pre-filled", and **Download diagnostics** with "the source
values an enumerated role met that its mapping does not list" — and *The
derivations*: "A charging session runs from the charging state turning to
`charging` until it turns to anything else; `unavailable` and `unknown` hold".

The charging state stands for every enumerated role: its domain state is the
one a user sees the ledger act on, as charging sessions printed by
``vledger derive charging``. The source used here speaks German ("Laden",
"Fertig"), so no value means charging unless it is ticked.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config flow, options flow, config entry and
diagnostics endpoints over HTTP, choosing every field by the label the
frontend shows — take the vehicle's subject id from the entry's diagnostics,
and run the ``vledger`` command in a process of its own on ``<config>/vledger/``.
Nothing of ``vledger`` or of the integration is imported."""

import asyncio
import json
import os
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from browser import open_frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"
ENTRIES = "/api/config/config_entries/entry"

CHARGING = "sensor.car_charging"
PLUG = "binary_sensor.car_plug"
SOC = "sensor.car_soc"
SOURCE_VALUES = ["Bereit", "Laden", "Fertig", "Fehler"]

pytestmark = [pytest.mark.req("REQ-0018")]


# --- what the browser does ---------------------------------------------------

@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client):
    """A Home Assistant with its frontend's API and diagnostics, and nothing
    configured for the ledger. The configuration directory is a temporary one,
    so the raw log lands there."""
    return UI(hass, await open_frontend(hass, tmp_path, hass_client, "diagnostics"))


class UI:
    def __init__(self, hass, client):
        self.hass, self.client = hass, client

    async def labels(self, category):
        return await async_get_translations(self.hass, "en", category, [DOMAIN])

    async def post(self, path, data):
        resp = await self.client.post(path, json=data)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def get(self, path):
        resp = await self.client.get(path)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def config_flow(self):
        return Flow(self, FLOW, "config", await self.post(FLOW, {"handler": DOMAIN}),
                    await self.labels("config"))

    async def options_flow(self, entry_id):
        return Flow(self, OPTIONS, "options", await self.post(OPTIONS, {"handler": entry_id}),
                    await self.labels("options"))

    async def entry(self):
        entries = await self.get(f"{ENTRIES}?domain={DOMAIN}")
        assert len(entries) == 1, entries
        return entries[0]

    async def diagnostics(self, entry_id):
        """The entry's menu → Download diagnostics."""
        return await self.get(f"/api/diagnostics/config_entry/{entry_id}")


class Flow:
    def __init__(self, ui, path, category, step, labels):
        self.ui, self.path, self.category, self.step, self.labels = ui, path, category, step, labels

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, self.category, "step",
                                         self.step["step_id"], *parts)))

    @property
    def title(self):
        return self._label("title")

    def options(self):
        assert self.step["type"] == "menu", self.step
        return {self._label("menu_options", o): o for o in self.step["menu_options"]}

    def fields(self):
        assert self.step["type"] == "form", self.step
        return {self._label("data", f["name"]): f for f in self.step["data_schema"]}

    def shown(self, label):
        field = self.fields()[label]
        return field.get("description", {}).get("suggested_value", field.get("default"))

    def offered(self, label):
        """The values a field offers to choose from."""
        selector = self.fields()[label]["selector"]
        [(_, config)] = selector.items()
        return [o["value"] if isinstance(o, dict) else o for o in config.get("options", [])]

    async def choose(self, label):
        options = self.options()
        assert label in options, f"{label!r} not among {sorted(options, key=str)}"
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}",
                                       {"next_step_id": options[label]})
        return self

    async def fill(self, values):
        """Fill in by label and submit; a field left alone is sent with what
        the form showed in it, an empty one is not sent."""
        fields = self.fields()
        missing = set(values) - set(fields)
        assert not missing, f"{sorted(missing)} not among {sorted(fields, key=str)}"
        data = {}
        for label, field in fields.items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}", data)
        return self


# --- the vehicle and its sources ---------------------------------------------

def report(hass, entity, state):
    """A source entity reports a value, as its own integration would."""
    if entity == CHARGING:
        attrs = {"device_class": "enum", "options": SOURCE_VALUES}
    elif entity == PLUG:
        attrs = {"device_class": "plug"}
    elif entity == SOC:
        attrs = {"unit_of_measurement": "%", "device_class": "battery"}
    else:
        attrs = {"unit_of_measurement": "km", "device_class": "distance",
                 "state_class": "total_increasing"}
    hass.states.async_set(entity, state, attrs)


async def roles_step(ui, name="Hybrid", plug=False):
    """Add integration → A vehicle → Name → Roles, with an odometer, a state of
    charge and the charging state (and a plug state) assigned; the flow is left
    at the step that follows the roles."""
    hass = ui.hass
    report(hass, "sensor.car_odometer", "12345")
    report(hass, SOC, "40")
    report(hass, CHARGING, "Bereit")
    roles = {"Odometer": "sensor.car_odometer", "State of charge": SOC,
             "Charging state": CHARGING}
    if plug:
        report(hass, PLUG, "off")
        roles["Plug state"] = PLUG
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    assert flow.title == "Roles"
    await flow.fill(roles)
    return flow


async def add_vehicle(ui, charging):
    """A vehicle whose Mapping step has ``charging`` ticked as the values that
    mean charging; returns its entry once it runs."""
    flow = await roles_step(ui)
    assert flow.title == "Mapping", flow.step
    await flow.fill({"Values that mean charging": charging})
    assert flow.title == "Parameters", flow.step
    await flow.fill({"Net battery capacity": 10.5})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()
    entry = await ui.entry()
    assert entry["state"] == "loaded", entry
    return entry


async def play(hass, reports):
    """The sources report one after the other, a moment apart."""
    for entity, state in reports:
        report(hass, entity, state)
        await hass.async_block_till_done()
        await asyncio.sleep(0.02)


# A charge as the German source reports it: ready, charging, a drop-out,
# charging again, finished, a fault, ready. The state of charge climbs
# meanwhile.
CHARGE = [(CHARGING, "Laden"), (SOC, "50"), (CHARGING, "unavailable"), (SOC, "60"),
          (CHARGING, "Laden"), (SOC, "70"), (CHARGING, "Fertig"), (SOC, "75"),
          (CHARGING, "Fehler"), (CHARGING, "Bereit")]


# --- what the user reads back ------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]


def subject_of(diagnostics):
    """The vehicle's subject id, as the entry's diagnostics name it."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "subject" and isinstance(value, str):
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(diagnostics["data"])
    assert len(found) == 1, found
    return found.pop()


async def ledger(ui, entry):
    """What ``vledger`` says of the vehicle: its raw log and its charging
    sessions."""
    base = pathlib.Path(ui.hass.config.path("vledger"))
    subject = subject_of(await ui.diagnostics(entry["entry_id"]))
    return (vledger(base, "l0", "read", "--vehicle", subject),
            vledger(base, "derive", "charging", "--vehicle", subject))


def reported(lines, state):
    """The times the charging state's source reported ``state``, in order."""
    return [ln["t"] for ln in lines
            if ln["kind"] == "state" and ln.get("entity") == CHARGING and ln["state"] == state]


def from_the_charging_state(sessions):
    return [(s["start"], s["end"]) for s in sessions if s["source"] == "charging_state"]


# --- the tests ---------------------------------------------------------------

async def test_the_mapping_step_offers_each_enumerated_roles_own_values(ui):
    """The Mapping step comes when an enumerated role is assigned and asks, for
    each such role, which of its source's values mean the domain state —
    offering the values that source reports, and none ticked for the user."""
    flow = await roles_step(ui, plug=True)
    assert flow.title == "Mapping", flow.step
    fields = flow.fields()
    assert {"Values that mean charging", "Values that mean plugged in"} <= set(fields)
    assert set(SOURCE_VALUES) <= set(flow.offered("Values that mean charging"))
    assert {"on", "off"} <= set(flow.offered("Values that mean plugged in"))
    assert not flow.shown("Values that mean charging")


async def test_no_mapping_step_without_an_enumerated_role(ui):
    """Only when an enumerated role was assigned: an odometer and a state of
    charge alone go from the roles straight to the parameters."""
    report(ui.hass, "sensor.car_odometer", "12345")
    report(ui.hass, SOC, "40")
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": "Plain"})
    await flow.fill({"Odometer": "sensor.car_odometer", "State of charge": SOC})
    assert flow.title == "Parameters", flow.step


@pytest.mark.parametrize("ticked, until", [
    (["Laden"], "Fertig"),
    (["Laden", "Fertig"], "Fehler"),
], ids=["charging-only", "charging-and-finished"])
async def test_the_values_ticked_are_the_ones_that_mean_charging(ui, ticked, until):
    """The same source, the same reports: the charging session runs from the
    first value ticked to the first value not ticked, so what was ticked
    decides where it ends. A drop-out to unavailable in between holds the
    state and does not split it."""
    entry = await add_vehicle(ui, ticked)
    await play(ui.hass, CHARGE)
    lines, sessions = await ledger(ui, entry)
    assert from_the_charging_state(sessions) == [
        (reported(lines, "Laden")[0], reported(lines, until)[0])]


async def test_a_value_nobody_ticked_means_not_charging(ui):
    """With nothing ticked, no value of the source means charging: the
    charge the source reports in its own words is no charging session."""
    entry = await add_vehicle(ui, [])
    await play(ui.hass, CHARGE)
    _, sessions = await ledger(ui, entry)
    assert from_the_charging_state(sessions) == []


async def test_the_diagnostics_name_the_values_the_mapping_does_not_list(ui):
    """Download diagnostics lists the source values the charging state met that
    its mapping does not list — read as not charging, worth a tick if wrong —
    and not the ticked one, nor unavailable, which holds."""
    entry = await add_vehicle(ui, ["Laden"])
    await play(ui.hass, CHARGE)
    diagnostics = await ui.diagnostics(entry["entry_id"])
    unlisted = {"Bereit", "Fertig", "Fehler"}

    def lists(node):
        if isinstance(node, list) and node and all(isinstance(v, str) for v in node):
            yield set(node)
        elif isinstance(node, dict):
            for value in node.values():
                yield from lists(value)
        elif isinstance(node, list):
            for value in node:
                yield from lists(value)

    assert unlisted in list(lists(diagnostics["data"])), json.dumps(diagnostics["data"])[:2000]


async def test_the_mapping_is_changed_in_the_options_and_the_ledger_follows(ui):
    """Configure → Mapping shows what was ticked; ticking another value is
    saved and shown next time, and a charge reported after it is read by the
    new mapping."""
    entry = await add_vehicle(ui, ["Laden"])

    flow = await ui.options_flow(entry["entry_id"])
    assert "Mapping" in flow.options()
    await flow.choose("Mapping")
    assert flow.shown("Values that mean charging") == ["Laden"]
    await flow.fill({"Values that mean charging": ["Laden", "Fertig"]})
    while flow.step["type"] == "form":
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()
    assert (await ui.entry())["state"] == "loaded"

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Mapping")
    assert sorted(flow.shown("Values that mean charging")) == ["Fertig", "Laden"]

    await play(ui.hass, CHARGE)
    lines, sessions = await ledger(ui, entry)
    assert from_the_charging_state(sessions) == [
        (reported(lines, "Laden")[0], reported(lines, "Fehler")[0])]
