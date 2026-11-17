# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0137 in Home Assistant: the role ``engine`` takes a binary sensor or an
enumeration, mapped to the domain state running, and its starting refines a
trip's start while its stopping ends none.

Derived from the requirement ("Role ``engine``, expected type: binary sensor
or enumeration, mapped to the domain state running … the engine starting is
a start marker; it stopping is never an end marker"), ADR-0021 point 3 and
ADR-0064 point 3, and the user guide:

- *In Home Assistant*: an engine status "is assigned as **Engine running**,
  not as the ignition: it can start a trip, never end one"; the **Mapping**
  step, "only when an enumerated role was assigned: tick the source's values
  that mean … *engine running*";
- *The raw log*: the stream read back with ``vledger l0 read``, every source
  change a ``state`` line under its role, the configuration a ``config``
  line (l0-format.md);
- *The derivations*: ``vledger derive trips``, "lock and engine the start
  only: the start moves back to the latest … *engine running* within T_still
  before the first moving sample", and ``refined_by`` "names the role that
  set each end".

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config flow and config entry endpoints over
HTTP, choosing every field by the label the frontend shows — run Home
Assistant's clock on, and read the raw log and the trips with the ``vledger``
command in a process of its own on ``<config>/vledger/``. The same role with
the command alone is test_req_0137_engine_role.py. Nothing of ``vledger`` or
of the integration is imported."""

import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

pytestmark = [pytest.mark.req("REQ-0137")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
ENTRIES = "/api/config/config_entries/entry"
ODOMETER = "sensor.car_odometer"

# The two kinds of source an engine status comes as: a binary sensor, whose
# values are on and off, and an enumeration, whose source names its values.
SOURCES = {
    "binary-sensor": ("binary_sensor.car_engine", {"device_class": "running"},
                      "on", "off"),
    "enumeration": ("sensor.car_engine",
                    {"device_class": "enum", "options": ["Running", "Stopped", "Fault"]},
                    "Running", "Stopped"),
}


# --- the vledger command -----------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]


def instant(text):
    """A time the command wrote, compared by the instant it names."""
    return datetime.fromisoformat(text).astimezone(UTC)


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
    # Home Assistant keeps its router open after it starts, since an
    # integration set up later in the UI adds paths to it; the test client
    # starts the server before any entry exists.
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

    async def config_flow(self):
        labels = await async_get_translations(self.hass, "en", "config", [DOMAIN])
        return Flow(self, FLOW, await self.post(FLOW, {"handler": DOMAIN}), labels)


class Flow:
    def __init__(self, ui, path, step, labels):
        self.ui, self.path, self.step, self.labels = ui, path, step, labels

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, "config", "step",
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

    def offered(self, label):
        """The values a field offers to choose from."""
        [(_, config)] = self.fields()[label]["selector"].items()
        return [o["value"] if isinstance(o, dict) else o for o in config.get("options", [])]

    async def choose(self, label):
        assert self.step["type"] == "menu", self.step
        options = {self._label("menu_options", o): o for o in self.step["menu_options"]}
        assert label in options, f"{label!r} not among {sorted(options, key=str)}"
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}",
                                       {"next_step_id": options[label]})

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


# --- the vehicle and its sources ---------------------------------------------

def odometer(hass, km):
    hass.states.async_set(ODOMETER, str(km), {"unit_of_measurement": "km",
                                              "device_class": "distance",
                                              "state_class": "total_increasing"})


def engine(hass, source, state):
    entity, attrs, *_ = SOURCES[source]
    hass.states.async_set(entity, state, attrs)


async def settle(hass):
    """Let capture write what it was handed: its writes run off the event
    loop. The wait is the thread's own, not the event loop's, whose sleep does
    not end while a test holds Home Assistant's clock still."""
    for _ in range(5):
        await hass.async_block_till_done(wait_background_tasks=True)
        time.sleep(0.05)  # noqa: ASYNC251 - the loop's sleep stands with the clock
    await hass.async_block_till_done(wait_background_tasks=True)


async def clock(hass, freezer, minutes):
    """Home Assistant's clock run on by so many minutes, in steps of at most
    ten, and what fell due in each step done."""
    while minutes > 0:
        step = min(minutes, 10)
        freezer.tick(timedelta(minutes=step))
        async_fire_time_changed(hass)
        await settle(hass)
        minutes -= step


async def add_vehicle(ui, source):
    """Add integration → A vehicle: Name, Roles with the odometer and the
    engine status assigned as **Engine running**, Mapping with the source's
    value for running ticked as the one that means the engine is running, and
    the Parameters as shown."""
    entity, _, running, stopped = SOURCES[source]
    odometer(ui.hass, 1000)
    engine(ui.hass, source, stopped)
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": "Hybrid"})
    assert flow.title == "Roles", flow.step
    await flow.fill({"Odometer": ODOMETER, "Engine running": entity})
    assert flow.title == "Mapping", flow.step
    [label] = [lab for lab in flow.fields() if "engine" in str(lab).lower()]
    assert running in flow.offered(label), flow.offered(label)
    await flow.fill({label: [running]})
    while flow.step["type"] == "form":
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    assert [e["state"] for e in await ui.entries()] == ["loaded"]


def base(hass):
    return pathlib.Path(hass.config.path("vledger"))


def subject(hass):
    [d] = sorted(base(hass).glob("vehicle-*"))
    return d.name.removeprefix("vehicle-")


def stream(hass):
    return vledger(base(hass), "l0", "read", "--vehicle", subject(hass))


def trips(hass):
    return vledger(base(hass), "derive", "trips", "--vehicle", subject(hass))


def times(lines, role, state=None):
    """The times of the state lines of ``role`` (with ``state``), in order."""
    return [instant(ln["t"]) for ln in lines if ln["kind"] == "state" and ln["role"] == role
            and (state is None or ln["state"] == state)]


# --- the tests ----------------------------------------------------------------

@pytest.mark.parametrize("source", list(SOURCES))
async def test_the_engine_is_assigned_and_logged_under_its_role(ui, source):
    """A binary sensor and an enumeration are each taken as the engine: the
    configuration names the role ``engine`` with its entity, and the source's
    changes are logged under that role."""
    entity, _, running, stopped = SOURCES[source]
    await add_vehicle(ui, source)
    engine(ui.hass, source, running)
    await settle(ui.hass)
    engine(ui.hass, source, stopped)
    await settle(ui.hass)
    lines = stream(ui.hass)
    [*_, config] = [ln["config"] for ln in lines if ln["kind"] == "config"]
    assert config["roles"]["engine"]["entity"] == entity, config["roles"]
    logged = [(ln["entity"], ln["state"]) for ln in lines
              if ln["kind"] == "state" and ln["role"] == "engine"]
    assert logged == [(entity, running), (entity, stopped)]


@pytest.mark.parametrize("source", list(SOURCES))
async def test_the_engine_starting_starts_the_trip_and_its_stopping_ends_none(
        ui, freezer, source):
    """The engine starts five minutes before the odometer first moves, stops
    while the car drives on electrically, runs again, and stops for good five
    minutes after the last kilometre. One trip: from the engine starting,
    which ``refined_by`` names, to the odometer's last change — neither stop
    ends it or moves its end."""
    _, _, running, stopped = SOURCES[source]
    await add_vehicle(ui, source)
    hass = ui.hass
    await clock(hass, freezer, 10)
    engine(hass, source, running)
    await settle(hass)
    for minutes, act in [(5, lambda: odometer(hass, 1005)),
                         (5, lambda: engine(hass, source, stopped)),
                         (5, lambda: odometer(hass, 1012)),
                         (5, lambda: engine(hass, source, running)),
                         (5, lambda: odometer(hass, 1018)),
                         (5, lambda: engine(hass, source, stopped))]:
        await clock(hass, freezer, minutes)
        act()
        await settle(hass)
    await clock(hass, freezer, 3 * 60)

    lines = stream(hass)
    [first_start, *_] = times(lines, "engine", running)
    [*_, last_km] = times(lines, "odometer")
    [trip] = trips(hass)
    assert instant(trip["start"]) == first_start
    assert (trip.get("refined_by") or {}).get("start") == "engine"
    assert instant(trip["end"]) == last_km
    assert (trip.get("refined_by") or {}).get("end") != "engine"


@pytest.mark.parametrize("source", list(SOURCES))
async def test_the_engine_running_alone_makes_no_trip(ui, freezer, source):
    """The engine runs for twenty minutes and the odometer never changes: no
    trip, since the engine is never the sole criterion."""
    _, _, running, stopped = SOURCES[source]
    await add_vehicle(ui, source)
    await clock(ui.hass, freezer, 10)
    engine(ui.hass, source, running)
    await settle(ui.hass)
    await clock(ui.hass, freezer, 20)
    engine(ui.hass, source, stopped)
    await settle(ui.hass)
    await clock(ui.hass, freezer, 3 * 60)
    assert trips(ui.hass) == []
