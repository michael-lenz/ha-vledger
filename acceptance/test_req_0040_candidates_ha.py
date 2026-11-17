# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0040, as a user meets it with charging sessions: a charging session
is a candidate only when its charge point is foreign; a session at a
configured charge point is confirmed by the charge point and waits for no
receipt; a foreign session without a receipt stays visible as unconfirmed,
a charging receipt entered while the session still runs is pending until
the session ends and the receipt meets it (ADR-0061), and none of them is
silently discarded.

Derived from the requirement, ADR-0061, README.md ("Knows your charge
points … Anything else is a foreign charge and asks for a receipt"; "a
detected event without a receipt stays visible as unconfirmed") and the user
guide: *In Home Assistant* (a vehicle's roles and parameters, a charge point
"in one step: name, location and radius on the map, … and the first
tariff"; "Stopping Home Assistant, unloading or reloading the entry ends the
stream orderly"), *What the vehicle shows* ("Charging sessions waiting for a
receipt … a session at a configured charge point never is"), the card ("an
event without a receipt says how it stands: *unconfirmed* or *ambiguous*
while it waits for one, *charge point* when it was at a configured charge
point and needs none"), *The derivations* (every session carries "the charge
point whose radius holds that position — its subject id, or ``foreign``";
"Only finished sessions are printed — one still charging appears once it
ends and the charge point's and the other vehicles' streams have reached
that moment"), *Receipts* (``vledger receipt add charging``; "a receipt for
… a charge still running waits for it, and ``derive match`` lists it as
pending") and *Where the data is* (``<config>/vledger/``, a subject's
directory ``vehicle-<id>`` or ``chargepoint-<id>``).

Home Assistant starts with nothing of the ledger configured. The tests do
what the frontend does — the config flow and the entries' reload over HTTP,
every field chosen by the label the frontend shows — set the source
entities as their own integrations would, and read what the ledger made with
the ``vledger`` command, run in a process of its own on ``<config>/vledger/``.
Nothing of ``vledger`` or of the integration is imported. The harness is the
one the other Home Assistant modules copy (ISSUE-0132)."""

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
pytestmark = [pytest.mark.req("REQ-0040")]


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

    async def entries(self):
        resp = await self.client.get(f"{ENTRIES}?domain={DOMAIN}")
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def config_flow(self):
        labels = await async_get_translations(self.hass, "en", "config", [DOMAIN])
        return Flow(self, FLOW, await self.post(FLOW, {"handler": DOMAIN}), labels)


class Flow:
    """One config flow as the browser shows it: a menu or a form, by its
    labels."""

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

    def field(self, name):
        """A field by the name the user guide gives it, whatever its case:
        the label that reads so, or else the one label that reads so and adds
        a parenthesis ("Position (device tracker)")."""
        labels = [lab for lab in self.fields() if lab]
        found = [lab for lab in labels if lab.casefold() == name.casefold()]
        found = found or [lab for lab in labels
                          if lab.casefold().startswith(name.casefold() + " (")]
        assert len(found) == 1, f"{name!r} not among {sorted(self.fields(), key=str)}"
        return found[0]

    def shown(self, label):
        field = self.fields()[label]
        return field.get("description", {}).get("suggested_value", field.get("default"))

    async def choose(self, label):
        assert self.step["type"] == "menu", self.step
        menu = {self._label("menu_options", o): o for o in self.step["menu_options"]}
        assert label in menu, f"{label!r} not among {sorted(menu, key=str)}"
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}",
                                       {"next_step_id": menu[label]})

    async def fill(self, values):
        """Fill in by label and submit. As in the browser, a field left alone
        is sent with what the form showed in it."""
        fields = self.fields()
        values = {self.field(k): v for k, v in values.items()}
        data = {}
        for label, field in fields.items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}", data)


async def add_charge_point(ui):
    """The home wallbox, in its one step: a name, location and radius on the
    map, and the first tariff with its date."""
    flow = await ui.config_flow()
    await flow.choose("A charge point")
    assert flow.title == "Charge point", flow.step
    await flow.fill({"Name": "Home wallbox",
                     "Location and radius": {"latitude": HOME[0], "longitude": HOME[1],
                                             "radius": 50},
                     "Tariff": 0.32,
                     "Tariff valid from": "2026-01-01"})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)


async def add_vehicle(ui):
    """A plug-in car with the four roles a charging session is read from —
    odometer, position, state of charge and charging state — in the steps of
    the user guide, with its net battery capacity. It comes up loaded."""
    roles = {"Odometer": odometer(ui.hass), "Position": position(ui.hass, *HOME, "home"),
             "State of charge": soc(ui.hass, 40), "Charging state": charging_state(ui.hass)}
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": "Family car"})
    assert flow.title == "Roles", flow.step
    await flow.fill(roles)
    assert flow.title == "Mapping", flow.step
    await flow.fill({"Values that mean charging": ["on"]})
    assert flow.step["type"] == "form" and flow.title == "Parameters", flow.step
    await flow.fill({"Net battery capacity": 10.5})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)
    assert [e["state"] for e in await ui.entries()] == ["loaded", "loaded"]


async def settle(hass):
    """Let the entries come up and capture write what it was handed: its
    writes run off the event loop."""
    for _ in range(10):
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)


# --- the source entities -----------------------------------------------------

HOME = (52.52, 13.40)
AWAY = (52.60, 13.55)


def odometer(hass, km=12345, entity="sensor.car_odometer"):
    hass.states.async_set(entity, str(km), {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})
    return entity


def position(hass, lat, lon, state="not_home", entity="device_tracker.car"):
    hass.states.async_set(entity, state, {"latitude": lat, "longitude": lon,
                                          "gps_accuracy": 10, "source_type": "gps"})
    return entity


def soc(hass, percent, entity="sensor.car_soc"):
    hass.states.async_set(entity, str(percent), {"unit_of_measurement": "%",
                                                 "device_class": "battery"})
    return entity


def charging_state(hass, state="off", entity="binary_sensor.car_charging"):
    hass.states.async_set(entity, state, {"device_class": "battery_charging"})
    return entity


async def charge(ui, where, zone, socs, end=True):
    """The car charges where it stands: plugged in and charging, a fix while
    charging, the state of charge climbing, and — with ``end`` — charging
    over."""
    hass = ui.hass
    charging_state(hass, "on")
    await settle(hass)
    position(hass, *where, zone)
    for percent in socs:
        await settle(hass)
        soc(hass, percent)
    await settle(hass)
    if end:
        charging_state(hass, "off")
        await settle(hass)


async def drive_away(ui, km=12360):
    """A short drive to a place no charge point covers."""
    hass = ui.hass
    odometer(hass, km - 5)
    position(hass, 52.56, 13.48)
    await settle(hass)
    odometer(hass, km)
    position(hass, *AWAY)
    await settle(hass)


async def streams_reach_now(ui):
    """Every entry reloaded from its menu, so that each stream holds a stop
    and a start after what happened: the charge point's stream has reached
    the end of every session."""
    for entry in await ui.entries():
        await ui.post(f"{ENTRIES}/{entry['entry_id']}/reload")
    await settle(ui.hass)


# --- what the user reads back ------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def jsonl(out):
    return [json.loads(x) for x in out.splitlines() if x.lstrip().startswith("{")]


def values(record):
    """Every scalar a record holds, at any depth: the keys an event and a
    match are written under are the design's, not the user guide's."""
    if isinstance(record, dict):
        return [v for x in record.values() for v in values(x)]
    if isinstance(record, list):
        return [v for x in record for v in values(x)]
    return [record]


WAITING = {"unconfirmed", "ambiguous", "pending"}


class Ledger:
    """The data directory under ``<config>/vledger/``, read with ``vledger``
    as the user reads it."""

    def __init__(self, hass):
        self.base = pathlib.Path(hass.config.path("vledger"))

    def subject(self, prefix):
        dirs = sorted(self.base.glob(f"{prefix}-*"))
        assert len(dirs) == 1, dirs
        return dirs[0].name.removeprefix(f"{prefix}-")

    @property
    def car(self):
        return ("--vehicle", self.subject("vehicle"))

    def run(self, *argv):
        return vledger(self.base, *argv)

    def sessions(self):
        return jsonl(self.run("derive", "charging", *self.car))

    def rebuilt(self):
        self.run("derive", "all", *self.car, "--write")

    def l1(self, *flags):
        return jsonl(self.run("l1", "read", *self.car, "--kind", "charging", *flags))

    def waiting(self):
        return self.l1("--last", "50", "--waiting")

    def charging_started(self):
        """The times the charging state turned on, as the raw log has them."""
        lines = jsonl(self.run("l0", "read", *self.car, "--kind", "state",
                               "--role", "charging_state"))
        return [ln["t"] for ln in lines if ln["state"] == "on"]

    def receipt(self, anchor):
        [line] = jsonl(self.run("receipt", "add", "charging", *self.car, "--anchor", anchor,
                                "--energy-kwh", "3.4", "--total-price", "2.21",
                                "--provider", "roaming card"))
        return line["id"]

    def match_lines(self, rid):
        return [x for x in self.run("derive", "match", *self.car).splitlines() if rid in x]


# --- the tests ----------------------------------------------------------------

async def test_only_the_charge_away_from_the_charge_point_waits_for_a_receipt(ui):
    """A charge at the home wallbox and one away from it: the first is the
    wallbox's and needs no receipt, the second is foreign, written to L1 all
    the same, unconfirmed and among the sessions waiting for a receipt."""
    await add_charge_point(ui)
    await add_vehicle(ui)
    await charge(ui, HOME, "home", [50, 60])
    await drive_away(ui)
    await charge(ui, AWAY, "not_home", [70, 80])
    await streams_reach_now(ui)

    ledger = Ledger(ui.hass)
    wallbox = ledger.subject("chargepoint")
    home, away = sorted(ledger.sessions(), key=lambda s: s["start"])
    assert home["chargepoint"] == wallbox and away["chargepoint"] == "foreign", (home, away)

    ledger.rebuilt()
    written = {s["start"]: s for s in ledger.l1()}
    assert set(written) == {home["start"], away["start"]}, written
    assert not WAITING & set(values(written[home["start"]])), written[home["start"]]
    assert "unconfirmed" in values(written[away["start"]]), written[away["start"]]
    assert [s["start"] for s in ledger.waiting()] == [away["start"]]


async def test_a_receipt_confirms_the_foreign_charge_and_nothing_waits_any_more(ui):
    """A charging receipt for the foreign charge meets it: the session names
    the receipt, and no session waits for one."""
    await add_charge_point(ui)
    await add_vehicle(ui)
    await drive_away(ui)
    await charge(ui, AWAY, "not_home", [50, 60])
    await streams_reach_now(ui)

    ledger = Ledger(ui.hass)
    [away] = ledger.sessions()
    assert away["chargepoint"] == "foreign", away
    rid = ledger.receipt(away["start"])
    ledger.rebuilt()

    [written] = ledger.l1()
    assert rid in values(written) and not WAITING & set(values(written)), written
    assert ledger.waiting() == []


async def test_a_receipt_for_a_charge_still_running_is_pending_until_it_ends(ui):
    """The receipt is entered while the foreign charge still runs: it is
    pending in ``derive match``, kept in the receipts, and forms no session
    of its own in L1. Once charging is over it meets the session, which then
    waits for nothing."""
    await add_charge_point(ui)
    await add_vehicle(ui)
    await drive_away(ui)
    await charge(ui, AWAY, "not_home", [50, 60], end=False)

    ledger = Ledger(ui.hass)
    [started] = ledger.charging_started()
    rid = ledger.receipt(started)
    assert rid in ledger.run("receipt", "list", *ledger.car)
    assert any("pending" in x for x in ledger.match_lines(rid)), ledger.match_lines(rid)
    ledger.rebuilt()
    assert all(rid not in values(s) for s in ledger.l1())

    soc(ui.hass, 70)
    await settle(ui.hass)
    charging_state(ui.hass, "off")
    await settle(ui.hass)
    await streams_reach_now(ui)
    ledger.rebuilt()

    [written] = ledger.l1()
    assert rid in values(written) and not WAITING & set(values(written)), written
    assert ledger.waiting() == []
    assert not any("pending" in x for x in ledger.match_lines(rid))
