# SPDX-License-Identifier: BSD-3-Clause
"""The corrected metrics as external statistics (ADR-0019): a row per
month line, the whole series written again when the months change."""

from datetime import timedelta
from functools import partial
from pathlib import Path

import pytest
from custom_components.vledger import external_statistics
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from test_metric_entities import line
from test_receipt_entry import _setup

from vledger import l1
from vledger.layout import Subject

GOLF = Subject("vehicle", "b8d2")


@pytest.fixture(autouse=True)
def _custom_integrations(recorder_db_url, enable_custom_integrations):
    """As in conftest, with the recorder's database prepared before Home
    Assistant starts, which ``recorder_mock`` requires."""
SEP, OCT = "2026-08-31T22:00:00.000Z", "2026-09-30T18:30:00.000Z"   # Berlin, then Kolkata's midnight


def months(sep_km=100.0):
    return [line("month", SEP, OCT, distance_km=sep_km, electric_energy_share=0.4),
            line("month", OCT, "2026-10-31T18:30:00.000Z", distance_km=50.0),
            line("year", SEP, "2026-12-31T23:00:00.000Z", distance_km=150.0),
            line("rolling", SEP, OCT, distance_km=80.0),
            line("lifetime", SEP, OCT, charge_cycles=3.0)]


async def _rows(hass, *ids):
    await async_wait_recording_done(hass)
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.parse_datetime(SEP) - timedelta(days=1), None,
        set(ids), "hour", None, {"state", "sum", "mean", "min", "max"})


def _at(t):
    return dt_util.parse_datetime(t).timestamp()


async def test_a_row_per_month_and_a_correction_rewrites_the_series(
        recorder_mock, hass, phev_entry, period_lines):
    hass.config.currency = "CHF"
    period_lines += months()
    await _setup(hass, phev_entry)
    km, share = "vledger:b8d2_distance_km", "vledger:b8d2_electric_energy_share"
    got = await _rows(hass, km, share, "vledger:b8d2_grid_kwh_per_100km")
    assert [(r["start"], r["state"], r["sum"]) for r in got[km]] == [
        (_at(SEP), 100.0, 100.0),
        (_at("2026-09-30T18:00:00.000Z"), 50.0, 150.0)]        # floored to the hour
    assert [(r["mean"], r["min"], r["max"]) for r in got[share]] == [(40.0, 40.0, 40.0)]
    assert "vledger:b8d2_grid_kwh_per_100km" not in got        # null in every month: no row

    rate = "vledger:b8d2_grid_kwh_per_100km"
    meta = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={km, share, rate, "vledger:b8d2_fuel_cost_eur"}))
    assert meta[km][1]["unit_of_measurement"] == "km" and meta[km][1]["unit_class"] == "distance"
    assert meta[rate][1]["unit_of_measurement"] == "kWh/100km" and meta[rate][1]["unit_class"] == "energy_distance"
    assert meta[km][1]["source"] == "vledger" and meta[km][1]["name"] == "Golf Distance"
    assert meta[km][1]["has_sum"] and not meta[share][1]["has_sum"]
    assert meta[share][1]["unit_of_measurement"] == "%"
    assert meta["vledger:b8d2_fuel_cost_eur"][1]["unit_of_measurement"] == "CHF"

    # A rebuild corrects September: its row is replaced and October's sum moves.
    period_lines[:] = months(sep_km=90.0)
    await hass.async_add_executor_job(l1.write_periods, Path(phev_entry.options["base_path"]), GOLF)
    await phev_entry.runtime_data.statistics.async_import()
    got = await _rows(hass, km)
    assert [(r["state"], r["sum"]) for r in got[km]] == [(90.0, 90.0), (50.0, 140.0)]


async def test_unchanged_months_are_not_written_again(recorder_mock, hass, vehicle_entry,
                                                      period_lines, monkeypatch):
    period_lines += months()
    written = []
    real = external_statistics.async_add_external_statistics
    monkeypatch.setattr(external_statistics, "async_add_external_statistics",
                        lambda hass, meta, rows: written.append(meta["statistic_id"]) or real(hass, meta, rows))
    await _setup(hass, vehicle_entry)
    # A petrol car: the metrics of every vehicle and of fuel, nothing electric.
    assert sorted(written) == sorted(f"vledger:a7c1_{k}" for k in (
        "distance_km", "eur_per_100km", "fuel_purchased_l", "fuel_consumed_l", "fuel_cost_eur",
        "tank_fills"))
    written.clear()
    await vehicle_entry.runtime_data.statistics.async_import()
    assert written == []


async def test_without_a_recorder_nothing_is_imported(hass, vehicle_entry, period_lines, monkeypatch):
    period_lines += months()
    written = []
    monkeypatch.setattr(external_statistics, "async_add_external_statistics",
                        lambda *args: written.append(args))
    await _setup(hass, vehicle_entry)
    assert written == []
