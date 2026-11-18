# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0014: each vehicle is its own config entry; several vehicles are
supported in parallel.

Derived from the requirement, README.md ("A vehicle is a config entry"), the
user guide's *In Home Assistant* (a vehicle added in four steps under *Add
integration → Vehicle Ledger*; from the moment an entry is set up its raw log
is written and **Capture status** shows ``running``; unloading or reloading
the entry ends its stream orderly) and *Where the data is* (a stream is named
by the vehicle's subject id, read with ``vledger l0 read --vehicle ID``), with
the file layout the user guide points to in l0-format.md (``<base>/
vehicle-<subject>/``, a new entry a new subject).

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config flow and config entry endpoints over HTTP,
choosing every field by the label the user guide names — and then read the
raw log back as the user would, with the ``vledger`` command in a process of
its own. Nothing of ``vledger`` or of the integration is imported."""

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

pytestmark = [pytest.mark.req("REQ-0014")]


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
    # Which test client starts it depends on the order pytest loads its
    # plugins in: pytest-aiohttp's freezes the router, the Home Assistant
    # plugin's does not (ISSUE-0122).
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

    async def add_vehicle(self, name, odometer):
        """Add integration → Vehicle Ledger → A vehicle: Name, Roles (the
        odometer alone), Parameters left as shown."""
        labels = await async_get_translations(self.hass, "en", "config", [DOMAIN])
        step = await self.post(FLOW, {"handler": DOMAIN})

        def label(*parts):
            return labels.get(".".join(("component", DOMAIN, "config", "step",
                                        step["step_id"], *parts)))

        def fields():
            assert step["type"] == "form", step
            return {label("data", f["name"]): f for f in step["data_schema"]}

        async def fill(values):
            shown = fields()
            missing = set(values) - set(shown)
            assert not missing, f"{sorted(missing)} not among {sorted(shown, key=str)}"
            data = {}
            for lab, field in shown.items():
                value = values.get(lab, field.get("description", {}).get(
                    "suggested_value", field.get("default")))
                if value is not None:
                    data[field["name"]] = value
            return await self.post(f"{FLOW}/{step['flow_id']}", data)

        assert step["type"] == "menu", step
        menu = {label("menu_options", o): o for o in step["menu_options"]}
        assert "A vehicle" in menu, menu
        step = await self.post(f"{FLOW}/{step['flow_id']}", {"next_step_id": menu["A vehicle"]})
        step = await fill({"Name": name})
        step = await fill({"Odometer": odometer})
        assert step["type"] == "form", step   # Parameters; no Mapping for an odometer
        step = await fill({})
        assert step["type"] == "create_entry", step
        await self.hass.async_block_till_done()
        return step

    async def capture_status(self):
        """Capture status of every vehicle, by the device's name the entity
        is shown under, once a first rebuild of L1 has finished."""
        for _ in range(200):
            status = {s.attributes["friendly_name"].removesuffix(" Capture status"): s.state
                      for s in self.hass.states.async_all("sensor")
                      if s.attributes.get("friendly_name", "").endswith(" Capture status")}
            if "recomputing" not in status.values():
                return status
            await asyncio.sleep(0.05)
            await self.hass.async_block_till_done()
        return status


def odometer(hass, entity, km):
    hass.states.async_set(entity, str(km), {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})
    return entity


# --- what the user reads back ------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def streams(hass):
    """Every vehicle's stream under ``<config>/vledger/``, by the name its
    configuration gives the vehicle: the lines ``vledger l0 read`` prints."""
    base = pathlib.Path(hass.config.path("vledger"))
    found = {}
    for d in sorted(base.glob("vehicle-*")):
        subject = d.name.removeprefix("vehicle-")
        lines = [json.loads(x) for x in vledger(base, "l0", "read", "--vehicle", subject).splitlines()
                 if x.strip()]
        names = {ln["config"]["name"] for ln in lines if ln["kind"] == "config"}
        assert len(names) == 1, (subject, names)
        found[names.pop()] = {"subject": subject, "lines": lines}
    return found


def states_of(stream):
    return [(ln["entity"], ln["state"]) for ln in stream["lines"] if ln["kind"] == "state"]


async def settle(hass):
    """Let capture write what it was handed: its writes run off the event loop."""
    for _ in range(5):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


def subjects_on_disk(hass):
    """The subjects whose stream directory is under ``<config>/vledger/``."""
    return {d.name.removeprefix("vehicle-")
            for d in pathlib.Path(hass.config.path("vledger")).glob("vehicle-*")}


def has_streams(*names):
    """Whether a stream of each vehicle named reads back, its configuration
    line among its lines."""
    def ready(hass):
        try:
            return set(names) <= set(streams(hass))
        except AssertionError:
            return False
    return ready


async def written(hass, ready):
    """Settle until ``ready(hass)`` holds, or give up after a few seconds.

    A vehicle's first lines are handed to capture as its entry is set up and
    written off the event loop afterwards, so its stream may not be on disk
    yet when the entry already shows as loaded (ISSUE-0161)."""
    for _ in range(40):
        await settle(hass)
        if ready(hass):
            return


# --- the tests ----------------------------------------------------------------

async def test_each_vehicle_added_is_a_config_entry_of_its_own(ui):
    """Adding a vehicle twice gives two entries of Vehicle Ledger, each named
    after its vehicle, both loaded side by side."""
    await ui.add_vehicle("Family car", odometer(ui.hass, "sensor.family_odometer", 12345))
    await ui.add_vehicle("Van", odometer(ui.hass, "sensor.van_odometer", 80000))

    entries = await ui.entries()
    assert sorted((e["title"], e["state"]) for e in entries) == [
        ("Family car", "loaded"), ("Van", "loaded")]
    assert len({e["entry_id"] for e in entries}) == 2


async def test_several_vehicles_capture_in_parallel(ui):
    """Three vehicles run at once: each shows Capture status ``running``, and
    each has a raw log of its own, under a subject of its own, holding its own
    odometer's changes and nobody else's."""
    cars = {"Family car": "sensor.family_odometer",
            "Van": "sensor.van_odometer",
            "Hybrid": "sensor.hybrid_odometer"}
    for i, (name, entity) in enumerate(cars.items()):
        await ui.add_vehicle(name, odometer(ui.hass, entity, 10000 * (i + 1)))

    assert await ui.capture_status() == {name: "running" for name in cars}

    # Interleaved changes, as several cars driving at the same time report them.
    for step in range(1, 4):
        for i, entity in enumerate(cars.values()):
            odometer(ui.hass, entity, 10000 * (i + 1) + step)
    await settle(ui.hass)

    found = streams(ui.hass)
    assert set(found) == set(cars)
    assert len({s["subject"] for s in found.values()}) == len(cars)
    for i, (name, entity) in enumerate(cars.items()):
        own = [(entity, str(10000 * (i + 1) + step)) for step in range(1, 4)]
        assert states_of(found[name]) == own, name
        assert {ln["subject"] for ln in found[name]["lines"]} == {found[name]["subject"]}


async def test_reloading_one_vehicle_leaves_the_others_capturing(ui):
    """Reloading one vehicle's entry ends and restarts its stream alone: the
    other vehicle's stream records no stop and goes on capturing throughout."""
    await ui.add_vehicle("Family car", odometer(ui.hass, "sensor.family_odometer", 12345))
    await ui.add_vehicle("Van", odometer(ui.hass, "sensor.van_odometer", 80000))
    await ui.capture_status()
    entry = {e["title"]: e["entry_id"] for e in await ui.entries()}

    await ui.post(f"{ENTRIES}/{entry['Family car']}/reload")
    await ui.hass.async_block_till_done()
    odometer(ui.hass, "sensor.van_odometer", 80001)
    odometer(ui.hass, "sensor.family_odometer", 12346)
    await settle(ui.hass)

    assert sorted((e["title"], e["state"]) for e in await ui.entries()) == [
        ("Family car", "loaded"), ("Van", "loaded")]
    found = streams(ui.hass)
    family = [(ln["kind"], ln.get("reason")) for ln in found["Family car"]["lines"]]
    # An orderly end: l0-format.md names unload and reload as reasons, and
    # neither document says which a reload from the UI records.
    assert [k for k, _ in family].count("start") == 2, family
    assert [r for k, r in family if k == "stop"] in (["reload"], ["unload"]), family
    van = [ln["kind"] for ln in found["Van"]["lines"]]
    assert "stop" not in van and van.count("start") == 1, van
    assert states_of(found["Van"]) == [("sensor.van_odometer", "80001")]
    assert states_of(found["Family car"]) == [("sensor.family_odometer", "12346")]


async def test_removing_one_vehicle_leaves_the_others(ui):
    """Deleting one vehicle's entry ends that vehicle alone: the other entry
    stays loaded and its stream goes on; a vehicle added again is a new
    subject, beside the old stream."""
    await ui.add_vehicle("Family car", odometer(ui.hass, "sensor.family_odometer", 12345))
    await ui.add_vehicle("Van", odometer(ui.hass, "sensor.van_odometer", 80000))
    await ui.capture_status()
    entry = {e["title"]: e["entry_id"] for e in await ui.entries()}
    await written(ui.hass, has_streams("Family car", "Van"))
    before = {name: s["subject"] for name, s in streams(ui.hass).items()}
    assert set(before) == {"Family car", "Van"}, before

    resp = await ui.client.delete(f"{ENTRIES}/{entry['Family car']}")
    assert resp.status == 200, await resp.text()
    await ui.hass.async_block_till_done()
    odometer(ui.hass, "sensor.van_odometer", 80001)
    await settle(ui.hass)

    assert [(e["title"], e["state"]) for e in await ui.entries()] == [("Van", "loaded")]
    assert states_of(streams(ui.hass)["Van"]) == [("sensor.van_odometer", "80001")]

    await ui.add_vehicle("Family car", odometer(ui.hass, "sensor.family_odometer", 12400))
    await written(ui.hass, lambda hass: len(subjects_on_disk(hass)) == 3)
    subjects = subjects_on_disk(ui.hass)
    assert before["Family car"] in subjects and before["Van"] in subjects
    assert len(subjects) == 3
