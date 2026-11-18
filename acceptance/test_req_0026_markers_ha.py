# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0026 in Home Assistant: the integration writes a start marker with a
snapshot of all assigned roles at start, a stop marker at orderly shutdown and
a heartbeat at a fixed interval, 60 minutes by default.

Derived from the requirement and the user guide's *In Home Assistant*: "From
the moment an entry is set up its raw log is written under
``<config>/vledger/``"; "Stopping Home Assistant, unloading or reloading the
entry ends the stream orderly; a crash does not, and the next start shows as a
gap"; saving the options "reloads the entry, which the stream records as stop,
start and the new configuration". The raw log is read back with ``vledger l0
read --vehicle ID`` and its gaps with ``vledger l0 gaps`` (*The raw log:
``vledger l0``*); the snapshot's items are those ``vledger l0 start
--snapshot`` takes, ``{role, entity, state, unit?, since}``, as its help says.
The stop marker's reason is one of ``shutdown``, ``unload`` and ``reload``,
the three ``vledger l0 stop --reason`` offers.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config flow and config entry endpoints over HTTP,
choosing every field by the label the frontend shows — let Home Assistant's
clock run on where an hour has to pass, and read the raw log back with
``vledger`` in a process of its own. Nothing of ``vledger`` or of the
integration is imported. The same requirement with the ``vledger`` command
alone is test_req_0026_markers.py."""

import itertools
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from browser import Browser, open_frontend
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

pytestmark = [pytest.mark.req("REQ-0026")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


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

ENTRIES = "/api/config/config_entries/entry"


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
    """Let capture write what it was handed: its writes run off the event
    loop. The wait is the thread's own, not the event loop's, whose sleep does
    not end while a test holds Home Assistant's clock still."""
    for _ in range(5):
        await hass.async_block_till_done(wait_background_tasks=True)
        time.sleep(0.05)  # noqa: ASYNC251 - the loop's sleep stands with the clock
    await hass.async_block_till_done(wait_background_tasks=True)


def sensor(hass, entity, state, unit=None, **attrs):
    if unit:
        attrs["unit_of_measurement"] = unit
    hass.states.async_set(entity, str(state), attrs)
    return entity


# The roles assigned in the tests, by the label the Roles step shows, with the
# entity and the state each reports when the entry is set up.
ROLES = {
    "Odometer": ("odometer", "sensor.car_odometer", "12345", "km",
                 {"device_class": "distance", "state_class": "total_increasing"}),
    "Position (device tracker)": ("position", "device_tracker.car", "home", None,
                                  {"latitude": 52.52, "longitude": 13.40, "gps_accuracy": 10}),
    "Fuel level": ("fuel_level", "sensor.car_fuel", "41.5", "L", {}),
    "State of charge": ("soc", "sensor.car_soc", "80", "%", {"device_class": "battery"}),
    "Outside temperature": ("outside_temperature", "sensor.car_outside", "12.5", "°C",
                            {"device_class": "temperature"}),
}


async def add_vehicle(ui, name="Family car", roles=ROLES):
    """Add integration → Vehicle Ledger → A vehicle: Name, the roles given
    (each entity reporting first), Parameters with a tank and a battery, and
    any step that follows as shown."""
    for _, entity, state, unit, attrs in roles.values():
        sensor(ui.hass, entity, state, unit, **attrs)
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    await flow.fill({label: entity for label, (_, entity, *_) in roles.items()})
    while flow.step["type"] == "form":
        given = {"Tank capacity": 50, "Net battery capacity": 10} if flow.title == "Parameters" else {}
        await flow.fill({k: v for k, v in given.items() if k in flow.fields()})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = [e for e in await ui.entries() if e["title"] == name]
    return entry["entry_id"]


async def clock(hass, freezer, minutes):
    """Home Assistant's clock run on by so many minutes, in steps of at most
    ten, and what fell due in each step done."""
    while minutes > 0:
        step = min(minutes, 10)
        freezer.tick(timedelta(minutes=step))
        async_fire_time_changed(hass)
        await settle(hass)
        minutes -= step


def log_dir(hass):
    return pathlib.Path(hass.config.path("vledger"))


def subject(hass):
    [d] = sorted(log_dir(hass).glob("vehicle-*"))
    return d.name.removeprefix("vehicle-")


def stream(hass, *more):
    """The one vehicle's raw log, as ``vledger l0 read`` prints it."""
    return jsonl(vledger(log_dir(hass), "l0", "read", "--vehicle", subject(hass), *more))


def kinds(lines):
    return [ln["kind"] for ln in lines if ln["kind"] != "state"]


def assert_snapshot_of_every_role(start, roles=ROLES):
    got = {s["role"]: s for s in start["snapshot"]}
    assert set(got) == {role for role, *_ in roles.values()}, start["snapshot"]
    for role, entity, state, unit, _ in roles.values():
        assert got[role]["entity"] == entity, got[role]
        assert got[role]["state"] == state, got[role]
        if unit:
            assert got[role].get("unit") == unit, got[role]


async def test_setting_up_a_vehicle_starts_its_stream_with_a_snapshot_of_every_role(ui):
    """A vehicle set up with five roles: its stream opens with a start
    marker whose snapshot holds each of the five, with the entity assigned
    and the state and unit it reported at that moment."""
    await add_vehicle(ui)
    lines = stream(ui.hass)
    assert lines[0]["kind"] == "start", lines[0]
    assert_snapshot_of_every_role(lines[0])


async def test_reloading_the_entry_writes_a_stop_and_then_a_start_with_a_fresh_snapshot(ui):
    """The entry reloaded from its menu: a stop marker, then a start marker
    whose snapshot holds every assigned role at the values they report
    now, not those of the first start."""
    entry_id = await add_vehicle(ui)
    sensor(ui.hass, "sensor.car_odometer", "12360", "km")
    sensor(ui.hass, "sensor.car_fuel", "38.0", "L")
    await settle(ui.hass)
    before = stream(ui.hass)

    await ui.post(f"{ENTRIES}/{entry_id}/reload")
    await settle(ui.hass)
    after = stream(ui.hass)[len(before):]

    assert kinds(after)[:2] == ["stop", "start"], kinds(after)
    [start] = [ln for ln in after if ln["kind"] == "start"]
    now = dict(ROLES)
    now["Odometer"] = ("odometer", "sensor.car_odometer", "12360", "km", {})
    now["Fuel level"] = ("fuel_level", "sensor.car_fuel", "38.0", "L", {})
    assert_snapshot_of_every_role(start, now)


async def test_a_reload_s_stop_marker_says_reload(ui):
    """The stop marker a reload writes gives its reason as ``reload``, the
    one of the three ``vledger l0 stop --reason`` offers that says what
    happened, so that a reader of the stream tells a reload from an unload."""
    entry_id = await add_vehicle(ui)
    await ui.post(f"{ENTRIES}/{entry_id}/reload")
    await settle(ui.hass)
    [stop] = stream(ui.hass, "--kind", "stop")
    assert stop["reason"] == "reload", stop


async def test_removing_the_entry_ends_its_stream_with_a_stop(ui):
    """The entry deleted from its menu, which unloads it first: the stream
    it leaves under ``<config>/vledger/`` ends with a stop marker."""
    entry_id = await add_vehicle(ui)
    resp = await ui.client.delete(f"{ENTRIES}/{entry_id}")
    assert resp.status == 200, await resp.text()
    await settle(ui.hass)
    lines = stream(ui.hass)
    assert lines[-1]["kind"] == "stop", lines[-1]
    assert lines[-1]["reason"] == "unload", lines[-1]


async def test_stopping_home_assistant_ends_the_stream_with_a_stop(ui):
    """Home Assistant shut down while the vehicle captures: the last line of
    its stream is a stop marker for the reason ``shutdown``."""
    await add_vehicle(ui)
    await ui.hass.async_stop()
    lines = stream(ui.hass)
    assert lines[-1]["kind"] == "stop", lines[-1]
    assert lines[-1]["reason"] == "shutdown", lines[-1]


async def test_a_heartbeat_is_written_every_60_minutes_by_default(ui, freezer):
    """With the thresholds left as they are, Home Assistant's clock run on
    for three hours: no heartbeat before the first hour is up, then one an
    hour, each 60 minutes after the one before."""
    await add_vehicle(ui)
    [start] = stream(ui.hass, "--kind", "start")

    async def run_on(minutes):
        await clock(ui.hass, freezer, minutes)

    await run_on(59)
    assert stream(ui.hass, "--kind", "heartbeat") == []
    await run_on(1)
    for _ in range(2):
        await run_on(60)
    beats = stream(ui.hass, "--kind", "heartbeat")
    assert len(beats) == 3, beats
    times = [start["t"], *(b["t"] for b in beats)]
    ts = [datetime.fromisoformat(t) for t in times]
    steps = [(b - a).total_seconds() for a, b in itertools.pairwise(ts)]
    assert all(abs(s - 3600) <= 1 for s in steps), steps


async def test_a_stream_kept_by_home_assistant_shows_no_gap_while_it_runs(ui, freezer):
    """Six hours of a vehicle that reports nothing, Home Assistant running
    throughout: the heartbeats alone keep ``vledger l0 gaps`` from finding a
    gap, judged at the end of those hours."""
    await add_vehicle(ui)
    await clock(ui.hass, freezer, 6 * 60)
    beats = stream(ui.hass, "--kind", "heartbeat")
    assert len(beats) == 6, beats
    now = beats[-1]["t"]
    out = vledger(log_dir(ui.hass), "l0", "gaps", "--vehicle", subject(ui.hass),
                  "--now", now, "--json")
    assert json.loads(out) == []
