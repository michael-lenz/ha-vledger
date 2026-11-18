# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0047 in Home Assistant: L1 is stored as files next to L0 and can be
deleted and regenerated at any time. JSON Lines is the L1 of record, written
alike by the live and the batch derivation; CSV is an export produced only by
the CLI and the Home Assistant action.

Derived from the requirement and the user documentation: the user guide's
*In Home Assistant* ("The integration also keeps the derivation on disk, L1
… the moment events complete"; the action **Vehicle Ledger: Recompute**,
``vledger.recompute``, rebuilds it on demand), *Where the data is*
(``<config>/vledger/``, a vehicle's directory ``vehicle-<id>``), *The
derivation on disk: vledger l1* (``derive all --write`` rebuilds ``l1/``
from scratch; ``l1 clean`` "delete l1/ — it is regenerable") and *Exports:
vledger export* (the action **Vehicle Ledger: Export**, ``vledger.export``,
"writes the same files into the instance's media folder,
``media/vledger/<vehicle>/``", by default ``trips.csv``, and answers with
``path``, ``count`` and ``current``); l0-format.md's *Files* (``l0/``,
``receipts.jsonl`` and ``l1/`` side by side in a stream's directory) and its
config line's ``name``; l1-format.md's *Files* and *Exports* ("JSON Lines is
the L1 of record; CSV, JSON and GPX are renderings of it … never by the live
path"; the verb and the action "select and render through the same two
library functions, so neither can produce what the other cannot"). The
command line is the other module, test_req_0047_l1_files.py.

The "live" derivation is the one Home Assistant runs as the vehicle is
driven; "batch" is ``vledger derive all --write`` on a copy of the stream it
captured. The instance's media folder is ``<config>/media``, Home
Assistant's default, and the vehicle's folder in it is its name, Volvo,
slugified: ``volvo``. The manifest's ``derived_at`` says when rather than
from what (l1-format.md, *The manifest*) and is left out where two L1s are
compared.

Home Assistant starts with nothing of the ledger configured. The tests do
what the frontend does — the config flow and the actions over HTTP and the
websocket, every field chosen by the label the frontend shows — set the
odometer as its own integration would, run Home Assistant's clock on, and
read what the ledger made with the ``vledger`` command in a process of its
own on ``<config>/vledger/``. Nothing of ``vledger`` or of the integration
is imported. The harness is the one the other Home Assistant modules copy
(ISSUE-0132)."""

import csv
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from browser import Browser, open_frontend
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

pytestmark = [pytest.mark.req("REQ-0047")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
DOMAIN = "vledger"

NAME, ODOMETER = "Volvo", "sensor.volvo_odometer"
FOLDER = "volvo"                                   # the name, slugified

# The event files l1-format.md names for a vehicle's L1.
EVENT_FILES = ("trips.jsonl", "refuellings.jsonl", "charging-sessions.jsonl")


# --- the vledger command -----------------------------------------------------

def vledger(base, *argv, expect=0):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def jsonl(text):
    return [json.loads(x) for x in text.splitlines() if x.lstrip().startswith("{")]


# --- what the browser does ---------------------------------------------------

@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client,
             hass_ws_client, hass_admin_user):
    """A Home Assistant with its frontend's API and nothing configured for the
    ledger. The configuration directory is a temporary one, so the raw log
    lands under it, in ``<config>/vledger/``, and the media folder is the
    default one, ``<config>/media``."""
    # Home Assistant's core configuration adds every media folder to the
    # directories an integration may write to; the test's own does not.
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    hass.config.allowlist_external_dirs.add(str(tmp_path / "media"))
    client = await open_frontend(hass, tmp_path, hass_client)
    return UI(hass, client, hass_ws_client, hass_admin_user)


class UI(Browser):
    def __init__(self, hass, client, ws_client, user):
        super().__init__(hass, client)
        self.ws_client, self.user = ws_client, user
        self.entry_id = None

    async def login(self):
        """The frontend logged in afresh, as a browser opened after hours
        holds a token of the hour: its websocket, authenticated."""
        refresh = await self.hass.auth.async_create_refresh_token(
            self.user, "https://example.com/app")
        return await self.ws_client(self.hass, self.hass.auth.async_create_access_token(refresh))

    async def action(self, service, data, response=False):
        """*Developer tools → Actions*: Vehicle Ledger: <service>, with
        ``data``, called over the websocket as the frontend calls it, what it
        did let finish, and its answer where one is asked for."""
        ws = await self.login()
        msg = {"type": "call_service", "domain": DOMAIN, "service": service,
               "service_data": data}
        if response:
            msg["return_response"] = True
        await ws.send_json_auto_id(msg)
        answer = await ws.receive_json()
        assert answer["success"], answer
        await settle(self.hass)
        await ws.close()
        return (answer.get("result") or {}).get("response")


# --- the vehicle and its odometer --------------------------------------------

def odometer(hass, km):
    hass.states.async_set(ODOMETER, str(km), {"unit_of_measurement": "km",
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


async def drive(hass, freezer, km, minutes=30):
    """The Volvo drives off, its odometer climbing every ten minutes by
    ``km`` in all."""
    start = int(hass.states.get(ODOMETER).state)
    for i in range(1, minutes // 10 + 1):
        odometer(hass, start + km * i * 10 // minutes)
        await settle(hass)
        await clock(hass, freezer, 10)


async def a_vehicle_with_a_history(ui, freezer):
    """Add integration → A vehicle: Name, the odometer as its one role, the
    further steps as shown; then three drives, parked between them long past
    T_still (30 minutes, the default), so that each is a completed trip."""
    hass = ui.hass
    odometer(hass, 1000)
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": NAME})
    assert flow.title == "Roles", flow.step
    await flow.fill({"Odometer": ODOMETER})
    while flow.step["type"] == "form":
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await settle(hass)
    [entry] = await ui.entries()
    assert entry["state"] == "loaded", entry
    ui.entry_id = entry["entry_id"]
    for km in (12, 25, 7):
        await drive(hass, freezer, km)
        await clock(hass, freezer, 120)


# --- what the user reads back ------------------------------------------------

def config_dir(hass):
    return pathlib.Path(hass.config.config_dir)


def base(hass):
    return pathlib.Path(hass.config.path("vledger"))


def subject(hass):
    [d] = sorted(base(hass).glob("vehicle-*"))
    sid = d.name.removeprefix("vehicle-")
    configs = jsonl(vledger(base(hass), "l0", "read", "--vehicle", sid, "--kind", "config"))
    assert configs[-1]["config"]["name"] == NAME, configs
    return sid


def l1_files(directory):
    """An L1 as it lies on disk, the manifest without the moment it was
    written."""
    out = {}
    for p in sorted(pathlib.Path(directory).iterdir()):
        if p.name == "manifest.json":
            m = json.loads(p.read_text(encoding="utf-8"))
            m.pop("derived_at", None)
            out[p.name] = json.dumps(m, sort_keys=True).encode()
        else:
            out[p.name] = p.read_bytes()
    return out


def csv_files(where):
    where = pathlib.Path(where)
    return sorted(str(p.relative_to(where)) for p in where.rglob("*")
                  if p.is_file() and p.suffix.lower() == ".csv")


# --- the tests ----------------------------------------------------------------

async def test_home_assistant_keeps_l1_as_json_lines_beside_l0(ui, freezer):
    """Driven in Home Assistant, the vehicle's directory under
    ``<config>/vledger/`` holds ``l1/`` beside ``l0/``: a manifest and JSON
    Lines files, ``trips.jsonl`` one line per trip, as ``l1 read`` gives them
    back. The live path writes no CSV, anywhere under the configuration
    directory."""
    await a_vehicle_with_a_history(ui, freezer)
    sid = subject(ui.hass)
    stream = base(ui.hass) / f"vehicle-{sid}"

    beside = sorted(p.name for p in stream.iterdir())
    assert {"l0", "l1"} <= set(beside), beside
    names = sorted(p.name for p in (stream / "l1").iterdir())
    assert {"manifest.json", "trips.jsonl", "periods.jsonl"} <= set(names), names
    assert all(n == "manifest.json" or n.endswith(".jsonl") for n in names), names

    trips = [json.loads(x) for x in
             (stream / "l1" / "trips.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(trips) == 3, trips
    assert all(t["kind"] == "trip" for t in trips), trips
    assert jsonl(vledger(base(ui.hass), "l1", "read", "--vehicle", sid, "--kind", "trip")) == trips
    assert csv_files(config_dir(ui.hass)) == []


async def test_the_live_l1_is_the_l1_the_batch_derivation_writes(ui, freezer, tmp_path_factory):
    """The stream Home Assistant captured, copied without its L1 and derived
    with ``vledger derive all --write``, gives event files and
    ``periods.jsonl`` byte for byte the same as those Home Assistant wrote
    live."""
    await a_vehicle_with_a_history(ui, freezer)
    sid = subject(ui.hass)
    live = base(ui.hass) / f"vehicle-{sid}" / "l1"

    batch = tmp_path_factory.mktemp("batch") / "vledger"
    shutil.copytree(base(ui.hass), batch, ignore=shutil.ignore_patterns("l1"))
    vledger(batch, "derive", "all", "--vehicle", sid, "--write")

    written = l1_files(live)
    derived = l1_files(batch / f"vehicle-{sid}" / "l1")
    for name in (*EVENT_FILES, "periods.jsonl"):
        assert written.get(name) == derived.get(name), name
    assert len(jsonl(derived["trips.jsonl"].decode())) == 3


async def test_l1_deleted_under_home_assistant_is_regenerated(ui, freezer):
    """``l1/`` deleted while Home Assistant runs, the stream left as it is:
    Vehicle Ledger: Recompute writes it again, the same files as before."""
    await a_vehicle_with_a_history(ui, freezer)
    sid = subject(ui.hass)
    l0 = base(ui.hass) / f"vehicle-{sid}" / "l0"
    l1 = base(ui.hass) / f"vehicle-{sid}" / "l1"
    before = l1_files(l1)
    stream = {p.name: p.read_bytes() for p in l0.iterdir()}

    vledger(base(ui.hass), "l1", "clean", "--vehicle", sid)
    await settle(ui.hass)
    assert not l1.exists()
    await ui.action("recompute", {"config_entry_id": ui.entry_id})

    assert l1_files(l1) == before
    assert {p.name: p.read_bytes() for p in l0.iterdir()} == stream


async def test_the_export_action_renders_l1_as_csv_into_the_media_folder(ui, freezer):
    """Vehicle Ledger: Export, as CSV of the trips and of the periods,
    writes ``trips.csv`` and ``periods.csv`` into ``media/vledger/volvo/``,
    and nowhere else: a header and a row per line of the kind's JSON Lines,
    in file order, the same file ``vledger export csv`` prints from the same
    L1. Its answer names the file, the events in it and that L1 is current.
    The data directory holds no CSV, and L1 is the same after the export."""
    await a_vehicle_with_a_history(ui, freezer)
    hass = ui.hass
    sid = subject(hass)
    l1 = base(hass) / f"vehicle-{sid}" / "l1"
    before = l1_files(l1)
    folder = config_dir(hass) / "media" / "vledger" / FOLDER

    for kind, name in (("trip", "trips"), ("period", "periods")):
        answer = await ui.action("export", {"config_entry_id": ui.entry_id, "format": "csv",
                                            "kind": kind}, response=True)
        target = folder / f"{name}.csv"
        assert target.is_file(), (answer, sorted(config_dir(hass).rglob("*.csv")))
        assert pathlib.PurePath(answer["path"]).parts[-4:] == ("media", "vledger", FOLDER,
                                                               f"{name}.csv"), answer

        events = [json.loads(x) for x in
                  (l1 / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]
        text = target.read_text(encoding="utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
        assert [r["kind"] for r in rows] == [kind] * len(events), text
        assert [r["start"] for r in rows] == [e["start"] for e in events], text
        assert answer["count"] == len(events), answer
        assert answer["current"] is True, answer
        assert text == vledger(base(hass), "export", "csv", "--vehicle", sid, "--kind", kind)

    assert csv_files(config_dir(hass)) == [f"media/vledger/{FOLDER}/periods.csv",
                                           f"media/vledger/{FOLDER}/trips.csv"]
    assert l1_files(l1) == before
