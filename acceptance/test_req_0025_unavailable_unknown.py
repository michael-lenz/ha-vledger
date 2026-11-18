# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0025: the states ``unavailable`` and ``unknown`` are logged like any
other state.

Derived from the requirement, README.md and the user guide: *In Home
Assistant* (the **Roles** and **Mapping** steps, where *unavailable* and
*unknown* hold the last known state for the derivations; "From the moment an
entry is set up its raw log is written under ``<config>/vledger/``"; "If an
assigned entity disappears, it is logged as ``unavailable``"), *Where the data
is* (a stream named by the vehicle's subject id) and *The raw log: vledger l0*
(``vledger l0 state`` writes a state line, a state "is always a string";
``vledger l0 read --vehicle ID --kind state --role odometer`` prints one role's
state lines as JSON Lines with their ``state``; ``vledger l0 validate`` lists
every problem and exits 1 on an error).

"Like any other state" is read as the user meets it in the raw log: a change
to ``unavailable`` or ``unknown`` adds one state line of its role, as a change
to a value does, holding the state as the string Home Assistant reported, for
a numeric, an enumerated and a position role alike; the mapping, which makes
them hold the last known state, is the derivations' affair and does not reach
the raw log.

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

from browser import Browser, open_frontend
from homeassistant.core import HomeAssistant

pytestmark = [pytest.mark.req("REQ-0025")]


# --- what the browser does ---------------------------------------------------

@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client):
    """A Home Assistant with its frontend's API and nothing configured for the
    ledger. The configuration directory is a temporary one, so the raw log
    lands under it, in ``<config>/vledger/``."""
    return Browser(hass, await open_frontend(hass, tmp_path, hass_client))


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




def run(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own, whatever its
    exit code."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    return subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                          check=False, capture_output=True, text=True, env=env)


# --- the tests ----------------------------------------------------------------

DROPOUTS = ["unavailable", "unknown"]

ROLES = {
    "odometer": ("Odometer", odometer, 12346),
    "fuel_level": ("Fuel level", fuel_level, 29.5),
    "soc": ("State of charge", soc, 79),
    "charging_state": ("Charging state", charging_state, "on"),
}


def dropout(hass, entity, state):
    """The entity reporting ``unavailable`` or ``unknown``, as Home Assistant
    sets it: the state alone, the attributes it keeps for what it is."""
    attrs = {k: v for k, v in hass.states.get(entity).attributes.items()
             if k in ("unit_of_measurement", "device_class", "state_class", "friendly_name")}
    hass.states.async_set(entity, state, attrs)


@pytest.mark.parametrize("state", DROPOUTS)
@pytest.mark.parametrize("role", list(ROLES))
async def test_a_dropout_is_one_line_with_its_state(ui, role, state):
    """A value, then ``unavailable`` or ``unknown``, then a value again: each
    of the three changes adds exactly one state line of the role, and the
    middle one holds the dropout's state verbatim."""
    label, make, value = ROLES[role]
    entity = make(ui.hass)
    roles = {label: entity}
    if role != "odometer":
        roles["Odometer"] = odometer(ui.hass)
    await add_vehicle(ui, roles)
    before = len(states(ui.hass, role))

    make(ui.hass, value)
    await settle(ui.hass)
    dropout(ui.hass, entity, state)
    await settle(ui.hass)
    make(ui.hass, value)
    await settle(ui.hass)

    lines = states(ui.hass, role)[before:]
    assert [ln["state"] for ln in lines] == [str(value), state, str(value)], lines
    assert lines[1]["entity"] == entity, lines[1]


@pytest.mark.parametrize("state", DROPOUTS)
async def test_a_position_dropout_is_one_line_with_its_state(ui, state):
    """A device tracker that goes ``unavailable`` or ``unknown`` and comes
    back: one position line each, the middle one with the dropout's state."""
    entity = position(ui.hass)
    await add_vehicle(ui, {"Position": entity})
    before = len(states(ui.hass, "position"))

    ui.hass.states.async_set(entity, state, {"source_type": "gps"})
    await settle(ui.hass)
    position(ui.hass, 52.5210, 13.4010)
    await settle(ui.hass)

    lines = states(ui.hass, "position")[before:]
    assert [ln["state"] for ln in lines] == [state, "not_home"], lines


async def test_dropouts_in_turn_are_one_line_each(ui):
    """``unavailable`` to ``unknown`` and back is a change like any other:
    every turn adds a line, in order, and none is merged into the one before
    because neither is a value."""
    entity = odometer(ui.hass)
    await add_vehicle(ui, {"Odometer": entity})
    before = len(states(ui.hass, "odometer"))

    sequence = ["unavailable", "unknown", "unavailable", "unknown"]
    for state in sequence:
        dropout(ui.hass, entity, state)
        await settle(ui.hass)
    odometer(ui.hass, 12346)
    await settle(ui.hass)

    lines = states(ui.hass, "odometer")[before:]
    assert [ln["state"] for ln in lines] == [*sequence, "12346"], lines


async def test_a_dropout_reported_again_adds_no_line(ui):
    """Like any other state, a dropout reported again unchanged is no change
    and adds nothing."""
    entity = odometer(ui.hass)
    await add_vehicle(ui, {"Odometer": entity})
    dropout(ui.hass, entity, "unavailable")
    await settle(ui.hass)
    before = states(ui.hass, "odometer")
    assert before[-1]["state"] == "unavailable", before

    for _ in range(3):
        dropout(ui.hass, entity, "unavailable")
        await settle(ui.hass)
    assert states(ui.hass, "odometer") == before


@pytest.mark.parametrize("state", DROPOUTS)
async def test_an_enumerated_roles_dropout_is_logged_as_reported_not_as_mapped(ui, state):
    """The Mapping makes ``unavailable`` and ``unknown`` hold the last known
    state of an enumerated role; the raw log still holds what the source
    reported, not ``on``, ``off`` or what the mapping reads it as."""
    entity = charging_state(ui.hass, "on")
    await add_vehicle(ui, {"Odometer": odometer(ui.hass), "Charging state": entity})
    before = len(states(ui.hass, "charging_state"))

    dropout(ui.hass, entity, state)
    await settle(ui.hass)

    lines = states(ui.hass, "charging_state")[before:]
    assert [ln["state"] for ln in lines] == [state], lines


async def test_an_entity_that_disappears_is_logged_as_unavailable(ui):
    """An assigned entity removed from Home Assistant adds one line of its
    role, with the state ``unavailable``."""
    entity = fuel_level(ui.hass)
    await add_vehicle(ui, {"Odometer": odometer(ui.hass), "Fuel level": entity})
    before = len(states(ui.hass, "fuel_level"))

    ui.hass.states.async_remove(entity)
    await settle(ui.hass)

    lines = states(ui.hass, "fuel_level")[before:]
    assert [ln["state"] for ln in lines] == ["unavailable"], lines
    assert lines[0]["entity"] == entity, lines[0]


@pytest.mark.parametrize("state", DROPOUTS)
def test_the_command_writes_and_reads_a_dropout_like_any_state(tmp_path, state):
    """By hand, ``vledger l0 state --state unavailable`` (or ``unknown``)
    writes a state line as ``--state 123457`` does: it reads back with its
    state verbatim, between the values around it, and the stream validates
    without an error."""
    base = tmp_path
    snapshot = json.dumps([{"role": "odometer", "entity": "sensor.car_odometer",
                            "state": "123456", "unit": "km",
                            "since": "2026-10-08T22:41:10Z"}])
    steps = [
        ["l0", "start", "--vehicle", "a7c1", "--homeassistant", "2026.10.1",
         "--snapshot", snapshot, "--t", "2026-10-09T08:00:00Z"],
        ["l0", "state", "--vehicle", "a7c1", "--role", "odometer",
         "--entity", "sensor.car_odometer", "--state", "123457", "--unit", "km",
         "--t", "2026-10-09T08:01:00Z"],
        ["l0", "state", "--vehicle", "a7c1", "--role", "odometer",
         "--entity", "sensor.car_odometer", "--state", state,
         "--t", "2026-10-09T08:02:00Z"],
        ["l0", "state", "--vehicle", "a7c1", "--role", "odometer",
         "--entity", "sensor.car_odometer", "--state", "123460", "--unit", "km",
         "--t", "2026-10-09T08:03:00Z"],
        ["l0", "stop", "--vehicle", "a7c1", "--reason", "shutdown",
         "--t", "2026-10-09T08:04:00Z"],
    ]
    for argv in steps:
        vledger(base, *argv)

    out = vledger(base, "l0", "read", "--vehicle", "a7c1", "--kind", "state",
                  "--role", "odometer")
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    assert [ln["state"] for ln in lines] == ["123457", state, "123460"], lines

    checked = run(base, "l0", "validate", "--vehicle", "a7c1")
    assert checked.returncode == 0, (checked.stdout, checked.stderr)
