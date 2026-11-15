# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0024, as Home Assistant's capture writes a state record: the schema
version; the vehicle's id; the Home Assistant timestamp (UTC, ISO 8601,
milliseconds); the role; the entity_id; the raw value; the source unit; and
the role-relevant attributes.

Derived from the requirement, README.md ("Every change of a source entity you
assign to a role becomes one record in a raw log"; "The raw log's layout,
version 5") and the user guide's *In Home Assistant* (a vehicle added in four
steps; "From the moment an entry is set up its raw log is written under
``<config>/vledger/``") and *The raw log: vledger l0* (the stream read back
with ``vledger l0 read``, its schema versions named by ``vledger l0
validate``, "only the attributes relevant to the role are kept"). The keys
are those the ``vledger l0`` verbs print, as test_req_0024_state_record.py
reads them.

The source's own measurement time is not tested here: the user guide does not
say what of a source entity capture reads as one (ISSUE-0126). It is accepted
through ``vledger l0 state --measured-at`` in the other module.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config flow over HTTP, choosing every field by the
label Home Assistant serves — set the source entities' states as a vehicle
integration would, and read the raw log back with the ``vledger`` command in a
process of its own. Nothing of ``vledger`` or of the integration is imported."""

import asyncio
import datetime as dt
import json
import os
import pathlib
import re
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

pytestmark = [pytest.mark.req("REQ-0024")]

UTC_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(Z|\+00:00)$")


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

    async def add_vehicle(self, name, roles):
        """Add integration → Vehicle Ledger → A vehicle: Name, Roles by the
        labels the user guide names, Mapping if shown and Parameters as
        shown."""
        labels = await async_get_translations(self.hass, "en", "config", [DOMAIN])
        step = await self.post(FLOW, {"handler": DOMAIN})

        def label(*parts):
            return labels.get(".".join(("component", DOMAIN, "config", "step",
                                        step["step_id"], *parts)))

        def field(shown, name):
            found = [lab for lab in shown if lab and (
                lab.casefold() == name.casefold()
                or lab.casefold().startswith(name.casefold() + " ("))]
            assert len(found) == 1, f"{name!r} not among {sorted(shown, key=str)}"
            return found[0]

        async def fill(values):
            assert step["type"] == "form", step
            shown = {label("data", f["name"]): f for f in step["data_schema"]}
            values = {field(shown, k): v for k, v in values.items()}
            data = {}
            for lab, f in shown.items():
                value = values.get(lab, f.get("description", {}).get(
                    "suggested_value", f.get("default")))
                if value is not None:
                    data[f["name"]] = value
            return await self.post(f"{FLOW}/{step['flow_id']}", data)

        assert step["type"] == "menu", step
        menu = {label("menu_options", o): o for o in step["menu_options"]}
        step = await self.post(f"{FLOW}/{step['flow_id']}", {"next_step_id": menu["A vehicle"]})
        step = await fill({"Name": name})
        assert label("title") == "Roles", step
        step = await fill(roles)
        while step["type"] == "form":     # Mapping, if shown; Parameters
            step = await fill({})
        assert step["type"] == "create_entry", step
        await settle(self.hass)


async def settle(hass):
    """Let the entry come up and capture write what it was handed: its writes
    run off the event loop."""
    for _ in range(10):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


# --- what the user reads back ------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def subject_of(hass):
    base = pathlib.Path(hass.config.path("vledger"))
    dirs = sorted(base.glob("vehicle-*"))
    assert len(dirs) == 1, dirs
    return base, dirs[0].name.removeprefix("vehicle-")


def states(hass):
    """The vehicle's state records, as ``vledger l0 read`` prints them."""
    base, subject = subject_of(hass)
    lines = [json.loads(x) for x in vledger(base, "l0", "read", "--vehicle", subject).splitlines()
             if x.strip()]
    return [ln for ln in lines if ln.get("kind") == "state"]


def instant(text):
    return dt.datetime.fromisoformat(text)


# --- the tests ----------------------------------------------------------------

async def test_a_captured_change_is_a_record_holding_every_item_the_requirement_names(ui):
    """An odometer changes in Home Assistant: its record carries the schema
    version, the vehicle's id, Home Assistant's time of the change in UTC to
    the millisecond, the role it was assigned to, its entity id, the value as
    Home Assistant held it and the unit the entity reports in."""
    hass = ui.hass
    hass.states.async_set("sensor.estate_odometer", "41000.0",
                          {"unit_of_measurement": "mi", "device_class": "distance"})
    await ui.add_vehicle("Estate", {"Odometer": "sensor.estate_odometer"})
    before = len(states(hass))

    hass.states.async_set("sensor.estate_odometer", "41002.70",
                          {"unit_of_measurement": "mi", "device_class": "distance",
                           "friendly_name": "Estate odometer"})
    held = hass.states.get("sensor.estate_odometer")
    await settle(hass)

    new = states(hass)[before:]
    assert len(new) == 1, new
    [line] = new
    base, subject = subject_of(hass)
    report = json.loads(vledger(base, "l0", "validate", "--vehicle", subject, "--json"))
    assert line["v"] in report["versions"]
    assert line["subject"] == subject
    assert UTC_MS.match(line["t"]), line["t"]
    ha_times = (held.last_changed, held.last_updated, held.last_reported)
    assert any(abs(instant(line["t"]) - t) < dt.timedelta(milliseconds=1) for t in ha_times), (
        line["t"], ha_times)
    assert line["role"] == "odometer"
    assert line["entity"] == "sensor.estate_odometer"
    assert line["state"] == "41002.70"
    assert line["unit"] == "mi"


async def test_a_captured_position_keeps_its_role_relevant_attributes_and_no_others(ui):
    """A device tracker reports its position with the attributes a phone or a
    car integration adds: the record keeps where the vehicle is, and none of
    the tracker's own battery, name or icon."""
    hass = ui.hass
    extras = {"battery_level": 63, "friendly_name": "Estate tracker", "icon": "mdi:car"}
    hass.states.async_set("device_tracker.estate", "home",
                          {"latitude": 52.52, "longitude": 13.40, "gps_accuracy": 8, **extras})
    await ui.add_vehicle("Estate", {"Position": "device_tracker.estate"})
    before = len(states(hass))

    hass.states.async_set("device_tracker.estate", "not_home",
                          {"latitude": 52.5301, "longitude": 13.4102, "gps_accuracy": 11,
                           **extras})
    await settle(hass)

    new = states(hass)[before:]
    assert len(new) == 1, new
    [line] = new
    assert (line["role"], line["entity"], line["state"]) == (
        "position", "device_tracker.estate", "not_home")
    attrs = [v for v in line.values() if isinstance(v, dict)]
    assert len(attrs) == 1, line
    assert attrs[0]["latitude"] == pytest.approx(52.5301)
    assert attrs[0]["longitude"] == pytest.approx(13.4102)
    text = json.dumps(line)
    for name in extras:
        assert name not in text, line
    assert "mdi:car" not in text and "Estate tracker" not in text, line


async def test_a_source_that_stops_reporting_is_recorded_with_its_raw_state(ui):
    """A fuel level reported with a trailing zero, then dropping out: each
    record keeps Home Assistant's own string, ``unavailable`` included, the
    reading its unit, and each its time in UTC to the millisecond."""
    hass = ui.hass
    hass.states.async_set("sensor.estate_fuel", "38.5",
                          {"unit_of_measurement": "L", "device_class": "volume"})
    hass.states.async_set("sensor.estate_odometer", "41000",
                          {"unit_of_measurement": "km", "device_class": "distance"})
    await ui.add_vehicle("Estate", {"Odometer": "sensor.estate_odometer",
                                    "Fuel level": "sensor.estate_fuel"})
    before = len(states(hass))

    hass.states.async_set("sensor.estate_fuel", "38.50",
                          {"unit_of_measurement": "L", "device_class": "volume"})
    await settle(hass)
    hass.states.async_set("sensor.estate_fuel", "unavailable", {})
    await settle(hass)

    new = [ln for ln in states(hass)[before:] if ln["entity"] == "sensor.estate_fuel"]
    assert [(ln["role"], ln["state"]) for ln in new] == [
        ("fuel_level", "38.50"), ("fuel_level", "unavailable")], new
    assert new[0]["unit"] == "L"
    assert all(UTC_MS.match(ln["t"]) for ln in new), new
