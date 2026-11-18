# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0135 in Home Assistant: the role ``in_use`` takes a binary sensor or an
enumeration, mapped to the domain state in use; a span in use is movement
evidence between distance samples and holds no standstill, and the role
yields no distance, so it is no movement role of its own.

Derived from the requirement ("Role ``in_use``, expected type: binary sensor
or enumeration, mapped to the domain state in use (FZG-05). Enables movement
evidence between distance samples: a span in use holds no standstill … It
yields no distance, so it does not alone meet FZG-02"), ADR-0021 points 1
and 2 ("A span in use is known from its first report … to its last … and is
movement throughout: no standstill lies inside it, whatever the poll
interval") and ADR-0064 point 3, and the user documentation:

- the user guide's *In Home Assistant*: "at least one movement role
  (odometer, position or trip counter) is required"; "**Vehicle in use** is
  for an entity that reports the vehicle being used (a car connection saying
  *car in use*): it keeps a trip whole while the odometer and position only
  arrive at the stop"; the **Mapping** step ticks "the source's values that
  mean … *in use*. *unavailable* and *unknown* hold the last known state;
  any other value means the opposite — … not in use";
- the user guide's *The derivations*: "A trip is the span between two
  standstills … A vehicle reported in use is moving, from the first report
  to the last"; every trip carries "its distance with its source";
- the glossary's *In use* ("it is no movement role, so it neither satisfies
  the mandatory one nor gives a distance") and *Standstill*;
- l0-format.md: an update that repeats a value writes no line and only moves
  its ``last_reported``, which the next line carries as ``reported_before``.

The odometer here is polled slowly: it moves at 06:10 and 06:15, and then
not until 06:55 and 07:00 — forty minutes without a distance sample, longer
than T_still (30 minutes, the default). The vehicle-in-use entity reports
every ten minutes in between. The clause on T_still's lower bound is the
user's setting rule (the user guide: "set T_still to twice its update
interval") and is not shown here.

Home Assistant starts with nothing of the ledger configured. The tests do what
the frontend does — call the config flow and config entry endpoints over
HTTP, choosing every field by the label the frontend shows — run Home
Assistant's clock on, and read the raw log and the trips with the ``vledger``
command in a process of its own on ``<config>/vledger/``. Nothing of
``vledger`` or of the integration is imported."""

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

pytestmark = [pytest.mark.req("REQ-0135")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
ENTRIES = "/api/config/config_entries/entry"
ODOMETER = "sensor.car_odometer"

# The two kinds of source a vehicle-in-use report comes as: a binary sensor,
# whose values are on and off, and an enumeration, whose source names its
# values — a car connection saying car_in_use, as the user guide's example.
SOURCES = {
    "binary-sensor": ("binary_sensor.car_in_use", {}, "on", "off"),
    "enumeration": ("sensor.car_connection",
                    {"device_class": "enum",
                     "options": ["car_in_use", "connected", "disconnected"]},
                    "car_in_use", "connected"),
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


def in_use(hass, source, state):
    """The source reports ``state``; a report that repeats the value it
    holds is an update all the same, as a polled entity's is."""
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


async def play(hass, freezer, script):
    """Each act of ``script`` at its minute, counted from the vehicle's
    setup; then three hours parked, long past T_still."""
    now = 0
    for minute, act in sorted(script, key=lambda s: s[0]):
        await clock(hass, freezer, minute - now)
        now = minute
        act()
        await settle(hass)
    await clock(hass, freezer, 3 * 60)


async def add_vehicle(ui, source):
    """Add integration → A vehicle: Name, Roles with the odometer and the
    source assigned as **Vehicle in use**, Mapping with the source's value
    for in use ticked as the one that means in use, and the Parameters as
    shown."""
    entity, _, yes, no = SOURCES[source]
    odometer(ui.hass, 1000)
    in_use(ui.hass, source, no)
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": "Volvo"})
    assert flow.title == "Roles", flow.step
    await flow.fill({"Odometer": ODOMETER, "Vehicle in use": entity})
    assert flow.title == "Mapping", flow.step
    [label] = [lab for lab in flow.fields() if "use" in str(lab).lower()]
    assert yes in flow.offered(label), flow.offered(label)
    await flow.fill({label: [yes]})
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


def times(lines, role):
    """The times of the state lines of ``role``, in order."""
    return [instant(ln["t"]) for ln in lines if ln["kind"] == "state" and ln["role"] == role]


def slow_odometer(hass):
    """The odometer's four changes: 06:10 and 06:15, then 06:55 and 07:00."""
    return [(10, lambda: odometer(hass, 1005)), (15, lambda: odometer(hass, 1009)),
            (55, lambda: odometer(hass, 1014)), (60, lambda: odometer(hass, 1018))]


# --- the tests ----------------------------------------------------------------

@pytest.mark.parametrize("source", list(SOURCES))
async def test_in_use_is_assigned_and_logged_under_its_role(ui, source):
    """A binary sensor and an enumeration are each taken as the vehicle in
    use: the configuration names the role ``in_use`` with its entity, and the
    source's changes are logged under that role."""
    entity, _, yes, no = SOURCES[source]
    await add_vehicle(ui, source)
    in_use(ui.hass, source, yes)
    await settle(ui.hass)
    in_use(ui.hass, source, no)
    await settle(ui.hass)
    lines = stream(ui.hass)
    [*_, config] = [ln["config"] for ln in lines if ln["kind"] == "config"]
    assert config["roles"]["in_use"]["entity"] == entity, config["roles"]
    logged = [(ln["entity"], ln["state"]) for ln in lines
              if ln["kind"] == "state" and ln["role"] == "in_use"]
    assert logged == [(entity, yes), (entity, no)]


@pytest.mark.parametrize("source", list(SOURCES))
async def test_in_use_alone_is_no_movement_role(ui, source):
    """Roles with the vehicle in use assigned and no odometer, position or
    trip counter: the step does not go on, since at least one movement role
    is required and in use is none."""
    entity, _, _, no = SOURCES[source]
    in_use(ui.hass, source, no)
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": "Volvo"})
    assert flow.title == "Roles", flow.step
    await flow.fill({"Vehicle in use": entity})
    assert flow.step["type"] == "form" and flow.title == "Roles", flow.step
    assert flow.step.get("errors"), flow.step
    assert await ui.entries() == []


async def test_without_in_use_the_odometer_s_silence_is_a_standstill(ui, freezer):
    """The reference the next test moves from: the vehicle-in-use entity
    stays at not in use throughout, and the forty minutes without an
    odometer change are a standstill — two trips."""
    await add_vehicle(ui, "binary-sensor")
    await play(ui.hass, freezer, slow_odometer(ui.hass))
    assert len(trips(ui.hass)) == 2


@pytest.mark.parametrize("source", list(SOURCES))
async def test_a_span_in_use_keeps_the_trip_whole(ui, freezer, source):
    """The vehicle is reported in use from 06:12, again every ten minutes to
    06:52, and not in use at 06:57. The span in use runs from 06:12 to 06:52
    and holds no standstill: one trip, from the odometer's first change to
    its last, whose distance is the odometer's — in use gives none."""
    _, _, yes, no = SOURCES[source]
    await add_vehicle(ui, source)
    hass = ui.hass
    script = slow_odometer(hass)
    script += [(m, lambda: in_use(hass, source, yes)) for m in (12, 22, 32, 42, 52)]
    script += [(57, lambda: in_use(hass, source, no))]
    await play(hass, freezer, script)

    lines = stream(hass)
    [first_km, *_, last_km] = times(lines, "odometer")
    [trip] = trips(hass)
    assert instant(trip["start"]) == first_km
    assert instant(trip["end"]) == last_km
    assert trip["distance_source"] == "odometer", trip
    assert trip["distance_km"] > 0, trip


@pytest.mark.parametrize("between, trips_found", [
    ("unavailable", 1),
    ("unknown", 1),
    ("disconnected", 2),
], ids=["unavailable-holds", "unknown-holds", "unmapped-is-not-in-use"])
async def test_the_source_s_values_are_read_through_its_mapping(
        ui, freezer, between, trips_found):
    """The car connection reports car_in_use at 06:12 and 06:17, then
    ``between`` at 06:20, car_in_use again at 06:53 and connected at 06:57,
    beside the slow odometer. *unavailable* and *unknown* hold in use, so the
    vehicle is in use from 06:12 to 06:53 without a break and no standstill
    lies in the odometer's silence: one trip. ``disconnected``, a value the
    mapping does not list, means not in use: the use ended with its last
    report at 06:17, and nothing moved until 06:53 — thirty-six minutes,
    longer than T_still: two trips."""
    hass = ui.hass
    await add_vehicle(ui, "enumeration")
    script = slow_odometer(hass)
    script += [(m, lambda: in_use(hass, "enumeration", "car_in_use")) for m in (12, 17)]
    script += [(20, lambda: in_use(hass, "enumeration", between))]
    script += [(53, lambda: in_use(hass, "enumeration", "car_in_use"))]
    script += [(57, lambda: in_use(hass, "enumeration", "connected"))]
    await play(hass, freezer, script)
    assert len(trips(hass)) == trips_found
