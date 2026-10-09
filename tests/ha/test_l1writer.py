# SPDX-License-Identifier: BSD-3-Clause
"""The live derivation (TASK-0018): what the integration writes to L1 is
checked against what the library's batch verb writes from the same stream."""

import asyncio
import shutil
import threading
from datetime import timedelta
from unittest.mock import patch

import pytest
from custom_components.vledger.const import (
    DATA_KIND,
    DATA_SUBJECT,
    DOMAIN,
    OPT_BASE_PATH,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from vledger import config as vconfig
from vledger import l1
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
T0 = dt_util.parse_datetime("2026-10-09T10:00:00Z")
KM = {"unit_of_measurement": "km"}

# Fixes roughly 5 km apart.
ROAD = [(51.0300, 7.0200), (51.0600, 7.0500), (51.0900, 7.0700)]


async def _settle(hass, entry):
    """Let the capture drain and every run the writer started finish."""
    for _ in range(3):
        await hass.async_block_till_done(wait_background_tasks=True)
        await entry.runtime_data.capture.async_flushed()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)


def _at(minutes: float):
    return T0 + timedelta(minutes=minutes)


async def _drive(hass, entry, freezer, start_min: float, odo: float) -> float:
    """Three samples 15 minutes apart, each moving odometer and position."""
    for i, (lat, lon) in enumerate(ROAD, start=1):
        freezer.move_to(_at(start_min + 15 * i))
        hass.states.async_set("sensor.volvo_odometer", str(odo + 7 * i), KM)
        hass.states.async_set("device_tracker.volvo", "not_home",
                              {"latitude": lat, "longitude": lon, "gps_accuracy": 10})
        await _settle(hass, entry)
    return start_min + 45


async def _standstill(hass, entry, freezer, minutes: float) -> None:
    """Nothing moves; the heartbeats go on, every ten minutes."""
    freezer.move_to(_at(minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await _settle(hass, entry)


def _batch_trips(tmp_path, live) -> bytes:
    """`vledger derive all --write` on a copy of the stream the integration wrote."""
    copy = tmp_path / "batch"
    shutil.copytree(live / "vehicle-a7c1" / "l0", copy / "vehicle-a7c1" / "l0")
    assert main(["derive", "all", "--base", str(copy), "--vehicle", "a7c1", "--write"]) == 0
    return (l1.l1_dir(copy, V) / "trips.jsonl").read_bytes()


def _live_trips(base) -> bytes:
    return (l1.l1_dir(base, V) / "trips.jsonl").read_bytes()


async def test_setup_without_l1_rebuilds_while_capture_runs(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    hass.states.async_set("sensor.volvo_odometer", "1000", KM)
    vehicle_entry.add_to_hass(hass)
    seen = []
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    data = vehicle_entry.runtime_data
    data.capture.listen(lambda: seen.append((data.capture.status, data.l1.available)))
    await _settle(hass, vehicle_entry)

    assert ("recomputing", False) in seen
    assert seen[-1] == ("running", True)
    assert hass.states.get("sensor.volvo_capture_status").state == "running"
    assert data.l1.last_rebuild["reason"] == "no manifest"
    assert l1.rebuild_due(tmp_path, V) is None
    # Capture did not wait: the start and config lines are in L0, and L1 read them.
    assert l1.read_manifest(tmp_path, V)["l0_through"] is not None


async def test_a_drive_through_state_changes_equals_the_batch(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    hass.states.async_set("sensor.volvo_odometer", "1000", KM)
    hass.states.async_set("device_tracker.volvo", "home",
                          {"latitude": 51.0, "longitude": 7.0, "gps_accuracy": 10})
    await _setup(hass, vehicle_entry)

    end = await _drive(hass, vehicle_entry, freezer, 60, 1000)
    await _standstill(hass, vehicle_entry, freezer, end + 20)
    assert list(l1.read(tmp_path, V, "trip")) == []          # the standstill has not elapsed
    await _standstill(hass, vehicle_entry, freezer, end + 40)  # a heartbeat after T_still completes it

    trips = list(l1.read(tmp_path, V, "trip"))
    assert len(trips) == 1
    assert vehicle_entry.runtime_data.l1.last_rebuild["reason"] == "no manifest"   # only the first run
    assert _live_trips(tmp_path) == _batch_trips(tmp_path, tmp_path)


async def test_state_lines_run_at_most_once_a_minute(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    await _setup(hass, vehicle_entry)
    writer = vehicle_entry.runtime_data.l1
    first = writer.last_run_at
    for i in range(5):
        freezer.move_to(T0 + timedelta(seconds=10 * (i + 1)))
        hass.states.async_set("sensor.volvo_odometer", str(1000 + i), KM)
        await _settle(hass, vehicle_entry)
    assert writer.last_run_at == first                       # held back
    freezer.move_to(T0 + timedelta(seconds=61))
    async_fire_time_changed(hass, dt_util.utcnow())
    await _settle(hass, vehicle_entry)
    assert writer.last_run_at != first                       # and then run once
    assert l1.read_manifest(tmp_path, V)["l0_through"].startswith("2026-10-09T10:00:50")


async def test_the_action_rebuilds(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    hass.states.async_set("sensor.volvo_odometer", "1000", KM)
    await _setup(hass, vehicle_entry)
    end = await _drive(hass, vehicle_entry, freezer, 60, 1000)
    await _standstill(hass, vehicle_entry, freezer, end + 40)
    before = _live_trips(tmp_path)
    (l1.l1_dir(tmp_path, V) / "trips.jsonl").write_text("")  # L1 is regenerable (ABL-05)

    await hass.services.async_call(DOMAIN, "recompute", {}, blocking=True)
    assert vehicle_entry.runtime_data.l1.last_rebuild["reason"] == "requested"
    assert _live_trips(tmp_path) == before

    await hass.services.async_call(DOMAIN, "recompute", {"config_entry_id": vehicle_entry.entry_id},
                                   blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "recompute", {"config_entry_id": "nope"}, blocking=True)


async def test_a_reload_keeps_the_cursor(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    hass.states.async_set("sensor.volvo_odometer", "1000", KM)
    await _setup(hass, vehicle_entry)
    end = await _drive(hass, vehicle_entry, freezer, 60, 1000)
    await _standstill(hass, vehicle_entry, freezer, end + 40)
    through = l1.read_manifest(tmp_path, V)["through"]

    freezer.move_to(_at(end + 50))
    assert await hass.config_entries.async_reload(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)
    writer = vehicle_entry.runtime_data.l1
    assert writer.last_rebuild is None                       # same version, config, receipts
    assert l1.read_manifest(tmp_path, V)["through"] == through

    end2 = await _drive(hass, vehicle_entry, freezer, end + 60, 1021)
    await _standstill(hass, vehicle_entry, freezer, end2 + 40)
    assert len(list(l1.read(tmp_path, V, "trip"))) == 2
    assert writer.last_rebuild is None
    assert _live_trips(tmp_path) == _batch_trips(tmp_path, tmp_path)


async def test_a_charge_points_l1_is_a_manifest(hass, tmp_path):
    options = vconfig.chargepoint("Garage", 51.0, 7.0, 30, [{"eur_per_kwh": 0.3, "from": "2026-01-01"}])
    options[OPT_BASE_PATH] = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, title="Garage", unique_id="cp1",
                            data={DATA_KIND: "chargepoint", DATA_SUBJECT: "cp1"}, options=options)
    await _setup(hass, entry)
    cp = Subject("chargepoint", "cp1")
    assert [p.name for p in l1.l1_dir(tmp_path, cp).iterdir()] == ["manifest.json"]



async def test_a_line_during_a_run_is_caught_by_the_next(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    await _setup(hass, vehicle_entry)
    gate = threading.Event()
    rebuild = l1.rebuild

    def slow_rebuild(base, subject):
        manifest = rebuild(base, subject)      # L0 read before the line below
        gate.wait(5)
        return manifest

    freezer.move_to(T0 + timedelta(minutes=5))
    with patch.object(l1, "rebuild", slow_rebuild):
        call = hass.async_create_task(hass.services.async_call(DOMAIN, "recompute", {}, blocking=True))
        while not vehicle_entry.runtime_data.capture.recomputing:   # the rebuild has begun
            await asyncio.sleep(0)
        hass.states.async_set("sensor.volvo_odometer", "1001", KM)
        while not vehicle_entry.runtime_data.l1._pending:          # written, and asked for a run
            await asyncio.sleep(0)
        gate.set()
        await call
        await _settle(hass, vehicle_entry)
    assert l1.read_manifest(tmp_path, V)["l0_through"] == "2026-10-09T10:05:00.000Z"


def test_the_writer_derives_what_the_library_derives():
    """Derivations register themselves on import: the writer must see every
    one the library has, not only those it happened to import."""
    import subprocess
    import sys
    code = ("import custom_components.vledger.l1writer, importlib, pkgutil, vledger; from vledger import l1; "
            "seen = sorted(l1.DERIVATIONS); "
            "[importlib.import_module('vledger.' + m.name) for m in pkgutil.iter_modules(vledger.__path__)]; "
            "print(seen == sorted(l1.DERIVATIONS), *seen)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split()[0] == "True" and "trip" in out.stdout.split()
