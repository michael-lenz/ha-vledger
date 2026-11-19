# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0050: when a full recomputation runs at startup, and what it must not
block.

Derived from the requirement and the user documentation:

- the user guide's *In Home Assistant*: "At startup it checks whether L1 has
  to be rebuilt — none yet, another version, changed configuration or
  receipts — and if so rebuilds it while capture goes on; meanwhile
  **Capture status** shows ``recomputing``"; "From the moment an entry is
  set up its raw log is written under ``<config>/vledger/``"; a charge point
  is added in one step with its first tariff, and its *Configure* offers
  "*add a tariff from a date*"; saving a vehicle's options "reloads the
  entry, which the stream records as stop, start and the new
  configuration"; the stop line says ``unload`` "when it is deleted or
  disabled";
- *What the vehicle shows*: the derived entities, *unavailable* while L1 is
  rebuilt — **Last trip** for every vehicle, the others as the table gives
  them;
- *Where the data is* (``vehicle-<id>``), *The raw log* (``l0 read``),
  *The receipts* (``receipt add``) and *The derivation on disk*
  (``l1 status`` exits 1 when a rebuild is due);
- l1-format.md, the format document: ``l1/manifest.json`` and its
  ``vledger`` key, the library version the L1 was derived by; and "a
  rebuild replaces the directory atomically: everything is derived into
  ``l1.tmp/``, which is then renamed into place".

"Startup" is an entry's setup: Home Assistant's start, an entry added, or
one enabled again after it was disabled in *Settings → Devices & services*
— each is where the stream gets its start line. A change made while the
vehicle's entry is disabled is one "since the last L1 state".

What a recomputation shows while it runs is read while it runs: the tests
hold the rebuild at its last step, the rename of ``l1.tmp/`` into place,
until they have looked, and then let it finish. A rebuild that ran on the
event loop could not be held so without holding Home Assistant with it;
the hold says so instead, and the test fails. While the rebuild is held, the
frontend's API answers, the capture status and the derived entities are
read, and a source that changes is found in the raw log.

Home Assistant starts with nothing of the ledger configured. The tests do
what the frontend does — the config and options flows and the entries' menu
over HTTP and its websocket, every field chosen by the label the frontend
shows — and read the files with the ``vledger`` command in a process of its
own. Nothing of ``vledger`` or of the integration is imported."""

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from browser import Browser, open_frontend
from homeassistant.core import HomeAssistant

pytestmark = [pytest.mark.req("REQ-0050")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
NAME, CHARGE_POINT = "Family car", "Home wallbox"
ODOMETER = "sensor.car_odometer"
HOME = {"latitude": 52.5200, "longitude": 13.4000, "radius": 50}

# The derived entities of *What the vehicle shows*, by the name the user
# guide gives them; which a vehicle has depends on its fuel and battery.
DERIVED = ("Last trip", "Last refuelling", "Last charging session",
           "Last trip fuel consumed", "Last trip fuel per 100 km",
           "Last trip battery energy", "Last trip battery energy per 100 km",
           "Waiting for a receipt", "Refuellings waiting for a receipt",
           "Charging sessions waiting for a receipt")

WAIT_S = 30


# --- holding a rebuild ---------------------------------------------------------

class Hold:
    """While armed, the rename of a vehicle's ``l1.tmp/`` into place waits
    until ``let_go``: the rebuild is held at its last step, everything
    derived and nothing swapped in. A rename on the event loop's own thread
    is not held, only remembered."""

    def __init__(self, loop_thread):
        self.loop_thread = loop_thread
        self.armed = False
        self.reached = threading.Event()
        self.go = threading.Event()
        self.on_loop = []

    def arm(self):
        self.reached.clear()
        self.go.clear()
        self.armed = True

    def let_go(self):
        self.armed = False
        self.go.set()

    def _is_rebuild(self, src):
        try:
            path = pathlib.Path(os.fsdecode(os.fspath(src)))
        except TypeError:
            return False
        return path.name == "l1.tmp" and path.parent.name.startswith("vehicle-")

    def wrap(self, real):
        def renamed(src, dst, *args, **kwargs):
            if self.armed and self._is_rebuild(src):
                if threading.get_ident() == self.loop_thread:
                    self.on_loop.append(os.fsdecode(os.fspath(src)))
                else:
                    self.reached.set()
                    self.go.wait(WAIT_S)
            return real(src, dst, *args, **kwargs)
        return renamed

    async def until_reached(self):
        """Until a rebuild has come to its last step, the event loop running
        meanwhile."""
        for _ in range(WAIT_S * 20):
            if self.reached.is_set() or self.on_loop:
                break
            await asyncio.sleep(0.05)
        assert not self.on_loop, f"L1 swapped in on the event loop: {self.on_loop}"
        assert self.reached.is_set(), "no rebuild of the vehicle's L1 started"


@pytest.fixture
async def hold(monkeypatch):
    h = Hold(threading.get_ident())
    monkeypatch.setattr(os, "rename", h.wrap(os.rename))
    monkeypatch.setattr(os, "replace", h.wrap(os.replace))
    yield h
    h.let_go()


# --- what the browser does ---------------------------------------------------

@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client,
             hass_ws_client, hold):
    """A Home Assistant with its frontend's API and nothing configured for the
    ledger, its configuration directory a temporary one."""
    client = await open_frontend(hass, tmp_path, hass_client)
    return UI(hass, client, await hass_ws_client(hass))


class UI(Browser):
    def __init__(self, hass, client, ws):
        super().__init__(hass, client)
        self.ws = ws

    async def entry_id(self, title):
        [entry] = [e for e in await self.entries() if e["title"] == title]
        return entry["entry_id"]

    async def disable(self, title, disabled=True):
        """The entry's menu: Disable, or Enable again."""
        await self.ws.send_json_auto_id({"type": "config_entries/disable",
                                         "entry_id": await self.entry_id(title),
                                         "disabled_by": "user" if disabled else None})
        msg = await self.ws.receive_json()
        assert msg["success"], msg

    def sensors(self, device):
        """The device's sensors by the name the frontend shows under it."""
        prefix = f"{device} "
        return {s.attributes["friendly_name"].removeprefix(prefix): s.state
                for s in self.hass.states.async_all("sensor")
                if s.attributes.get("friendly_name", "").startswith(prefix)}

    def capture_status(self):
        return self.sensors(NAME).get("Capture status")

    def derived(self):
        shown = self.sensors(NAME)
        found = {name: shown[name] for name in DERIVED if name in shown}
        assert "Last trip" in found, sorted(shown)
        return found


async def settle(ui):
    """Until the vehicle's capture runs and its L1 is due for no rebuild."""
    for _ in range(WAIT_S * 10):
        await asyncio.sleep(0.1)
        if ui.capture_status() == "running" and (
                await asyncio.to_thread(l1_status, ui.hass)).returncode == 0:
            return
    raise AssertionError(f"Capture status {ui.capture_status()}, "
                         f"l1 status {l1_status(ui.hass).stdout}")


def odometer(hass, km):
    hass.states.async_set(ODOMETER, str(km), {"unit_of_measurement": "km",
                                              "device_class": "distance",
                                              "state_class": "total_increasing"})


async def add_vehicle(ui):
    """Add integration → A vehicle: its name, the odometer as its one role,
    petrol, and the rest as shown."""
    odometer(ui.hass, 12345)
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": NAME})
    await flow.fill({"Odometer": ODOMETER})
    while flow.step["type"] == "form" and flow.title != "Parameters":
        await flow.fill({})
    await flow.fill({"Fuel": "petrol", "Tank capacity": 50})
    assert flow.step["type"] == "create_entry", flow.step


async def add_charge_point(ui):
    flow = await ui.config_flow()
    await flow.choose("A charge point")
    await flow.fill({"Name": CHARGE_POINT, "Location and radius": HOME,
                     "Tariff": 0.32, "Tariff valid from": "2026-01-01"})
    assert flow.step["type"] == "create_entry", flow.step


async def set_up(ui):
    """A charge point and a vehicle set up, the vehicle's first L1 built."""
    await add_charge_point(ui)
    await add_vehicle(ui)
    await settle(ui)


# --- what the user reads back ------------------------------------------------

def vledger(hass, *argv):
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    return subprocess.run([sys.executable, "-m", "vledger.cli", *argv,
                           "--base", hass.config.path("vledger")],
                          check=False, capture_output=True, text=True, env=env)


def vehicle(hass):
    [d] = pathlib.Path(hass.config.path("vledger")).glob("vehicle-*")
    return d


def l1_status(hass):
    return vledger(hass, "l1", "status", "--vehicle", vehicle(hass).name.removeprefix("vehicle-"))


def odometer_readings(hass):
    p = vledger(hass, "l0", "read", "--vehicle", vehicle(hass).name.removeprefix("vehicle-"))
    assert p.returncode == 0, p.stderr
    lines = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
    return [ln["state"] for ln in lines if ln.get("kind") == "state"
            and ln.get("entity") == ODOMETER]


# --- while it runs ----------------------------------------------------------

async def while_it_runs(ui, hold, km):
    """With the rebuild held: the frontend's API answers, Capture status says
    ``recomputing``, every derived entity is ``unavailable``, and a new
    odometer reading reaches the raw log; then the rebuild is let go and the
    vehicle's L1 is due for nothing."""
    await hold.until_reached()
    assert [e["title"] for e in await ui.entries()]  # the event loop answers
    assert ui.capture_status() == "recomputing"
    derived = ui.derived()
    assert set(derived.values()) == {"unavailable"}, derived

    odometer(ui.hass, km)
    for _ in range(WAIT_S * 10):
        if str(km) in await asyncio.to_thread(odometer_readings, ui.hass):
            break
        await asyncio.sleep(0.1)
    assert str(km) in odometer_readings(ui.hass), "capture waited for L1"
    assert hold.reached.is_set() and not hold.go.is_set()  # still held

    hold.let_go()
    await settle(ui)


async def restart_after(ui, hold, change):
    """The vehicle's entry disabled, ``change`` made, and the entry enabled
    again with the rebuild held."""
    await ui.disable(NAME)
    await ui.hass.async_block_till_done()
    await change()
    hold.arm()
    await ui.disable(NAME, disabled=False)


# --- the tests ----------------------------------------------------------------

async def test_a_vehicle_without_l1_is_recomputed_at_startup_and_capture_goes_on(ui, hold):
    """A vehicle just added has no L1: its setup rebuilds it, and while it
    runs the capture status says so, the derived entities are unavailable
    and the odometer is captured; afterwards L1 is there."""
    await add_charge_point(ui)
    hold.arm()
    await add_vehicle(ui)
    await while_it_runs(ui, hold, 12346)
    assert (vehicle(ui.hass) / "l1" / "manifest.json").is_file()


async def test_a_vehicle_whose_l1_was_deleted_is_recomputed_at_startup(ui, hold):
    """``l1 clean`` while the vehicle is disabled: enabling it rebuilds."""
    await set_up(ui)

    async def clean():
        p = await asyncio.to_thread(
            vledger, ui.hass, "l1", "clean", "--vehicle",
            vehicle(ui.hass).name.removeprefix("vehicle-"))
        assert p.returncode == 0, p.stderr
        assert not (vehicle(ui.hass) / "l1").exists()

    await restart_after(ui, hold, clean)
    await while_it_runs(ui, hold, 12347)


async def test_l1_from_another_version_of_the_derivation_is_recomputed_at_startup(ui, hold):
    """The manifest names another library version, as after an update:
    enabling the vehicle rebuilds, and the manifest names the installed
    version again."""
    await set_up(ui)
    manifest = vehicle(ui.hass) / "l1" / "manifest.json"
    version = json.loads(manifest.read_text(encoding="utf-8"))["vledger"]

    async def older():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["vledger"] = "0.0.1"
        manifest.write_text(json.dumps(data), encoding="utf-8")

    await restart_after(ui, hold, older)
    await while_it_runs(ui, hold, 12348)
    assert json.loads(manifest.read_text(encoding="utf-8"))["vledger"] == version


async def test_changed_receipts_are_recomputed_at_startup(ui, hold):
    """A refuelling receipt added with ``vledger receipt add`` while the
    vehicle is disabled: enabling it rebuilds."""
    await set_up(ui)

    async def receipt():
        p = await asyncio.to_thread(
            vledger, ui.hass, "receipt", "add", "refuelling", "--vehicle",
            vehicle(ui.hass).name.removeprefix("vehicle-"),
            "--anchor", "2026-01-02T10:00:00Z", "--quantity-l", "41.37",
            "--total-price", "72.36", "--full")
        assert p.returncode == 0, p.stderr

    await restart_after(ui, hold, receipt)
    await while_it_runs(ui, hold, 12349)


async def test_the_vehicle_s_changed_configuration_is_recomputed_at_startup(ui, hold):
    """A threshold saved under the vehicle's Configure reloads the entry with
    the new configuration: its start rebuilds."""
    await set_up(ui)
    flow = await ui.options_flow(await ui.entry_id(NAME))
    await flow.choose("Thresholds and time constants")
    changed = flow.shown("Standstill (T_still)") * 2
    hold.arm()
    await flow.fill({"Standstill (T_still)": changed})
    assert flow.step["type"] == "create_entry", flow.step
    await while_it_runs(ui, hold, 12350)


async def test_a_charge_point_s_changed_tariff_is_recomputed_at_the_vehicle_s_startup(ui, hold):
    """A tariff added to the charge point while the vehicle is disabled:
    enabling the vehicle rebuilds its L1, which the charge point's tariffs
    are an input of."""
    await set_up(ui)

    async def tariff():
        flow = await ui.options_flow(await ui.entry_id(CHARGE_POINT))
        await flow.choose("Add a tariff from a date")
        await flow.fill({"Tariff": 0.29, "Valid from": "2026-01-01"})
        assert flow.step["type"] == "create_entry", flow.step
        await ui.hass.async_block_till_done()
        assert (await asyncio.to_thread(l1_status, ui.hass)).returncode == 1

    await restart_after(ui, hold, tariff)
    await while_it_runs(ui, hold, 12351)
