# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0027: every configuration change (role assignment, state mapping,
parameters, thresholds, charge points, tariffs) is logged to L0 as a complete
configuration state.

Derived from the requirement and the user guide's *In Home Assistant*: a
vehicle is added in four steps (Name, Roles, Mapping, Parameters) and a charge
point in one (name, location and radius, an optional energy meter, an optional
charging power, the first tariff with its date); from the moment an entry is
set up its raw log is written under ``<config>/vledger/``; the entry's
*Configure* is "a menu over the same steps, pre-filled, plus the thresholds
and time constants, the remaining parameters", and for a charge point "the
meter, the charging power, and *add a tariff from a date*"; "Saving any of
them reloads the entry, which the stream records as stop, start and the new
configuration." The stream is read back with ``vledger l0 read --vehicle ID``
or ``--chargepoint ID`` (*Where the data is*), its directory named after the
subject as in test_req_0014_vehicles_in_parallel.py.

A configuration line is told by its kind, ``config``, as the guide's
``vledger l0 config`` writes it. The user documentation names none of its
keys, so the tests find each configured value as a value anywhere in that one
line. "Complete" is read as: the latest configuration line alone holds every
value the configuration then has — the one just changed and every one left as
it was, the defaults the user never touched included — so that no earlier line
is needed to know it. For a vehicle that is every value its options forms show
pre-filled in the four steps REQ-0027 names (Roles, Mapping, Parameters,
Thresholds and time constants); for a charge point, which shows neither its
location nor its tariffs in the options, every value it was given.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config and options flow endpoints over HTTP,
choosing every field by the label the frontend shows — and read the raw log
back with ``vledger`` in a process of its own. Nothing of ``vledger`` or of
the integration is imported."""

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

pytestmark = [pytest.mark.req("REQ-0027")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"
ENTRIES = "/api/config/config_entries/entry"

# The four steps of a vehicle's configuration REQ-0027 names, as the options
# menu labels them.
STEPS = ("Roles", "Mapping", "Parameters", "Thresholds and time constants")

ODOMETER, CHARGING, FUEL_LEVEL = "sensor.odometer", "sensor.charging", "sensor.fuel_level"


def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def jsonl(text):
    return [json.loads(x) for x in text.splitlines() if x.strip()]


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
    # Home Assistant's own server keeps its router open after it starts, since
    # an integration set up later in the UI adds paths to it. The test client
    # starts the server here, before any entry exists: the router is kept open
    # as Home Assistant keeps it.
    hass.http.app.router.freeze = lambda: None
    return UI(hass, await hass_client())


class UI:
    """What a person's browser does: start a flow, read the form with its
    labels, fill it in by label, submit."""

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

    async def flow(self, path, category, handler):
        labels = await async_get_translations(self.hass, "en", category, [DOMAIN])
        return Flow(self, path, category, await self.post(path, {"handler": handler}), labels)


class Flow:
    def __init__(self, ui, path, category, step, labels):
        self.ui, self.path, self.category, self.step, self.labels = ui, path, category, step, labels

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, self.category, "step",
                                         self.step["step_id"], *parts)))

    @property
    def title(self):
        return self._label("title")

    def fields(self):
        assert self.step["type"] == "form", self.step
        return {self._label("data", f["name"]): f for f in self.step["data_schema"]}

    def shown(self, label):
        field = self.fields()[label]
        return field.get("description", {}).get("suggested_value", field.get("default"))

    async def choose(self, label):
        assert self.step["type"] == "menu", self.step
        options = {self._label("menu_options", o): o for o in self.step["menu_options"]}
        assert label in options, f"{label!r} not among {sorted(options, key=str)}"
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}",
                                       {"next_step_id": options[label]})

    async def fill(self, values):
        """Fill in by label and submit. As in the browser, a field left alone
        is sent with what the form showed, and an empty one is not sent."""
        fields = self.fields()
        missing = set(values) - set(fields)
        assert not missing, f"{sorted(missing)} not among {sorted(fields, key=str)}"
        data = {}
        for label, field in fields.items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}", data)


async def settle(hass):
    """Let capture write what it was handed: its writes run off the event loop."""
    for _ in range(5):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


def sources(hass):
    """The vehicle's source entities: an odometer, a charging state that
    reports words of its own (an enumerated role), and a fuel level."""
    hass.states.async_set(ODOMETER, "12345", {"unit_of_measurement": "km",
                                              "device_class": "distance",
                                              "state_class": "total_increasing"})
    hass.states.async_set(CHARGING, "Laden", {})
    hass.states.async_set(FUEL_LEVEL, "31", {"unit_of_measurement": "L",
                                             "device_class": "volume_storage",
                                             "state_class": "measurement"})


async def add_vehicle(ui, name="Family car"):
    """Add integration → Vehicle Ledger → A vehicle: Name; Roles, the odometer
    and the charging state; Mapping, *Laden* means charging; Parameters,
    petrol and a 52 L tank."""
    sources(ui.hass)
    flow = await ui.flow(FLOW, "config", DOMAIN)
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    await flow.fill({"Odometer": ODOMETER, "Charging state": CHARGING})
    assert flow.title == "Mapping", flow.step
    await flow.fill({"Values that mean charging": ["Laden"]})
    assert flow.title == "Parameters", flow.step
    await flow.fill({"Fuel": "petrol", "Tank capacity": 52})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = [e for e in await ui.entries() if e["title"] == name]
    return entry["entry_id"]


async def add_chargepoint(ui, name="Home"):
    """Add integration → Vehicle Ledger → A charge point, with a location and
    radius of its own and its first tariff."""
    flow = await ui.flow(FLOW, "config", DOMAIN)
    await flow.choose("A charge point")
    await flow.fill({"Name": name,
                     "Location and radius": {"latitude": 51.4437, "longitude": 7.1413,
                                             "radius": 35},
                     "Tariff": 0.31, "Tariff valid from": "2026-01-01"})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = [e for e in await ui.entries() if e["title"] == name]
    return entry["entry_id"]


async def change(ui, entry_id, step, values):
    """Configure → <step>: the values given, the rest as the form shows it,
    and saved; a step that follows (such as the mapping after the roles) is
    saved as shown."""
    flow = await ui.flow(OPTIONS, "options", entry_id)
    await flow.choose(step)
    await flow.fill(values)
    while flow.step["type"] == "form":
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    assert {e["entry_id"]: e["state"] for e in await ui.entries()}[entry_id] == "loaded"


async def shown_in_options(ui, entry_id):
    """Every value the vehicle's options forms show pre-filled in the four
    steps of its configuration, by step and label; nothing is saved."""
    shown = {}
    for step in STEPS:
        flow = await ui.flow(OPTIONS, "options", entry_id)
        await flow.choose(step)
        for label in flow.fields():
            value = flow.shown(label)
            if value not in (None, "", []):
                shown[(step, label)] = value
    return shown


def stream(hass, kind):
    """The raw log of the one vehicle or charge point under
    ``<config>/vledger/``, as ``vledger l0 read`` prints it."""
    log = pathlib.Path(hass.config.path("vledger"))
    [d] = sorted(log.glob(f"{kind}-*"))
    subject = d.name.removeprefix(f"{kind}-")
    return jsonl(vledger(log, "l0", "read", f"--{kind}", subject))


def configs(lines):
    return [ln for ln in lines if ln["kind"] == "config"]


def values_in(node):
    """Every value a line holds, at any depth: the scalars, and each list as a
    whole beside its items."""
    if isinstance(node, dict):
        for v in node.values():
            yield from values_in(v)
    elif isinstance(node, list):
        yield node
        for v in node:
            yield from values_in(v)
    else:
        yield node


def holds(line, value):
    """Whether the line holds the value somewhere; a list (the ticked values
    of a mapping) is held when each of its items is."""
    if isinstance(value, list):
        return all(holds(line, v) for v in value)
    found = list(values_in(line))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return any(v == value and type(v) is type(value) for v in found)
    return any(isinstance(v, (int, float)) and not isinstance(v, bool) and v == value
               for v in found)


def missing(line, shown):
    return {k: v for k, v in shown.items() if not holds(line, v)}


async def test_a_vehicle_added_logs_its_whole_configuration(ui):
    """The vehicle just added: its stream holds one configuration line, and
    that line holds every value the options then show — the roles, the
    mapping, the parameters given and the defaults, and every threshold and
    time constant, none of which the user touched."""
    entry_id = await add_vehicle(ui)
    lines = stream(ui.hass, "vehicle")
    [config] = configs(lines)

    shown = await shown_in_options(ui, entry_id)
    assert {step for step, _ in shown} == set(STEPS), shown
    assert not missing(config, shown)


CHANGES = {
    "roles": ("Roles", {"Fuel level": FUEL_LEVEL}, FUEL_LEVEL),
    "mapping": ("Mapping", {"Values that mean charging": ["Laden", "Ladevorgang aktiv"]},
                "Ladevorgang aktiv"),
    "parameters": ("Parameters", {"Net battery capacity": 17.3}, 17.3),
    "thresholds": ("Thresholds and time constants", {"Standstill (T_still)": 2700}, 2700),
}


@pytest.mark.parametrize("what", list(CHANGES))
async def test_each_change_is_logged_as_the_whole_new_configuration(ui, what):
    """One step of the vehicle's configuration changed under Configure and
    saved: the stream goes on with stop, start and exactly one new
    configuration line; that line holds the value just set, which no earlier
    one did, and every other value of the configuration as the options now
    show it, the ones left unchanged included."""
    step, values, new = CHANGES[what]
    entry_id = await add_vehicle(ui)
    before = stream(ui.hass, "vehicle")
    assert not holds(configs(before)[-1], new)

    await change(ui, entry_id, step, values)
    after = stream(ui.hass, "vehicle")

    assert after[:len(before)] == before
    added = after[len(before):]
    kinds = [ln["kind"] for ln in added]
    assert kinds[:3] == ["stop", "start", "config"], kinds
    [config] = configs(added)
    assert holds(config, new), config

    shown = await shown_in_options(ui, entry_id)
    assert holds(list(shown.values()), new), shown
    assert not missing(config, shown)


async def test_successive_changes_each_log_the_configuration_as_it_then_is(ui):
    """Three changes in turn, to three steps: each adds one configuration line,
    and each line holds what was changed so far — the latest all of it, and
    the earlier ones none of what came after."""
    entry_id = await add_vehicle(ui)
    await change(ui, entry_id, "Thresholds and time constants", {"Refuelling threshold": 4.5})
    await change(ui, entry_id, "Parameters", {"Tank capacity": 61})
    await change(ui, entry_id, "Roles", {"Fuel level": FUEL_LEVEL})

    first, *changed = configs(stream(ui.hass, "vehicle"))
    assert len(changed) == 3
    assert [holds(c, 4.5) for c in (first, *changed)] == [False, True, True, True]
    assert [holds(c, 61) for c in (first, *changed)] == [False, False, True, True]
    assert [holds(c, FUEL_LEVEL) for c in (first, *changed)] == [False, False, False, True]
    # The tank capacity given when the vehicle was added is replaced, not kept
    # beside the new one.
    assert [holds(c, 52) for c in (first, *changed)] == [True, True, False, False]

    shown = await shown_in_options(ui, entry_id)
    assert not missing(changed[-1], shown)


async def test_a_charge_point_added_logs_its_whole_configuration(ui):
    """The charge point just added: its own stream holds one configuration
    line, with the location, the radius and the first tariff with its date."""
    await add_chargepoint(ui)
    [config] = configs(stream(ui.hass, "chargepoint"))
    for value in (51.4437, 7.1413, 35, 0.31, "2026-01-01"):
        assert holds(config, value), (value, config)


CHARGEPOINT_CHANGES = {
    "meter": ("Energy meter", {"Energy meter entity (empty: none)": "sensor.wallbox_energy"},
              "sensor.wallbox_energy"),
    "power": ("Charging power", {"Charging power (empty: unknown)": 7.4}, 7.4),
    "tariff": ("Add a tariff from a date", {"Tariff": 0.36, "Valid from": "2026-07-01"}, 0.36),
}


@pytest.mark.parametrize("what", list(CHARGEPOINT_CHANGES))
async def test_a_charge_points_change_is_logged_as_its_whole_new_configuration(ui, what):
    """A charge point's meter, charging power or a new tariff saved under
    Configure: its stream goes on with stop, start and exactly one new
    configuration line, which holds the value just set and everything the
    charge point was given before — its location and radius and its first
    tariff, which a new tariff joins and does not replace."""
    step, values, new = CHARGEPOINT_CHANGES[what]
    if what == "meter":
        ui.hass.states.async_set("sensor.wallbox_energy", "1520.4", {
            "unit_of_measurement": "kWh", "device_class": "energy",
            "state_class": "total_increasing"})
    entry_id = await add_chargepoint(ui)
    before = stream(ui.hass, "chargepoint")

    await change(ui, entry_id, step, values)
    after = stream(ui.hass, "chargepoint")

    assert after[:len(before)] == before
    added = after[len(before):]
    kinds = [ln["kind"] for ln in added]
    assert kinds[:3] == ["stop", "start", "config"], kinds
    [config] = configs(added)
    assert not holds(configs(before)[-1], new)
    for value in (new, 51.4437, 7.1413, 35, 0.31, "2026-01-01"):
        assert holds(config, value), (value, config)
    if what == "tariff":
        assert holds(config, "2026-07-01"), config


async def test_tariffs_added_in_turn_are_all_in_the_latest_configuration(ui):
    """Two tariffs added after the first, one a correction under the same date
    as an earlier one: the latest configuration line holds every tariff
    entered, each with its date, so the price valid at any time is read from
    that line alone."""
    entry_id = await add_chargepoint(ui)
    await change(ui, entry_id, "Add a tariff from a date", {"Tariff": 0.36, "Valid from": "2026-07-01"})
    await change(ui, entry_id, "Add a tariff from a date", {"Tariff": 0.35, "Valid from": "2026-07-01"})

    _first, *changed = configs(stream(ui.hass, "chargepoint"))
    assert len(changed) == 2
    latest = changed[-1]
    for value in (0.31, "2026-01-01", 0.36, 0.35, "2026-07-01"):
        assert holds(latest, value), (value, latest)
