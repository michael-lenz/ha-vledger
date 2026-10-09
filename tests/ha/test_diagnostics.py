# SPDX-License-Identifier: BSD-3-Clause
"""The raw log as entities and as diagnostics (TASK-0008): what the
integration shows is checked against what the library reads back."""

from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from vledger import clock, layout, stats
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")

LOG_ENTITIES = (
    "current_month_file_size", "raw_log_size", "month_files", "last_line_written",
    "last_heartbeat", "last_state_line", "state_lines_since_start", "state_lines_today",
    "capture_gaps", "latest_capture_gap",
)


async def _settle(hass, entry):
    await hass.async_block_till_done()
    await entry.runtime_data._queue.join()
    await hass.async_block_till_done()


def _at(state, t: str) -> bool:
    """A timestamp sensor shows ``t``; Home Assistant renders it to the second."""
    return dt_util.parse_datetime(state.state) == clock.parse(t).replace(microsecond=0)


def _value(hass, name):
    return hass.states.get(f"sensor.volvo_{name}")


async def _set_up(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, entry)


async def test_the_raw_log_as_diagnostic_entities(hass, vehicle_entry, tmp_path):
    hass.states.async_set("sensor.volvo_odometer", "100", {"unit_of_measurement": "km"})
    await _set_up(hass, vehicle_entry)
    hass.states.async_set("sensor.volvo_odometer", "101", {"unit_of_measurement": "km"})
    hass.states.async_set("sensor.volvo_charging", "Charging")
    await _settle(hass, vehicle_entry)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await _settle(hass, vehicle_entry)

    registry = er.async_get(hass)
    for name in LOG_ENTITIES:
        entity = registry.async_get(f"sensor.volvo_{name}")
        assert entity is not None, name
        assert entity.entity_category is EntityCategory.DIAGNOSTIC, name

    counted = stats.scan(tmp_path, V)
    size = layout.l0_file(tmp_path, V, clock.month_of(counted.last_line_at)).stat().st_size
    # Shown in the suggested units; the native bytes are what the file holds.
    assert float(_value(hass, "raw_log_size").state) * 1024 == pytest.approx(size)
    assert _value(hass, "raw_log_size").attributes["state_class"] == "total_increasing"
    assert float(_value(hass, "current_month_file_size").state) * 1024 == pytest.approx(size)
    assert _value(hass, "month_files").state == "1"
    assert _value(hass, "state_lines_since_start").state == "2"
    assert _value(hass, "state_lines_today").state == "2"
    last_state = _value(hass, "last_state_line")
    assert last_state.attributes["role"] == "charging_state"
    assert _at(last_state, counted.last_states["charging_state"]["t"])
    assert _at(_value(hass, "last_heartbeat"), counted.last_heartbeat_at)
    assert _at(_value(hass, "last_line_written"), counted.last_line_at)
    assert _value(hass, "capture_gaps").state == "0"
    assert _value(hass, "latest_capture_gap").state == "unknown"


async def test_what_the_stream_held_before_start_is_counted(hass, vehicle_entry, tmp_path):
    # An earlier run today that crashed: a start, a state line, no stop.
    earlier = dt_util.utcnow() - timedelta(minutes=30)
    b = ["--base", str(tmp_path), "--vehicle", V.id]
    assert main(["l0", "start", *b, "--t", clock.to_text(earlier),
                 "--homeassistant", "2026.10.1"]) == 0
    assert main(["l0", "state", *b, "--t", clock.to_text(earlier + timedelta(seconds=1)),
                 "--role", "odometer", "--entity", "sensor.volvo_odometer",
                 "--state", "99", "--unit", "km"]) == 0
    hass.states.async_set("sensor.volvo_odometer", "100", {"unit_of_measurement": "km"})
    await _set_up(hass, vehicle_entry)

    assert _value(hass, "capture_gaps").state == "1"
    gap = _value(hass, "latest_capture_gap")
    assert gap.attributes["reason"] == "crash"
    assert gap.attributes["start"] == clock.to_text(earlier + timedelta(seconds=1))
    assert 1790 <= float(gap.state) <= 1810
    assert _value(hass, "state_lines_since_start").state == "0"
    # The line of the earlier run counts for today, unless that was yesterday.
    expected_today = 1 if dt_util.as_local(earlier).date() == dt_util.now().date() else 0
    assert _value(hass, "state_lines_today").state == str(expected_today)
    assert _value(hass, "last_state_line").attributes["role"] == "odometer"
    # Whole seconds, shown as whole seconds.
    entity = er.async_get(hass).async_get("sensor.volvo_latest_capture_gap")
    assert entity.options["sensor"]["suggested_display_precision"] == 0


def test_every_sensor_has_a_name_and_an_icon():
    import json

    from custom_components.vledger.sensor import LOG_SENSORS

    root = Path(__file__).resolve().parents[2] / "custom_components/vledger"
    icons = json.loads((root / "icons.json").read_text())["entity"]["sensor"]
    names = json.loads((root / "strings.json").read_text())["entity"]["sensor"]
    keys = {"capture_status", *(d.translation_key for d in LOG_SENSORS)}
    assert set(icons) == set(names) == keys


async def test_lines_today_restart_at_local_midnight(hass, vehicle_entry):
    await _set_up(hass, vehicle_entry)
    hass.states.async_set("sensor.volvo_odometer", "101")
    await _settle(hass, vehicle_entry)
    assert _value(hass, "state_lines_today").state == "1"
    midnight = dt_util.start_of_local_day() + timedelta(days=1)
    vehicle_entry.runtime_data._on_midnight(midnight)
    await hass.async_block_till_done()
    assert _value(hass, "state_lines_today").state == "0"
    assert _value(hass, "state_lines_since_start").state == "1"


async def test_diagnostics_count_the_stream_and_redact_positions(
        hass, hass_client, vehicle_entry, tmp_path):
    hass.states.async_set("sensor.volvo_odometer", "100", {"unit_of_measurement": "km"})
    hass.states.async_set("device_tracker.volvo", "not_home",
                          {"latitude": 48.1, "longitude": 11.5, "gps_accuracy": 10})
    await _set_up(hass, vehicle_entry)
    for km in ("101", "102", "103"):
        hass.states.async_set("sensor.volvo_odometer", km, {"unit_of_measurement": "km"})
        await _settle(hass, vehicle_entry)
    hass.states.async_set("device_tracker.volvo", "home",
                          {"latitude": 48.2, "longitude": 11.6, "gps_accuracy": 8})
    hass.states.async_set("sensor.volvo_charging", "Charging")
    hass.states.async_set("sensor.volvo_charging", "Done")
    await _settle(hass, vehicle_entry)

    d = await get_diagnostics_for_config_entry(hass, hass_client, vehicle_entry)
    capture, stream = d["capture"], d["stream"]
    assert capture["lines_since_start"] == stream["lines_since_start"] == 6
    assert capture["stream_bytes"] == stream["bytes"]
    assert capture["gaps"] == len(stream["gaps"]) == 0
    assert stream["change_intervals"]["odometer"]["count"] == 2
    assert set(stream["change_intervals"]["odometer"]) == {"count", "median_s", "p95_s"}
    # Every value changed at its first report: nothing to measure sampling by.
    assert stream["sampling_intervals"] == {}
    assert stream["unlisted"] == {"charging_state": ["Done"]}
    position = stream["last_states"]["position"]
    assert position["state"] == "home"
    assert position["attrs"]["latitude"] == REDACTED and position["attrs"]["longitude"] == REDACTED
    assert position["attrs"]["gps_accuracy"] == 8
    assert "48.2" not in str(d) and "11.6" not in str(d)


async def test_diagnostics_redact_a_charge_point_s_place(hass, hass_client, tmp_path):
    from custom_components.vledger.const import (
        DATA_KIND,
        DATA_SUBJECT,
        DOMAIN,
        OPT_BASE_PATH,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from vledger import config as vconfig

    options = vconfig.chargepoint("Home", 48.1371, 11.5754, 50,
                                  [{"from": "2026-01-01", "eur_per_kwh": 0.30}])
    options[OPT_BASE_PATH] = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, title="Home", unique_id="cp1",
                            data={DATA_KIND: "chargepoint", DATA_SUBJECT: "cp1"}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data._queue.join()

    d = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert d["entry"]["options"]["latitude"] == REDACTED
    assert d["entry"]["options"]["longitude"] == REDACTED
    assert "48.1371" not in str(d)
