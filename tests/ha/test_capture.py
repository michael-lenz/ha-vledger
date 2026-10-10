# SPDX-License-Identifier: BSD-3-Clause
"""Capture: what the integration writes is read back with the library."""

from datetime import timedelta

import pytest
from custom_components.vledger.const import (
    DATA_KIND,
    DATA_SUBJECT,
    DOMAIN,
    OPT_BASE_PATH,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from vledger import config as vconfig
from vledger import l0
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")


async def _settle(hass, entry):
    """Let the writer drain, and the L1 writer's runs finish."""
    await hass.async_block_till_done()
    await entry.runtime_data.capture._queue.join()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_stream_is_written_start_to_stop(hass, vehicle_entry, tmp_path):
    hass.states.async_set("sensor.volvo_odometer", "100", {"unit_of_measurement": "km"})
    hass.states.async_set("device_tracker.volvo", "home",
                          {"latitude": 48.1, "longitude": 11.5, "gps_accuracy": 10, "battery": 80})
    vehicle_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)

    # Changes: a real one, an irrelevant attribute only, a unit change.
    hass.states.async_set("sensor.volvo_odometer", "101", {"unit_of_measurement": "km"})
    hass.states.async_set("device_tracker.volvo", "home",
                          {"latitude": 48.1, "longitude": 11.5, "gps_accuracy": 10, "battery": 79})
    hass.states.async_set("sensor.volvo_odometer", "101", {"unit_of_measurement": "mi"})
    hass.states.async_set("sensor.volvo_charging", "Charging")
    hass.states.async_set("sensor.unrelated", "x")
    await _settle(hass, vehicle_entry)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await _settle(hass, vehicle_entry)

    status = hass.states.get("sensor.volvo_capture_status")
    assert status.state == "running"
    assert status.attributes["lines_since_start"] == 3
    assert status.attributes["last_heartbeat_at"] is not None

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    await _settle(hass, vehicle_entry)

    lines = [r.line for r in l0.read(tmp_path, V)]
    kinds = [x["kind"] for x in lines]
    assert kinds == ["start", "config", "state", "state", "state", "heartbeat", "stop"]
    start = lines[0]
    assert {s["role"] for s in start["snapshot"]} == {"odometer", "position", "charging_state"}
    snap = {s["role"]: s for s in start["snapshot"]}
    assert snap["odometer"]["state"] == "100" and snap["odometer"]["unit"] == "km"
    assert snap["position"]["attrs"] == {"latitude": 48.1, "longitude": 11.5, "gps_accuracy": 10}
    assert snap["charging_state"]["state"] == "unavailable"   # never set
    assert lines[1]["config"]["thresholds"]["heartbeat_s"] == 600
    assert "base_path" not in lines[1]["config"]
    assert lines[1]["config"]["time_zone"] == hass.config.time_zone   # ADR-0014, point 2
    states = lines[2:5]
    assert [(s["role"], s["state"], s.get("unit")) for s in states] == [
        ("odometer", "101", "km"), ("odometer", "101", "mi"), ("charging_state", "Charging", None)]
    assert lines[5]["lines"] == 3
    assert lines[6]["reason"] == "shutdown"
    assert l0.validate(tmp_path, V).problems == []
    assert l0.gaps(tmp_path, V) == []


async def test_reload_writes_stop_start_config(hass, vehicle_entry, tmp_path):
    hass.states.async_set("sensor.volvo_odometer", "100")
    vehicle_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)

    new_options = dict(vehicle_entry.options)
    new_options["thresholds"] = dict(new_options["thresholds"], t_still_s=3600)
    hass.config_entries.async_update_entry(vehicle_entry, options=new_options)
    await hass.async_block_till_done()
    await _settle(hass, vehicle_entry)

    kinds = [r.line["kind"] for r in l0.read(tmp_path, V)]
    assert kinds == ["start", "config", "stop", "start", "config"]
    lines = [r.line for r in l0.read(tmp_path, V)]
    assert lines[2]["reason"] == "reload"
    assert lines[4]["config"]["thresholds"]["t_still_s"] == 3600
    assert [g.reason for g in l0.gaps(tmp_path, V, now=lines[4]["t"])] == ["stopped"]

    assert await hass.config_entries.async_unload(vehicle_entry.entry_id)
    await hass.async_block_till_done()
    last = [r.line for r in l0.read(tmp_path, V)][-1]
    assert last["kind"] == "stop" and last["reason"] == "unload"


async def test_a_removed_entity_is_unavailable_and_raises_an_issue(hass, vehicle_entry, tmp_path):
    hass.states.async_set("sensor.volvo_odometer", "100")
    vehicle_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)
    hass.states.async_remove("sensor.volvo_odometer")
    await _settle(hass, vehicle_entry)

    last = [r.line for r in l0.read(tmp_path, V)][-1]
    assert last["kind"] == "state" and last["state"] == "unavailable" and last["role"] == "odometer"
    issues = ir.async_get(hass).issues
    assert any(k[1].startswith("entity_removed_a7c1_sensor.volvo_odometer") for k in issues)


async def test_lines_keep_their_order_under_a_burst(hass, vehicle_entry, tmp_path):
    hass.states.async_set("sensor.volvo_odometer", "0")
    vehicle_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    for i in range(1, 201):
        hass.states.async_set("sensor.volvo_odometer", str(i))
    await _settle(hass, vehicle_entry)
    values = [r.line["state"] for r in l0.read(tmp_path, V, kind="state")]
    assert values == [str(i) for i in range(1, 201)]
    assert l0.validate(tmp_path, V).problems == []


async def test_a_line_carries_when_the_old_value_was_last_reported(
        hass, vehicle_entry, tmp_path, freezer):
    """An update repeating a value writes nothing but moves last_reported;
    the next line carries it, so t minus it is one sampling interval
    (ADR-0011)."""
    kms = {"unit_of_measurement": "km"}
    freezer.move_to("2026-10-09T06:00:00Z")
    hass.states.async_set("sensor.volvo_odometer", "100", kms)
    vehicle_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)
    freezer.move_to("2026-10-09T06:01:00Z")
    hass.states.async_set("sensor.volvo_odometer", "100", kms)   # reported, unchanged
    freezer.move_to("2026-10-09T06:02:00Z")
    hass.states.async_set("sensor.volvo_odometer", "101", kms)
    freezer.move_to("2026-10-09T06:03:00Z")
    hass.states.async_set("sensor.volvo_odometer", "102", kms)   # changed at its first report
    freezer.move_to("2026-10-09T06:04:00Z")
    hass.states.async_remove("sensor.volvo_odometer")             # no old state after this
    freezer.move_to("2026-10-09T06:05:00Z")
    hass.states.async_set("sensor.volvo_odometer", "103", kms)
    await _settle(hass, vehicle_entry)

    first, second, removed, readded = [r.line for r in l0.read(tmp_path, V, kind="state")]
    assert first["t"] == "2026-10-09T06:02:00.000Z"
    assert first["reported_before"] == "2026-10-09T06:01:00.000Z"
    # Heard once, then changed: still one sampling interval (ADR-0011).
    assert second["reported_before"] == "2026-10-09T06:02:00.000Z"
    assert removed["state"] == "unavailable" and "reported_before" not in removed
    assert "reported_before" not in readded
    assert l0.validate(tmp_path, V).problems == []


@pytest.mark.parametrize("unit", ["%", "L"])
async def test_tank_capacity_is_demanded_only_for_percent(hass, unit):
    from custom_components.vledger.config_flow import _needs_tank_capacity

    hass.states.async_set("sensor.fuel", "50", {"unit_of_measurement": unit})
    assert _needs_tank_capacity(hass, {"fuel_level": {"entity": "sensor.fuel"}}) == (unit == "%")


def _entry(tmp_path, **thresholds):
    options = vconfig.vehicle(
        "Volvo",
        {"odometer": {"entity": "sensor.volvo_odometer"},
         "charging_state": {"entity": "sensor.volvo_charging", "map": {"charging": ["Charging"]}}},
        {"fuel": "petrol", "tank_capacity_l": 71}, thresholds)
    options[OPT_BASE_PATH] = str(tmp_path)
    return MockConfigEntry(domain=DOMAIN, title="Volvo", unique_id="a7c1",
                           data={DATA_KIND: "vehicle", DATA_SUBJECT: "a7c1"}, options=options)


def _issue_ids(hass) -> set[str]:
    return {k[1] for k in ir.async_get(hass).issues if k[0] == "vledger"}


async def _elapse(hass, entry, freezer, when: str) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, dt_util.utcnow())
    await _settle(hass, entry)


async def test_an_entity_unavailable_longer_than_the_threshold_raises_an_issue_until_it_reports(
        hass, tmp_path, freezer):
    """HAI-08's second half: the timer runs from when the entity stopped
    reporting, and the first value it reports again clears the issue."""
    entry = _entry(tmp_path, outage_s=3600, heartbeat_s=600)
    freezer.move_to("2026-10-09T06:00:00Z")
    hass.states.async_set("sensor.volvo_odometer", "100")
    hass.states.async_set("sensor.volvo_charging", "Idle")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)
    assert _issue_ids(hass) == set()

    freezer.move_to("2026-10-09T06:10:00Z")
    hass.states.async_set("sensor.volvo_odometer", "unavailable")
    await _settle(hass, entry)
    assert entry.runtime_data.capture.outages == {"sensor.volvo_odometer": "2026-10-09T06:10:00.000Z"}
    freezer.move_to("2026-10-09T06:40:00Z")
    hass.states.async_set("sensor.volvo_odometer", "unknown")     # the same outage, not a new one
    await _settle(hass, entry)
    assert entry.runtime_data.capture.outages == {"sensor.volvo_odometer": "2026-10-09T06:10:00.000Z"}

    await _elapse(hass, entry, freezer, "2026-10-09T07:09:00Z")
    assert _issue_ids(hass) == set()
    await _elapse(hass, entry, freezer, "2026-10-09T07:11:00Z")
    assert _issue_ids(hass) == {"entity_unavailable_a7c1_sensor.volvo_odometer"}
    # The stream says what Home Assistant said; the issue adds nothing to it.
    states = [(x.line["state"]) for x in l0.read(tmp_path, V, kind="state")]
    assert states == ["unavailable", "unknown"]

    freezer.move_to("2026-10-09T08:00:00Z")
    hass.states.async_set("sensor.volvo_odometer", "101")
    await _settle(hass, entry)
    assert _issue_ids(hass) == set()
    assert entry.runtime_data.capture.outages == {}
    assert entry.runtime_data.capture.counts()["outages"] == {}


async def test_an_outage_is_counted_from_when_the_entity_stopped_not_from_the_start(
        hass, tmp_path, freezer):
    """A restart in the middle of an outage does not restart the clock: an
    entity that exists is in outage since Home Assistant last saw it
    change, one that does not since the stream's last line of its role. One
    already past the threshold at start raises the issue at once."""
    entry = _entry(tmp_path, outage_s=3600, heartbeat_s=600)
    freezer.move_to("2026-10-09T05:30:00Z")
    hass.states.async_set("sensor.volvo_odometer", "unavailable")
    hass.states.async_set("sensor.volvo_charging", "Idle")
    freezer.move_to("2026-10-09T06:00:00Z")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)
    assert entry.runtime_data.capture.outages == {"sensor.volvo_odometer": "2026-10-09T05:30:00.000Z"}
    await _elapse(hass, entry, freezer, "2026-10-09T06:29:00Z")
    assert _issue_ids(hass) == set()
    await _elapse(hass, entry, freezer, "2026-10-09T06:31:00Z")
    assert _issue_ids(hass) == {"entity_unavailable_a7c1_sensor.volvo_odometer"}

    freezer.move_to("2026-10-09T06:40:00Z")
    hass.states.async_remove("sensor.volvo_charging")
    await _settle(hass, entry)
    assert _issue_ids(hass) == {"entity_unavailable_a7c1_sensor.volvo_odometer",
                                "entity_removed_a7c1_sensor.volvo_charging"}

    # A reload while both are out: the odometer's issue is back at once;
    # the charging state, gone from Home Assistant, is in outage since its
    # last line in the stream, not since this start.
    freezer.move_to("2026-10-09T06:50:00Z")
    new_options = dict(entry.options, thresholds=dict(entry.options["thresholds"], t_still_s=3600))
    hass.config_entries.async_update_entry(entry, options=new_options)
    await hass.async_block_till_done()
    await _settle(hass, entry)
    assert _issue_ids(hass) == {"entity_unavailable_a7c1_sensor.volvo_odometer"}
    assert entry.runtime_data.capture.outages == {"sensor.volvo_odometer": "2026-10-09T05:30:00.000Z",
                                                  "sensor.volvo_charging": "2026-10-09T06:40:00.000Z"}
    await _elapse(hass, entry, freezer, "2026-10-09T07:41:00Z")
    assert _issue_ids(hass) == {"entity_unavailable_a7c1_sensor.volvo_odometer",
                                "entity_unavailable_a7c1_sensor.volvo_charging"}

    hass.states.async_set("sensor.volvo_charging", "Idle")
    await _settle(hass, entry)
    assert _issue_ids(hass) == {"entity_unavailable_a7c1_sensor.volvo_odometer"}

    # Unloading takes the capture's opinion with it.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue_ids(hass) == set()


async def test_a_removed_entity_that_comes_back_clears_its_issue(hass, vehicle_entry, tmp_path):
    hass.states.async_set("sensor.volvo_odometer", "100")
    vehicle_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)
    hass.states.async_remove("sensor.volvo_odometer")
    await _settle(hass, vehicle_entry)
    assert "entity_removed_a7c1_sensor.volvo_odometer" in _issue_ids(hass)
    hass.states.async_set("sensor.volvo_odometer", "101")
    await _settle(hass, vehicle_entry)
    assert "entity_removed_a7c1_sensor.volvo_odometer" not in _issue_ids(hass)
