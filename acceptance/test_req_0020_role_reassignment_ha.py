# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0020 in Home Assistant: the role assignment can be changed at any time
without loss of history.

Derived from the requirement and the user guide's *In Home Assistant*: roles
are picked in the vehicle's second step; the entry's *Configure* is a menu
over the same steps, pre-filled; saving any of them reloads the entry, "which
the stream records as stop, start and the new configuration"; "an entity that
is gone for good is replaced in the options". The raw log lies under
``<config>/vledger/`` and is read back with ``vledger l0 read --vehicle ID``
(*Where the data is*), the stream's directory named after the subject as in
test_req_0014_vehicles_in_parallel.py.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config and options flow endpoints over HTTP,
choosing every field by the label the frontend shows — and read the raw log
back with ``vledger`` in a process of its own. Nothing of ``vledger`` or of
the integration is imported. The same requirement with the ``vledger``
command alone is test_req_0020_role_reassignment.py."""

import asyncio
import json
import os
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from browser import Browser, open_frontend
from homeassistant.core import HomeAssistant

pytestmark = [pytest.mark.req("REQ-0020")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
OLD, NEW = "sensor.old_odometer", "sensor.new_odometer"


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
    return Browser(hass, await open_frontend(hass, tmp_path, hass_client))


def odometer(hass, entity, km):
    hass.states.async_set(entity, str(km), {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})
    return entity


async def settle(hass):
    """Let capture write what it was handed: its writes run off the event loop."""
    for _ in range(5):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


async def add_vehicle(ui, name, entity):
    """Add integration → Vehicle Ledger → A vehicle: Name, Roles (the
    odometer alone), Parameters left as shown."""
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    await flow.fill({"Odometer": entity})
    assert flow.title == "Parameters", flow.step
    await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = [e for e in await ui.entries() if e["title"] == name]
    return entry["entry_id"]


async def assign_odometer(ui, entry_id, entity):
    """Configure → Roles: the odometer given to another entity, the rest as
    the form shows it, and saved."""
    flow = await ui.options_flow(entry_id)
    await flow.choose("Roles")
    await flow.fill({"Odometer": entity})
    while flow.step["type"] == "form":  # a step that follows, such as the mapping
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)


def stream(hass):
    """The one vehicle's raw log under ``<config>/vledger/``, as ``vledger l0
    read`` prints it."""
    log = pathlib.Path(hass.config.path("vledger"))
    [d] = sorted(log.glob("vehicle-*"))
    subject = d.name.removeprefix("vehicle-")
    return jsonl(vledger(log, "l0", "read", "--vehicle", subject))


def states_of(lines):
    return [(ln["entity"], ln["state"]) for ln in lines if ln["kind"] == "state"]


async def test_reassigning_a_role_in_the_options_keeps_the_raw_log(ui):
    """The odometer's entity replaced under Configure → Roles while the
    vehicle captures: the entry runs on, every line logged before is still
    read back, and the odometer is captured from the new entity from then
    on, while the old one's changes are no longer the vehicle's."""
    entry_id = await add_vehicle(ui, "Family car", odometer(ui.hass, OLD, 12345))
    for km in (12346, 12347):
        odometer(ui.hass, OLD, km)
    await settle(ui.hass)
    before = stream(ui.hass)
    assert states_of(before) == [(OLD, "12346"), (OLD, "12347")]

    odometer(ui.hass, NEW, 12347)
    await assign_odometer(ui, entry_id, NEW)
    assert [e["state"] for e in await ui.entries()] == ["loaded"]

    odometer(ui.hass, NEW, 12348)
    odometer(ui.hass, OLD, 99999)
    await settle(ui.hass)
    after = stream(ui.hass)

    assert after[:len(before)] == before
    assert states_of(after) == [(OLD, "12346"), (OLD, "12347"), (NEW, "12348")]
    kinds = [ln["kind"] for ln in after[len(before):]]
    assert kinds[:3] == ["stop", "start", "config"], kinds


async def test_a_reassigned_role_shows_its_new_entity_in_the_options(ui):
    """After the change the options form shows the new assignment, and the
    stream's latest configuration names it; a second change is just as
    possible, and the history of both earlier entities stays in the log."""
    third = "sensor.third_odometer"
    entry_id = await add_vehicle(ui, "Family car", odometer(ui.hass, OLD, 500))
    odometer(ui.hass, OLD, 501)
    await settle(ui.hass)

    odometer(ui.hass, NEW, 501)
    await assign_odometer(ui, entry_id, NEW)
    odometer(ui.hass, NEW, 502)
    await settle(ui.hass)

    odometer(ui.hass, third, 502)
    await assign_odometer(ui, entry_id, third)
    odometer(ui.hass, third, 503)
    await settle(ui.hass)

    flow = await ui.options_flow(entry_id)
    await flow.choose("Roles")
    assert flow.shown("Odometer") == third

    lines = stream(ui.hass)
    assert states_of(lines) == [(OLD, "501"), (NEW, "502"), (third, "503")]
    configs = [ln["config"]["roles"]["odometer"]["entity"] for ln in lines if ln["kind"] == "config"]
    assert configs == [OLD, NEW, third]
