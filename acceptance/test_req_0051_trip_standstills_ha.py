# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0051 in Home Assistant: T_still, the standstill that ends a trip, is
30 minutes unless configured, and configurable per vehicle in the entry's
options; the trips of each vehicle are cut by its own.

Derived from the requirement ("A standstill exists when no movement role
(odometer, position, trip counter) changes for at least T_still (default 30
min). T_still is configurable per vehicle (FZG-06)") and from the user
documentation:

- the user guide's *In Home Assistant*: **Options** (the entry's
  *Configure*) is "a menu over the same steps, pre-filled, plus the
  thresholds and time constants"; "Saving any of them reloads the entry,
  which the stream records as stop, start and the new configuration";
- the user guide's *Periods*: "the standstill time after a trip (half an
  hour unless configured otherwise)";
- the user guide's *The derivations*: "A trip is the span between two
  standstills: from the first sample that moved after at least T_still of
  nothing moving, to the last before the next such span";
- l0-format.md's ``config``: ``thresholds.t_still_s``.

The field is found by the label Home Assistant serves the frontend, the
menu's **Thresholds and time constants** and the field that names T_still.

Home Assistant starts with nothing of the ledger configured. The tests do
what the frontend does — call the config and options flow endpoints over
HTTP — run Home Assistant's clock on, and read the raw log and the trips with
the ``vledger`` command in a process of its own on ``<config>/vledger/``.
Nothing of ``vledger`` or of the integration is imported."""

import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from browser import Browser, open_frontend
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

pytestmark = [pytest.mark.req("REQ-0051")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
THRESHOLDS = "Thresholds and time constants"


def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]


@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client):
    """A Home Assistant with its frontend's API and nothing configured for the
    ledger, its configuration directory a temporary one."""
    return Browser(hass, await open_frontend(hass, tmp_path, hass_client))


def odometer(hass, entity, km):
    hass.states.async_set(entity, str(km), {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})


async def settle(hass):
    """Let capture write what it was handed: its writes run off the event
    loop. The wait is the thread's own, since the loop's sleep does not end
    while the test holds Home Assistant's clock still."""
    for _ in range(5):
        await hass.async_block_till_done(wait_background_tasks=True)
        time.sleep(0.05)  # noqa: ASYNC251 - the loop's sleep stands with the clock
    await hass.async_block_till_done(wait_background_tasks=True)


async def clock(hass, freezer, minutes):
    """Home Assistant's clock run on by so many minutes, in steps of at most
    five, and what fell due in each step done."""
    while minutes > 0:
        step = min(minutes, 5)
        freezer.tick(timedelta(minutes=step))
        async_fire_time_changed(hass)
        await settle(hass)
        minutes -= step


async def add_vehicle(ui, name, entity):
    """Add integration → A vehicle with an odometer, everything else as
    shown; the entry's id."""
    odometer(ui.hass, entity, 1000)
    before = {e["entry_id"] for e in await ui.entries()}
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    assert flow.title == "Roles", flow.step
    await flow.fill({"Odometer": entity})
    while flow.step["type"] == "form":
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = [e for e in await ui.entries() if e["entry_id"] not in before]
    assert entry["state"] == "loaded", entry
    return entry["entry_id"]


async def thresholds(ui, entry_id):
    """Configure → Thresholds and time constants, and the label of the field
    that names T_still."""
    flow = await ui.options_flow(entry_id)
    await flow.choose(THRESHOLDS)
    [label] = [lab for lab in flow.fields() if lab and "T_still" in lab]
    return flow, label


def base(hass):
    return pathlib.Path(hass.config.path("vledger"))


def subjects(hass):
    return [d.name.removeprefix("vehicle-") for d in sorted(base(hass).glob("vehicle-*"))]


def t_still_in_l0(hass, vehicle):
    """``thresholds.t_still_s`` of the vehicle's latest ``config`` line, or
    ``None`` where the line leaves it out."""
    configs = [ln for ln in vledger(base(hass), "l0", "read", "--vehicle", vehicle,
                                    "--kind", "config")]
    assert configs, f"no config line for {vehicle}"
    return (configs[-1]["config"].get("thresholds") or {}).get("t_still_s")


async def test_t_still_is_30_minutes_unless_configured(ui):
    """A vehicle just added: its options show T_still at 30 minutes, the
    field's unit seconds, and its raw log's configuration reads so — 1800 s,
    or the threshold left out, which reads as its default."""
    entry_id = await add_vehicle(ui, "Volvo", "sensor.volvo_odometer")
    flow, label = await thresholds(ui, entry_id)
    assert flow.fields()[label]["selector"]["number"].get("unit_of_measurement") == "s"
    assert flow.shown(label) == 1800
    [vehicle] = subjects(ui.hass)
    assert t_still_in_l0(ui.hass, vehicle) in (1800, None)


async def test_t_still_is_set_per_vehicle_in_the_options(ui):
    """Two vehicles; T_still of the first is set to an hour in its options.
    Saving reloads the entry: the first vehicle's raw log records 3600 s and
    its options show it again; the second keeps 30 minutes."""
    first = await add_vehicle(ui, "Volvo", "sensor.volvo_odometer")
    second = await add_vehicle(ui, "Golf", "sensor.golf_odometer")
    vehicles = set(subjects(ui.hass))
    assert len(vehicles) == 2

    flow, label = await thresholds(ui, first)
    await flow.fill({label: 3600})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)

    flow, label = await thresholds(ui, first)
    assert flow.shown(label) == 3600
    flow, label = await thresholds(ui, second)
    assert flow.shown(label) == 1800
    recorded = sorted(t_still_in_l0(ui.hass, v) or 1800 for v in vehicles)
    assert recorded == [1800, 3600]


async def test_each_vehicle_s_trips_are_cut_by_its_own_t_still(ui, freezer):
    """Two vehicles drive alike: the odometer moves at minutes 10, 15 and 20,
    stands for 25 minutes, and moves again at 45, 50 and 55. The first keeps
    T_still at 30 minutes, so the stop is inside one trip; the second's is set
    to 15 minutes in its options, so the stop is a standstill between two
    trips."""
    cars = {"sensor.volvo_odometer": await add_vehicle(ui, "Volvo", "sensor.volvo_odometer"),
            "sensor.golf_odometer": await add_vehicle(ui, "Golf", "sensor.golf_odometer")}
    known = subjects(ui.hass)
    flow, label = await thresholds(ui, cars["sensor.golf_odometer"])
    await flow.fill({label: 900})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)

    km, now = 1000, 0
    for minute in (10, 15, 20, 45, 50, 55):
        await clock(ui.hass, freezer, minute - now)
        now, km = minute, km + 2
        for entity in cars:
            odometer(ui.hass, entity, km)
        await settle(ui.hass)
    await clock(ui.hass, freezer, 2 * 60)

    counts = {}
    for vehicle in known:
        t_still = t_still_in_l0(ui.hass, vehicle) or 1800
        counts[t_still] = len(vledger(base(ui.hass), "derive", "trips", "--vehicle", vehicle))
    assert counts == {1800: 1, 900: 2}, counts
