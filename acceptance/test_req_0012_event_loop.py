# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0012: file I/O does not block the event loop.

Derived from the requirement and the user guide's *In Home Assistant*: from
the moment an entry is set up its raw log is written under
``<config>/vledger/``, L1 is kept on disk beside it and rebuilt at startup
and on **Vehicle Ledger: Recompute**, a receipt entered in Home Assistant
changes the receipts file, **Download diagnostics** counts the whole log,
**Vehicle Ledger: Export** writes into the media folder, and saving the
options reloads the entry, which ends the stream orderly.

Home Assistant runs every coroutine and callback on one thread, its event
loop; file I/O that is not to block it runs on another. So the tests watch
every file operation Python offers — opening, reading the directory, stat,
creating, renaming, removing, syncing — on that one thread, for any path in
the instance's configuration directory, while they use the integration as a
person does, and the requirement holds when there is none. Home Assistant's
own blocking-call detection leaves file I/O out under tests, which is why the
watch is the tests' own; the first test shows that it sees what it is meant
to."""

import asyncio
import builtins
import datetime as dt
import io
import os
import pathlib
import threading
import traceback

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

DOMAIN = "vledger"

pytestmark = [pytest.mark.req("REQ-0012")]


class LoopIO:
    """Every file operation on the event loop's thread that touches a path
    under ``root``, with where it came from."""

    # What opens, lists, inspects or changes a file or directory. pathlib,
    # shutil, gzip, json and the like all end in one of these.
    OS_CALLS = ("open", "stat", "lstat", "listdir", "scandir", "mkdir", "rmdir",
                "remove", "unlink", "rename", "replace", "truncate", "utime",
                "chmod", "fsync", "fdatasync", "access")

    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))
        self.loop_thread = threading.get_ident()
        self.seen = []
        self._busy = threading.local()

    def _path(self, target):
        if isinstance(target, int):  # a descriptor: what it is open on
            try:
                return os.readlink(f"/proc/self/fd/{target}")
            except OSError:
                return None
        try:
            return os.path.abspath(os.fsdecode(os.fspath(target)))
        except TypeError:
            return None

    def _check(self, name, args):
        if threading.get_ident() != self.loop_thread or getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            path = self._path(args[0]) if args else (None if name != "listdir" else os.getcwd())
            if path and (path == self.root or path.startswith(self.root + os.sep)):
                where = [f"{f.filename}:{f.lineno} {f.name}"
                         for f in traceback.extract_stack()[:-3]][-6:]
                self.seen.append((name, os.path.relpath(path, self.root), where))
        finally:
            self._busy.on = False

    def _wrap(self, name, func):
        def watched(*args, **kwargs):
            self._check(name, args)
            return func(*args, **kwargs)
        return watched

    def install(self, monkeypatch):
        opened = self._wrap("open", io.open)
        monkeypatch.setattr(builtins, "open", opened)
        monkeypatch.setattr(io, "open", opened)
        for name in self.OS_CALLS:
            if hasattr(os, name):
                monkeypatch.setattr(os, name, self._wrap(f"os.{name}", getattr(os, name)))

    def report(self):
        return "\n".join(f"{name} {path}\n    " + "\n    ".join(where)
                         for name, path, where in self.seen)


@pytest.fixture
async def instance(hass: HomeAssistant, tmp_path, enable_custom_integrations,
                   hass_client, monkeypatch):
    """A Home Assistant whose configuration directory is a temporary one,
    with diagnostics and the media folder as on an instance, and the watch
    on its event loop installed before the ledger is ever set up."""
    hass.config.config_dir = str(tmp_path)
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    # As Home Assistant's configuration does on an instance: its media
    # folders may be written to.
    hass.config.allowlist_external_dirs = {str(tmp_path / "media")}
    hass.config.latitude, hass.config.longitude = 52.52, 13.40
    assert await async_setup_component(hass, "diagnostics", {})
    # The frontend's own package (home-assistant-frontend) is not part of the
    # test stack; it is stood in for as set up, with nowhere to serve from.
    hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(lambda *_: None, [])
    hass.config.components.add("frontend")
    await hass.async_block_till_done()
    # Home Assistant's own server keeps its router open after it starts, since
    # an integration set up later in the UI adds paths to it (the http
    # component's start). The test client starts the server here, before any
    # entry exists, and which test client a test stack provides is not fixed:
    # the router is kept open as Home Assistant keeps it.
    hass.http.app.router.freeze = lambda: None
    watch = LoopIO(tmp_path)
    watch.install(monkeypatch)
    return hass, watch, await hass_client()


async def flow_by_labels(hass, category, result, steps):
    """Walk a config or options flow as the frontend does: a menu option and
    each field chosen by the English label shown, a field left alone sent
    with what the form showed."""
    labels = await async_get_translations(hass, "en", category, [DOMAIN])
    manager = hass.config_entries.flow if category == "config" else hass.config_entries.options

    def label(*parts):
        return labels.get(".".join(("component", DOMAIN, category, "step",
                                    result["step_id"], *parts)))

    for wanted in steps:
        if result["type"] == "menu":
            options = {label("menu_options", o): o for o in result["menu_options"]}
            result = await manager.async_configure(result["flow_id"],
                                                   {"next_step_id": options[wanted]})
            continue
        assert result["type"] == "form", result
        data = {}
        for key in result["data_schema"].schema:
            shown = label("data", str(key))
            if shown in wanted:
                data[str(key)] = wanted[shown]
            else:
                default = key.default() if callable(getattr(key, "default", None)) else None
                suggested = (getattr(key, "description", None) or {}).get("suggested_value")
                value = suggested if suggested is not None else default
                if value is not None:
                    data[str(key)] = value
        result = await manager.async_configure(result["flow_id"], data)
    return result


def sensors(hass, odometer="12345", lat=52.52, lon=13.40, fuel="31.5"):
    hass.states.async_set("sensor.car_odometer", odometer,
                          {"unit_of_measurement": "km", "device_class": "distance",
                           "state_class": "total_increasing"})
    hass.states.async_set("device_tracker.car", "home",
                          {"latitude": lat, "longitude": lon, "source_type": "gps"})
    hass.states.async_set("sensor.car_fuel", fuel,
                          {"unit_of_measurement": "L", "device_class": "volume_storage"})


async def settle(hass):
    """Until the ledger is idle: capture running, no rebuild left."""
    for _ in range(200):
        await hass.async_block_till_done(wait_background_tasks=True)
        status = [s.state for s in hass.states.async_all("sensor")
                  if s.attributes.get("friendly_name", "").endswith("Capture status")]
        if status and "recomputing" not in status:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"Capture status stayed {status}")


async def add_vehicle(hass):
    sensors(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow_by_labels(hass, "config", result, [
        "A vehicle",
        {"Name": "Family car"},
        {"Odometer": "sensor.car_odometer",
         "Position (device tracker)": "device_tracker.car",
         "Fuel level": "sensor.car_fuel"},
        {"Fuel": "petrol", "Tank capacity": 50},
    ])
    assert result["type"] == "create_entry", result
    assert await settle(hass) == ["running"]
    [entry] = hass.config_entries.async_entries(DOMAIN)
    return entry


async def files(hass, directory, pattern):
    """The files under a directory of the configuration, looked up as the
    tests' own I/O is done: off the event loop, so the watch does not see it."""
    return await hass.async_add_executor_job(
        lambda: [p for p in pathlib.Path(hass.config.path(directory)).rglob(pattern)
                 if p.is_file()])


def assert_loop_free(watch):
    assert not watch.seen, ("file I/O on the event loop:\n" + watch.report())


async def test_the_watch_sees_file_io_on_the_event_loop_and_only_there(instance):
    """A file opened on the loop is seen; the same file opened by an executor
    job, as Home Assistant runs blocking work, is not."""
    hass, watch, _ = instance
    probe = os.path.join(hass.config.path("vledger"), "probe")

    def write():
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w") as f:
            f.write("x")

    await hass.async_add_executor_job(write)
    assert watch.seen == []
    with open(probe) as f:  # noqa: ASYNC230 -- on the loop, on purpose
        f.read()
    assert [(name, path) for name, path, _ in watch.seen] == [("open", "vledger/probe")]


async def test_setting_up_a_vehicle_and_capturing_blocks_nothing(instance):
    """Set up from the UI: the raw log is opened and written, L1 is built at
    startup, and every state the assigned entities report is captured."""
    hass, watch, _ = instance
    await add_vehicle(hass)
    for km, lat in ((12346, 52.53), (12350, 52.56), (12357, 52.60)):
        sensors(hass, odometer=str(km), lat=lat, lon=13.45, fuel="30.9")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert await files(hass, "vledger", "*"), "no raw log under <config>/vledger/"
    assert_loop_free(watch)


async def test_recompute_receipts_diagnostics_and_export_block_nothing(instance):
    """The actions and the entry's menu: Recompute, a refuelling receipt
    entered and cancelled (each rebuilds L1), Download diagnostics (a full
    count of the log) and an export into the media folder."""
    hass, watch, client = instance
    entry = await add_vehicle(hass)

    await hass.services.async_call(DOMAIN, "recompute", {}, blocking=True)
    await settle(hass)

    anchor = (dt.datetime.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    receipt = await hass.services.async_call(
        DOMAIN, "add_refuelling_receipt",
        {"config_entry_id": entry.entry_id, "anchor": anchor, "quantity_l": 41.37,
         "total_price": 72.36, "full": True},
        blocking=True, return_response=True)
    await settle(hass)
    await hass.services.async_call(
        DOMAIN, "cancel_receipt",
        {"config_entry_id": entry.entry_id, "receipt": receipt["id"]}, blocking=True)
    await settle(hass)

    resp = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert resp.status == 200, await resp.text()

    await hass.services.async_call(
        DOMAIN, "export", {"config_entry_id": entry.entry_id, "format": "csv",
                           "kind": "trip"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await files(hass, "media", "*.csv"), "no export in the media folder"
    assert_loop_free(watch)


async def test_options_reload_and_unload_block_nothing(instance):
    """Saving the options reloads the entry (stop, start and the new
    configuration in the stream); unloading it ends the stream orderly."""
    hass, watch, _ = instance
    entry = await add_vehicle(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await flow_by_labels(hass, "options", result, [
        "Thresholds and time constants", {"Standstill (T_still)": 1200}])
    assert result["type"] == "create_entry", result
    await settle(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert_loop_free(watch)
