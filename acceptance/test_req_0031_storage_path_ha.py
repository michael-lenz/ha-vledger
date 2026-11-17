# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0031: the storage path is configurable; the default lies below the Home
Assistant configuration directory.

This module is the storage path as a Home Assistant user meets it. Derived
from the requirement, README.md's *Privacy* ("the default storage path lies
under the Home Assistant configuration directory and is therefore part of your
backups") and the user guide's *In Home Assistant*: "From the moment an entry
is set up its raw log is written under ``<config>/vledger/``"; the entry's
*Configure* offers, beside the steps of its setup, "the data directory", and
"Saving any of them reloads the entry, which the stream records as stop, start
and the new configuration"; and *Where the data is*: "Under Home Assistant that
directory is ``<config>/vledger/``", a stream's directory under it named
``vehicle-<id>`` or ``chargepoint-<id>`` after the subject id the integration
minted, and read back with ``vledger l0 read --base DIR``. The data directory
of the ``vledger`` command is the other module, test_req_0031_storage_path.py.

The options menu's *Storage* and its field *Data directory* are the labels
the frontend shows for "the data directory"; the user guide names neither
(ISSUE-0136). The guide offers the data directory under Configure for a
vehicle and names, for a charge point, only the meter, the charging power and
the tariffs, so a changed data directory is shown for a vehicle alone. Which
form of path the field takes the guide does not say; the tests give it an
absolute one.

Home Assistant starts with nothing of the ledger configured, in a temporary
configuration directory. The tests do what the frontend does — call the config
and options flow endpoints over HTTP, choosing every field by the label the
frontend shows — and read the raw log back with ``vledger`` in a process of its
own. Nothing of ``vledger`` or of the integration is imported. The harness is
that of test_req_0027_configuration_state.py, as ISSUE-0132 describes."""

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

pytestmark = [pytest.mark.req("REQ-0031")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"
ENTRIES = "/api/config/config_entries/entry"

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




def streams(directory):
    """The streams' directories directly under a data directory, by name."""
    directory = pathlib.Path(directory)
    return sorted(p.name for p in directory.iterdir() if p.is_dir()) if directory.exists() else []


def read(directory, name):
    """The stream of the subject whose directory is ``name``, as ``vledger l0
    read --base <directory>`` prints it."""
    kind, subject = name.split("-", 1)
    return jsonl(vledger(directory, "l0", "read", f"--{kind}", subject))


def odometer_states(lines):
    return [ln for ln in lines if ln["kind"] == "state"
            and ODOMETER in json.dumps(ln)]


def holds_reading(lines, value):
    """Whether one of the stream's odometer state lines holds the raw value."""
    return any(f'"{value}"' in json.dumps(ln) for ln in odometer_states(lines))


async def test_a_vehicle_s_raw_log_is_written_below_the_configuration_directory_by_default(ui):
    """A vehicle added and its data directory left alone: its stream is
    written in ``<config>/vledger/``, under a directory of its own, and holds
    the start, the configuration and a reading taken since."""
    await add_vehicle(ui)
    ui.hass.states.async_set(ODOMETER, "12350", {"unit_of_measurement": "km",
                                                 "device_class": "distance",
                                                 "state_class": "total_increasing"})
    await settle(ui.hass)

    default = pathlib.Path(ui.hass.config.config_dir) / "vledger"
    [name] = streams(default)
    assert name.startswith("vehicle-"), name
    lines = read(default, name)
    assert [ln["kind"] for ln in lines][:2] == ["start", "config"], lines
    assert holds_reading(lines, "12350"), lines


async def test_a_charge_point_s_raw_log_is_written_below_the_configuration_directory_by_default(ui):
    """A charge point added: its stream is written in ``<config>/vledger/``,
    under a directory of its own, and holds its start and configuration."""
    await add_chargepoint(ui)

    default = pathlib.Path(ui.hass.config.config_dir) / "vledger"
    [name] = streams(default)
    assert name.startswith("chargepoint-"), name
    assert [ln["kind"] for ln in read(default, name)][:2] == ["start", "config"]


async def test_a_vehicle_and_a_charge_point_share_the_default_data_directory(ui):
    """Both kinds of entry, left alone, write side by side in the one
    default data directory."""
    await add_vehicle(ui)
    await add_chargepoint(ui)

    default = pathlib.Path(ui.hass.config.config_dir) / "vledger"
    assert [n.split("-", 1)[0] for n in streams(default)] == ["chargepoint", "vehicle"]


async def test_the_data_directory_chosen_under_configure_is_where_the_stream_goes_on(
        ui, tmp_path_factory):
    """Configure → Storage → Data directory set to a directory outside the
    configuration directory and saved: the entry reloads, and its stream goes
    on there — under the same subject's directory, beginning with start and
    configuration — with every reading taken since; the stream left under
    ``<config>/vledger/`` gets none of them."""
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "ledger"
    entry_id = await add_vehicle(ui)
    default = pathlib.Path(ui.hass.config.config_dir) / "vledger"
    [name] = streams(default)

    await change(ui, entry_id, "Storage", {"Data directory": str(elsewhere)})
    ui.hass.states.async_set(ODOMETER, "12360", {"unit_of_measurement": "km",
                                                 "device_class": "distance",
                                                 "state_class": "total_increasing"})
    await settle(ui.hass)

    assert streams(elsewhere) == [name]
    moved = read(elsewhere, name)
    assert [ln["kind"] for ln in moved][:2] == ["start", "config"], moved
    assert holds_reading(moved, "12360"), moved
    assert not holds_reading(read(default, name), "12360")


async def test_the_chosen_data_directory_holds_after_home_assistant_restarts(
        ui, tmp_path_factory):
    """The data directory chosen and the entry then unloaded and set up again,
    as when Home Assistant stops and starts: the stream goes on in the chosen
    directory, not in the default one."""
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "ledger"
    entry_id = await add_vehicle(ui)
    [name] = streams(pathlib.Path(ui.hass.config.config_dir) / "vledger")
    await change(ui, entry_id, "Storage", {"Data directory": str(elsewhere)})

    assert await ui.hass.config_entries.async_unload(entry_id)
    await settle(ui.hass)
    assert await ui.hass.config_entries.async_setup(entry_id)
    await settle(ui.hass)
    ui.hass.states.async_set(ODOMETER, "12370", {"unit_of_measurement": "km",
                                                 "device_class": "distance",
                                                 "state_class": "total_increasing"})
    await settle(ui.hass)

    assert holds_reading(read(elsewhere, name), "12370")
    assert not holds_reading(read(pathlib.Path(ui.hass.config.config_dir) / "vledger", name),
                             "12370")
