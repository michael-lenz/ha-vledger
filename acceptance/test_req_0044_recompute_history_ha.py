# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0044 in Home Assistant: the whole history can be fully recomputed at
any time, via a Home Assistant action.

Derived from the requirement and the user documentation: the user guide's
*In Home Assistant* ("The integration also keeps the derivation on disk, L1
… the moment events complete … To rebuild on demand, call the action
**Vehicle Ledger: Recompute** (``vledger.recompute``) — for one vehicle or
charge point, or, left empty, for all", with ``config_entry_id`` optional),
*The derivation on disk: vledger l1* (``l1 read``; ``l1 clean`` "delete l1/ —
it is regenerable"; "L1 holds completed events only: a trip is written once
its standstill has elapsed"), *The derivations* (``derive trips`` prints the
trips found in the stream) and *Where the data is* (``<config>/vledger/``,
a vehicle's directory ``vehicle-<id>``), l0-format.md (a ``config`` line
carries the vehicle's ``name``) and l1-format.md's *Files* (``trips.jsonl``,
one line per trip, in order of start). The command line is the other module,
``test_req_0044_recompute_history.py``.

"The whole history" is every trip each vehicle has driven since it was set
up; "fully recomputed" is L1 holding all of them again after the action,
however it stood before — deleted, or with past trips cut out of its files,
damage that only a rebuild from the stream repairs, and that stays
until the action is called. "At any time" is the
action called while a vehicle is driving: it answers, capture goes on, and
the trip completes into L1 as any other.

Home Assistant starts with nothing of the ledger configured. The tests do
what the frontend does — the config flow and the action over HTTP, every
field chosen by the label the frontend shows — set the odometers as their
own integrations would, run Home Assistant's clock on, and read what the
ledger made with the ``vledger`` command in a process of its own on
``<config>/vledger/``. Nothing of ``vledger`` or of the integration is
imported. The harness is the one the other Home Assistant modules copy
(ISSUE-0132)."""

import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

pytestmark = [pytest.mark.req("REQ-0044")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
ENTRIES = "/api/config/config_entries/entry"

# Two vehicles, each with its own odometer.
CARS = {"Volvo": "sensor.volvo_odometer", "Fiat": "sensor.fiat_odometer"}


# --- the vledger command -----------------------------------------------------

def vledger(base, *argv):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return [json.loads(x) for x in p.stdout.splitlines() if x.lstrip().startswith("{")]


# --- what the browser does ---------------------------------------------------

@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client,
             hass_ws_client, hass_admin_user):
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
    return UI(hass, await hass_client(), hass_ws_client, hass_admin_user)


class UI:
    def __init__(self, hass, client, ws_client, user):
        self.hass, self.client, self.ws_client, self.user = hass, client, ws_client, user
        self.ids = {}

    async def remember_entries(self):
        """The entries' ids by name, read while the session's first token is
        fresh: the clock the tests run on outlives it."""
        self.ids = {e["title"]: e["entry_id"] for e in await self.entries()}

    async def login(self):
        """The frontend logged in afresh, as a browser opened after hours
        holds a token of the hour: its websocket, authenticated."""
        refresh = await self.hass.auth.async_create_refresh_token(
            self.user, "https://example.com/app")
        return await self.ws_client(self.hass, self.hass.auth.async_create_access_token(refresh))

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

    async def recompute(self, **data):
        """*Developer tools → Actions*: Vehicle Ledger: Recompute, with what
        ``data`` names, called over the websocket as the frontend calls it,
        and what the action did let finish."""
        ws = await self.login()
        await ws.send_json_auto_id({"type": "call_service", "domain": DOMAIN,
                                    "service": "recompute", "service_data": data})
        msg = await ws.receive_json()
        assert msg["success"], msg
        await settle(self.hass)
        await ws.close()


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


# --- the vehicles and their sources ------------------------------------------

def odometer(hass, name, km):
    hass.states.async_set(CARS[name], str(km), {"unit_of_measurement": "km",
                                                "device_class": "distance",
                                                "state_class": "total_increasing"})


async def settle(hass):
    """Let capture and the derivation write what they were handed: their
    writes run off the event loop. The wait is the thread's own, not the
    event loop's, whose sleep does not end while a test holds Home
    Assistant's clock still."""
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


async def drive(hass, freezer, name, km, minutes=30):
    """``name`` drives off, its odometer climbing every ten minutes by
    ``km`` in all."""
    start = int(hass.states.get(CARS[name]).state)
    for i in range(1, minutes // 10 + 1):
        odometer(hass, name, start + km * i * 10 // minutes)
        await settle(hass)
        await clock(hass, freezer, 10)


async def park(hass, freezer, hours=2):
    """Every vehicle parked for so long — long past T_still (30 minutes, the
    default) — that its last trip has completed."""
    await clock(hass, freezer, hours * 60)


async def add_vehicle(ui, name):
    """Add integration → A vehicle: Name, the odometer as its one role, and
    the further steps as shown."""
    odometer(ui.hass, name, 1000)
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": name})
    assert flow.title == "Roles", flow.step
    await flow.fill({"Odometer": CARS[name]})
    while flow.step["type"] == "form":
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(ui.hass)


async def two_vehicles_with_a_history(ui, freezer):
    """Both vehicles set up, and each driven three times, parked between
    the drives long enough that each drive is a trip of its own."""
    hass = ui.hass
    for name in CARS:
        await add_vehicle(ui, name)
    assert [e["state"] for e in await ui.entries()] == ["loaded", "loaded"]
    await ui.remember_entries()
    for km in (12, 25, 7):
        for name in CARS:
            await drive(hass, freezer, name, km)
        await park(hass, freezer)


# --- what the user reads back ------------------------------------------------

def base(hass):
    return pathlib.Path(hass.config.path("vledger"))


def subjects(hass):
    """Each vehicle's subject id by its name, as its stream's config line
    gives it."""
    out = {}
    for d in sorted(base(hass).glob("vehicle-*")):
        sid = d.name.removeprefix("vehicle-")
        configs = vledger(base(hass), "l0", "read", "--vehicle", sid, "--kind", "config")
        out[configs[-1]["config"]["name"]] = sid
    assert set(out) == set(CARS), out
    return out


def l1_trips(hass, sid):
    """The trips L1 holds for the vehicle, as ``l1 read`` gives them; none
    where L1 is gone."""
    if not (base(hass) / f"vehicle-{sid}" / "l1").exists():
        return []
    return vledger(base(hass), "l1", "read", "--vehicle", sid, "--kind", "trip")


def stream_trips(hass, sid):
    """The trips ``derive trips`` finds in the vehicle's whole stream."""
    return vledger(base(hass), "derive", "trips", "--vehicle", sid)


def starts(trips):
    return [t["start"] for t in trips]


def cut_trips(hass, sid, keep):
    """Cut every trip but those ``keep`` picks by index out of the vehicle's
    ``l1/trips.jsonl``, leaving the manifest alone."""
    path = base(hass) / f"vehicle-{sid}" / "l1" / "trips.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(x for i, x in enumerate(lines) if keep(i, len(lines))),
                    encoding="utf-8")


# --- the tests ----------------------------------------------------------------

async def test_l1_holds_each_vehicle_s_whole_history_as_it_is_driven(ui, freezer):
    """The ground the other tests stand on: each vehicle's three drives are
    three trips in its stream, and L1, kept live, holds the same three."""
    await two_vehicles_with_a_history(ui, freezer)
    for name, sid in subjects(ui.hass).items():
        found = stream_trips(ui.hass, sid)
        assert len(found) == 3, (name, found)
        assert starts(l1_trips(ui.hass, sid)) == starts(found), name


async def test_the_action_left_empty_recomputes_every_vehicle_s_whole_history(ui, freezer):
    """One vehicle's L1 deleted with ``l1 clean``, the other's with its first
    and last trips cut out of ``trips.jsonl``: the action called with no
    data writes each vehicle's L1 anew, holding every trip of its stream."""
    hass = ui.hass
    await two_vehicles_with_a_history(ui, freezer)
    ids = subjects(hass)
    before = {name: l1_trips(hass, sid) for name, sid in ids.items()}

    vledger(base(hass), "l1", "clean", "--vehicle", ids["Volvo"])
    cut_trips(hass, ids["Fiat"], lambda i, n: 0 < i < n - 1)
    await settle(hass)
    assert l1_trips(hass, ids["Volvo"]) == []
    assert len(l1_trips(hass, ids["Fiat"])) == 1

    await ui.recompute()
    for name, sid in ids.items():
        assert starts(l1_trips(hass, sid)) == starts(stream_trips(hass, sid)), name
        assert l1_trips(hass, sid) == before[name], name


async def test_the_action_for_one_entry_recomputes_that_vehicle_alone(ui, freezer):
    """Both vehicles' L1 lose their first trip; the action called with the
    Volvo's ``config_entry_id`` gives the Volvo its whole history back and
    leaves the Fiat's L1 as it was."""
    hass = ui.hass
    await two_vehicles_with_a_history(ui, freezer)
    ids = subjects(hass)
    before = {name: l1_trips(hass, sid) for name, sid in ids.items()}
    for sid in ids.values():
        cut_trips(hass, sid, lambda i, n: i > 0)
    await settle(hass)
    fiat_cut = l1_trips(hass, ids["Fiat"])
    assert len(fiat_cut) == 2 and len(l1_trips(hass, ids["Volvo"])) == 2

    await ui.recompute(config_entry_id=ui.ids["Volvo"])
    assert l1_trips(hass, ids["Volvo"]) == before["Volvo"]
    assert l1_trips(hass, ids["Fiat"]) == fiat_cut


async def test_the_history_is_recomputed_while_a_vehicle_drives(ui, freezer):
    """The action called while the Volvo is half-way through a fourth drive
    answers, and L1 holds the three trips completed so far. Capture goes on:
    once the Volvo has parked, its fourth trip is in L1 too, beside the
    others, as the stream has them."""
    hass = ui.hass
    await two_vehicles_with_a_history(ui, freezer)
    sid = subjects(hass)["Volvo"]
    await drive(hass, freezer, "Volvo", 10, minutes=20)

    cut_trips(hass, sid, lambda i, n: i > 0)
    await settle(hass)
    assert len(l1_trips(hass, sid)) == 2
    await ui.recompute(config_entry_id=ui.ids["Volvo"])
    assert len(l1_trips(hass, sid)) == 3

    await drive(hass, freezer, "Volvo", 10, minutes=20)
    await park(hass, freezer)
    found = stream_trips(hass, sid)
    assert len(found) == 4, found
    assert starts(l1_trips(hass, sid)) == starts(found)
