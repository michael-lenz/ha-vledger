# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0028: the energy meter of a charge point is logged in a stream of its
own per charge point, not in a vehicle's stream.

Derived from the requirement, README.md ("one stream per vehicle and per
charge point"; "Home, work, anywhere fixed: position, radius, tariff,
optionally a meter") and the user guide's *In Home Assistant* — a charge point
is added in one step with "an optional energy meter entity", and is changed
under its entry's *Configure*, which offers "the meter"; from the moment an
entry is set up its raw log is written under ``<config>/vledger/``; saving
an option "reloads the entry, which the stream records as stop, start and the
new configuration" — and *Where the data is*: a stream is named by what it
belongs to, ``--vehicle ID`` or ``--chargepoint ID``, the id being the name of
the subject's directory without its prefix, ``vehicle-<id>`` or
``chargepoint-<id>``, read back with ``vledger l0 read``.

The user documentation names no key a meter reading is filed under, so a
reading is found as a state line (kind ``state``, as ``vledger l0 state``
writes it) of the meter's entity, and "not in a vehicle's stream" as: no line
of any vehicle's stream holds the meter's entity id anywhere.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config and options flow endpoints over HTTP,
choosing every field by the label the frontend shows — and read the raw log
back with ``vledger`` in a process of its own. Nothing of ``vledger`` or of
the integration is imported. The harness is the one of
test_req_0027_configuration_state.py, copied, as every module keeps its own
(ISSUE-0132)."""

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

pytestmark = [pytest.mark.req("REQ-0028")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"
ENTRIES = "/api/config/config_entries/entry"

ODOMETER, CHARGING = "sensor.car_odometer", "sensor.car_charging"
HOME_METER, WORK_METER = "sensor.home_wallbox_energy", "sensor.work_wallbox_energy"
HOME = {"latitude": 51.4437, "longitude": 7.1413, "radius": 35}
WORK = {"latitude": 51.4556, "longitude": 7.0116, "radius": 60}


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
    # Home Assistant's own server keeps its router open after it starts, since
    # an integration set up later in the UI adds paths to it. The test client
    # starts the server here, before any entry exists: the router is kept open
    # as Home Assistant keeps it (ISSUE-0122).
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


def meter(hass, entity, kwh):
    """An energy meter as a wallbox integration reports it."""
    hass.states.async_set(entity, str(kwh), {"unit_of_measurement": "kWh",
                                             "device_class": "energy",
                                             "state_class": "total_increasing"})
    return entity


async def add_vehicle(ui, name="Family car"):
    """Add integration → Vehicle Ledger → A vehicle: Name; Roles, the odometer
    and the charging state; Mapping, *charging* means charging; Parameters, a
    plug-in hybrid's petrol, tank and net battery capacity."""
    ui.hass.states.async_set(ODOMETER, "12345", {"unit_of_measurement": "km",
                                                 "device_class": "distance",
                                                 "state_class": "total_increasing"})
    ui.hass.states.async_set(CHARGING, "not_charging", {})
    flow = await ui.flow(FLOW, "config", DOMAIN)
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    await flow.fill({"Odometer": ODOMETER, "Charging state": CHARGING})
    while flow.step["type"] == "form" and "Values that mean charging" in flow.fields():
        await flow.fill({"Values that mean charging": ["charging"]})
    await flow.fill({"Fuel": "petrol", "Tank capacity": 42, "Net battery capacity": 10.5})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)


async def add_chargepoint(ui, name, where, meter_entity=None):
    """Add integration → Vehicle Ledger → A charge point: name, location and
    radius, the energy meter where one is given, and the first tariff."""
    flow = await ui.flow(FLOW, "config", DOMAIN)
    await flow.choose("A charge point")
    values = {"Name": name, "Location and radius": where,
              "Tariff": 0.31, "Tariff valid from": "2026-01-01"}
    if meter_entity:
        values["Energy meter (optional)"] = meter_entity
    await flow.fill(values)
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    [entry] = [e for e in await ui.entries() if e["title"] == name]
    assert entry["state"] == "loaded", entry
    return entry["entry_id"]


# --- what the user reads back ------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def streams(hass, kind):
    """Every vehicle's or charge point's stream under ``<config>/vledger/``,
    by its subject id: the lines ``vledger l0 read`` prints."""
    log = pathlib.Path(hass.config.path("vledger"))
    found = {}
    for d in sorted(log.glob(f"{kind}-*")):
        subject = d.name.removeprefix(f"{kind}-")
        found[subject] = [json.loads(x) for x in
                          vledger(log, "l0", "read", f"--{kind}", subject).splitlines()
                          if x.strip()]
    return found


def readings(lines, entity):
    """The states the entity's state lines hold, in the stream's order."""
    return [ln["state"] for ln in lines if ln["kind"] == "state" and ln.get("entity") == entity]


def values_in(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from values_in(v)
    elif isinstance(node, list):
        for v in node:
            yield from values_in(v)
    else:
        yield node


def mentions(lines, value):
    """The lines that hold the value anywhere, at any depth."""
    return [ln for ln in lines if value in list(values_in(ln))]


def holding(found, value):
    """The one stream among those found whose lines hold the value."""
    hits = [s for s, lines in found.items() if mentions(lines, value)]
    assert len(hits) == 1, (value, hits)
    return found[hits[0]]


async def read_meter(ui, entity, values):
    for kwh in values:
        meter(ui.hass, entity, kwh)
        await settle(ui.hass)


# --- the tests ----------------------------------------------------------------

async def test_a_charge_point_s_meter_is_logged_in_the_charge_point_s_own_stream(ui):
    """A charge point set up with an energy meter has a stream of its own,
    under ``chargepoint-<id>``, read with ``vledger l0 read --chargepoint``;
    every change of the meter is a state line there, in order."""
    await add_chargepoint(ui, "Home wallbox", HOME, meter(ui.hass, HOME_METER, 1520.4))
    await read_meter(ui, HOME_METER, [1523.1, 1527.85, 1530.0])

    found = streams(ui.hass, "chargepoint")
    assert len(found) == 1, found
    [lines] = found.values()
    assert readings(lines, HOME_METER)[-3:] == ["1523.1", "1527.85", "1530.0"]
    assert not streams(ui.hass, "vehicle")


async def test_the_meter_is_in_no_vehicle_s_stream_while_a_vehicle_charges(ui):
    """A vehicle and a charge point with a meter, the vehicle charging while the
    meter counts up: the meter's readings are in the charge point's stream, and
    no line of the vehicle's stream holds the meter, while the vehicle's own
    roles stay in its own stream and out of the charge point's."""
    await add_vehicle(ui)
    await add_chargepoint(ui, "Home wallbox", HOME, meter(ui.hass, HOME_METER, 1520.4))

    ui.hass.states.async_set(CHARGING, "charging", {})
    await settle(ui.hass)
    await read_meter(ui, HOME_METER, [1522.0, 1524.5, 1527.25])
    ui.hass.states.async_set(CHARGING, "not_charging", {})
    await settle(ui.hass)

    [vehicle] = streams(ui.hass, "vehicle").values()
    [chargepoint] = streams(ui.hass, "chargepoint").values()
    assert readings(chargepoint, HOME_METER)[-3:] == ["1522.0", "1524.5", "1527.25"]
    assert readings(vehicle, CHARGING)[-2:] == ["charging", "not_charging"]
    assert mentions(vehicle, HOME_METER) == []
    assert mentions(chargepoint, CHARGING) == []
    assert mentions(chargepoint, ODOMETER) == []


async def test_each_charge_point_s_meter_has_a_stream_of_its_own(ui):
    """Two charge points, each with a meter, beside a vehicle: two charge point
    streams, each holding its own meter's readings and not the other's, and
    neither meter in the vehicle's stream."""
    await add_vehicle(ui)
    await add_chargepoint(ui, "Home wallbox", HOME, meter(ui.hass, HOME_METER, 1520.4))
    await add_chargepoint(ui, "Work wallbox", WORK, meter(ui.hass, WORK_METER, 88.0))

    meter(ui.hass, HOME_METER, 1525.0)
    meter(ui.hass, WORK_METER, 96.5)
    await settle(ui.hass)
    meter(ui.hass, WORK_METER, 101.25)
    meter(ui.hass, HOME_METER, 1531.5)
    await settle(ui.hass)

    found = streams(ui.hass, "chargepoint")
    assert len(found) == 2, sorted(found)
    home, work = holding(found, HOME_METER), holding(found, WORK_METER)
    assert home is not work
    assert readings(home, HOME_METER)[-2:] == ["1525.0", "1531.5"]
    assert readings(work, WORK_METER)[-2:] == ["96.5", "101.25"]
    assert mentions(home, WORK_METER) == [] and mentions(work, HOME_METER) == []

    [vehicle] = streams(ui.hass, "vehicle").values()
    assert mentions(vehicle, HOME_METER) == [] and mentions(vehicle, WORK_METER) == []


async def test_a_meter_added_under_configure_is_logged_in_the_same_charge_point_stream(ui):
    """A charge point set up without a meter is given one under Configure →
    Energy meter: the entry reloads, and the meter's readings from then on are
    in that charge point's one stream — the stream it had before goes on — and
    in no vehicle's."""
    await add_vehicle(ui)
    entry_id = await add_chargepoint(ui, "Home wallbox", HOME)
    [(subject, before)] = streams(ui.hass, "chargepoint").items()
    assert mentions(before, HOME_METER) == []

    meter(ui.hass, HOME_METER, 1520.4)
    flow = await ui.flow(OPTIONS, "options", entry_id)
    await flow.choose("Energy meter")
    await flow.fill({"Energy meter entity (empty: none)": HOME_METER})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    await read_meter(ui, HOME_METER, [1524.0, 1529.5])

    found = streams(ui.hass, "chargepoint")
    assert list(found) == [subject]
    after = found[subject]
    assert after[:len(before)] == before
    assert readings(after, HOME_METER)[-2:] == ["1524.0", "1529.5"]
    [vehicle] = streams(ui.hass, "vehicle").values()
    assert mentions(vehicle, HOME_METER) == []
