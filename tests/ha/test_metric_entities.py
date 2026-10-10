# SPDX-License-Identifier: BSD-3-Clause
"""The metric entities (ADR-0018): the current month, year and rolling
period's metrics and the lifetime counters — shown as periods.jsonl holds
them, read back with the library."""

import json
from pathlib import Path

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from test_l1writer import KM, T0, _drive, _standstill
from test_receipt_entry import _settle, _setup

from vledger import l1, periods
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
ROOT = Path(__file__).resolve().parents[2] / "custom_components/vledger"

MONTH_START, YEAR_START = "2026-09-30T22:00:00.000Z", "2025-12-31T23:00:00.000Z"


def line(period, start, end, **metrics):
    """A period line as periods.jsonl holds it: every metric, null unless given."""
    out = {"kind": "period", "subject": V.id, "start": start, "end": end, "quality": "measured",
           "version": "x", "period": period, "open": period != "lifetime"}
    for key in periods.METRICS:
        out[key] = metrics.get(key)
        out[key + "_quality"] = "measured" if metrics.get(key) is not None else None
    out.update(fuel_level_corrected=True, soc_corrected=False, gaps=1)
    return out


def _state(hass, entity_id):
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


async def _enable(hass, entry, *entity_ids):
    """Enable entities registered disabled, and reload so they are added."""
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, entry)


def _metric_ids(hass, prefix):
    registry = er.async_get(hass)
    return {e.unique_id.removeprefix(prefix): e for e in registry.entities.values()
            if e.platform == "vledger" and e.unique_id.startswith(prefix)}


async def test_which_metric_entities_a_vehicle_gets(hass, vehicle_entry, phev_entry, chargepoint_entry):
    from custom_components.vledger.sensor import METRICS, PERIODS

    await _setup(hass, vehicle_entry, phev_entry, chargepoint_entry)
    petrol = {"distance_km", "eur_per_100km", "fuel_purchased_l", "fuel_consumed_l",
              "fuel_cost_eur", "tank_fills"}
    volvo = _metric_ids(hass, "a7c1_")
    assert {k for k in volvo if k.endswith("_month")} == {f"{m}_month" for m in petrol}
    assert {"tank_fills_total", "fuel_consumption", "distance_km_total", "fuel_consumed_l_total"} <= set(volvo)
    assert "charge_cycles_total" not in volvo and "grid_kwh_month" not in volvo
    assert "grid_kwh_total" not in volvo and "battery_kwh_per_100km_total" not in volvo
    golf = _metric_ids(hass, "b8d2_")
    assert {k for k in golf if k.endswith(PERIODS)} == {f"{m.key}_{p}" for m in METRICS for p in PERIODS}
    assert {"charge_cycles_total", "tank_fills_total", "fuel_consumption", "distance_km_total",
            "fuel_consumed_l_total", "grid_kwh_total", "battery_kwh_total",
            "grid_kwh_per_100km_total", "battery_kwh_per_100km_total"} <= set(golf)
    assert all(e.disabled_by is None for k, e in golf.items() if k.endswith("_total"))
    assert not _metric_ids(hass, "c9e3_distance")
    # The month enabled, the year and the rolling period registered disabled.
    assert all(e.disabled_by is None for k, e in golf.items() if k.endswith("_month"))
    assert all(e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
               for k, e in golf.items() if k.endswith(("_year", "_rolling")))
    # Capture has just begun: a month with nothing in it yet.
    assert _state(hass, "sensor.golf_distance_this_month").state == "0"
    assert float(_state(hass, "sensor.golf_charge_cycles_in_total").state) == 0


async def test_sums_restart_with_their_period(hass, phev_entry, period_lines):
    hass.config.currency = "CHF"
    period_lines += [
        line("month", MONTH_START, "2026-10-31T23:00:00.000Z", distance_km=412.5, fuel_cost_eur=61.2,
             grid_kwh=33.1, grid_kwh_per_100km=18.25, electric_energy_share=0.4321),
        line("year", YEAR_START, "2026-12-31T23:00:00.000Z", distance_km=9120.0),
        line("rolling", "2026-09-09T10:00:00.000Z", "2026-10-09T10:00:00.000Z", distance_km=380.0),
        line("lifetime", "2026-07-01T08:00:00.000Z", "2026-10-09T10:00:00.000Z", charge_cycles=112.5),
    ]
    await _setup(hass, phev_entry)
    await _enable(hass, phev_entry, "sensor.golf_distance_this_year",
                  "sensor.golf_distance_in_the_rolling_period")

    s = _state(hass, "sensor.golf_distance_this_month")
    assert float(s.state) == 412.5 and s.attributes["unit_of_measurement"] == "km"
    assert s.attributes["device_class"] == "distance" and s.attributes["state_class"] == "total"
    assert dt_util.parse_datetime(s.attributes["last_reset"]) == dt_util.parse_datetime(MONTH_START)
    assert {k: s.attributes[k] for k in ("start", "end", "open", "gaps", "state_quality")} == {
        "start": MONTH_START, "end": "2026-10-31T23:00:00.000Z", "open": True, "gaps": 1,
        "state_quality": "measured"}
    assert "soc_corrected" not in s.attributes            # a metric of every vehicle

    s = _state(hass, "sensor.golf_distance_this_year")
    assert float(s.state) == 9120.0 and s.attributes["state_class"] == "total"
    assert dt_util.parse_datetime(s.attributes["last_reset"]) == dt_util.parse_datetime(YEAR_START)
    # A sliding window is no running sum: no statistics of the wrong kind.
    s = _state(hass, "sensor.golf_distance_in_the_rolling_period")
    assert float(s.state) == 380.0 and "state_class" not in s.attributes
    assert "last_reset" not in s.attributes

    s = _state(hass, "sensor.golf_fuel_cost_this_month")
    assert float(s.state) == 61.2 and s.attributes["unit_of_measurement"] == "CHF"
    assert s.attributes["device_class"] == "monetary" and s.attributes["fuel_level_corrected"] is True
    s = _state(hass, "sensor.golf_grid_energy_this_month")
    assert s.attributes["device_class"] == "energy" and s.attributes["soc_corrected"] is False
    s = _state(hass, "sensor.golf_grid_energy_per_100_km_this_month")
    assert float(s.state) == 18.25 and s.attributes["unit_of_measurement"] == "kWh/100km"
    assert s.attributes["state_class"] == "measurement" and "device_class" not in s.attributes
    assert "last_reset" not in s.attributes
    s = _state(hass, "sensor.golf_cost_per_100_km_this_month")
    assert s.state == "unknown" and s.attributes["unit_of_measurement"] == "CHF/100km"
    assert s.attributes["state_quality"] is None
    s = _state(hass, "sensor.golf_electric_share_of_energy_this_month")
    assert float(s.state) == 43.21 and s.attributes["unit_of_measurement"] == "%"

    # total, never total_increasing: a rebuild may lower a counter.
    s = _state(hass, "sensor.golf_charge_cycles_in_total")
    assert float(s.state) == 112.5 and s.attributes["state_class"] == "total"
    assert "last_reset" not in s.attributes and "unit_of_measurement" not in s.attributes

    # A new month: the sum starts again, and says when.
    period_lines[0] = line("month", "2026-10-31T23:00:00.000Z", "2026-11-30T23:00:00.000Z",
                           distance_km=3.0)
    await hass.async_add_executor_job(l1.write_periods, Path(phev_entry.options["base_path"]),
                                      Subject("vehicle", "b8d2"))
    await phev_entry.runtime_data.view.async_refresh()
    s = _state(hass, "sensor.golf_distance_this_month")
    assert float(s.state) == 3.0
    assert s.attributes["last_reset"].startswith("2026-10-31T23:00:00")


async def test_the_consumption_and_its_interval(hass, vehicle_entry, period_lines):
    lifetime = line("lifetime", "2026-07-01T08:00:00.000Z", "2026-10-09T10:00:00.000Z", tank_fills=12.4)
    lifetime.update(consumption_l_per_100km=6.84, consumption_quality="receipt",
                    consumption_from="2026-09-01T10:00:00.000Z", consumption_to="2026-10-01T10:00:00.000Z",
                    consumption_receipts=3, consumption_error_pct=1.2)
    period_lines.append(lifetime)
    await _setup(hass, vehicle_entry)
    s = _state(hass, "sensor.volvo_fuel_consumption")
    assert float(s.state) == 6.84 and s.attributes["unit_of_measurement"] == "L/100km"
    assert s.attributes["state_class"] == "measurement" and s.attributes["state_quality"] == "receipt"
    assert s.attributes["consumption_receipts"] == 3 and s.attributes["consumption_error_pct"] == 1.2
    assert float(_state(hass, "sensor.volvo_tank_fills_in_total").state) == 12.4


async def test_the_overall_consumption_and_totals_are_the_lifetime_lines(hass, phev_entry, period_lines):
    """ADR-0025, point 6: the electricity rates on the whole distance as
    levels, the sums as totals without a reset, like the counters."""
    lifetime = line("lifetime", "2026-07-01T08:00:00.000Z", "2026-10-09T10:00:00.000Z",
                    distance_km=9120.0, fuel_consumed_l=412.3, grid_kwh=1210.5, battery_kwh=1080.8,
                    grid_kwh_per_100km=13.27, battery_kwh_per_100km=11.85)
    lifetime["soc_corrected"] = True
    period_lines.append(lifetime)
    await _setup(hass, phev_entry)
    s = _state(hass, "sensor.golf_grid_energy_per_100_km_in_total")
    assert float(s.state) == 13.27 and s.attributes["unit_of_measurement"] == "kWh/100km"
    assert s.attributes["state_class"] == "measurement" and "device_class" not in s.attributes
    assert s.attributes["soc_corrected"] is True and s.attributes["state_quality"] == "measured"
    assert "last_reset" not in s.attributes
    s = _state(hass, "sensor.golf_battery_energy_per_100_km_in_total")
    assert float(s.state) == 11.85
    s = _state(hass, "sensor.golf_distance_in_total")
    assert float(s.state) == 9120.0 and s.attributes["device_class"] == "distance"
    assert s.attributes["state_class"] == "total" and "last_reset" not in s.attributes
    assert "soc_corrected" not in s.attributes
    s = _state(hass, "sensor.golf_fuel_consumed_in_total")
    assert float(s.state) == 412.3 and s.attributes["unit_of_measurement"] == "L"
    assert s.attributes["fuel_level_corrected"] is True
    s = _state(hass, "sensor.golf_grid_energy_in_total")
    assert float(s.state) == 1210.5 and s.attributes["device_class"] == "energy"
    assert float(_state(hass, "sensor.golf_battery_energy_in_total").state) == 1080.8
    assert {"start", "end", "gaps", "state_quality"} <= set(s.attributes)


async def test_a_trip_reaches_the_month_through_l1(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    hass.states.async_set("sensor.volvo_odometer", "1000", KM)
    hass.states.async_set("device_tracker.volvo", "home",
                          {"latitude": 51.0, "longitude": 7.0, "gps_accuracy": 10})
    await _setup(hass, vehicle_entry)
    end = await _drive(hass, vehicle_entry, freezer, 60, 1000)
    await _standstill(hass, vehicle_entry, freezer, end + 40)

    month = l1.current_periods(tmp_path, V)["month"]
    s = _state(hass, "sensor.volvo_distance_this_month")
    assert float(s.state) == month["distance_km"] == 14
    assert s.attributes["start"] == month["start"]


async def test_unavailable_while_l1_is_rebuilt(hass, vehicle_entry):
    await _setup(hass, vehicle_entry)
    capture = vehicle_entry.runtime_data.capture
    capture.set_recomputing(True)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.volvo_distance_this_month").state == "unavailable"
    assert _state(hass, "sensor.volvo_fuel_consumption").state == "unavailable"
    capture.set_recomputing(False)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.volvo_distance_this_month").state == "0"


def test_every_metric_has_a_name_and_an_icon():
    from custom_components.vledger.sensor import (
        LIFETIME_SENSORS,
        METRICS,
        PERIODS,
        corrected,
    )

    assert {m.key for m in METRICS} == set(periods.METRICS)
    keys = {f"{m.key}_{p}": m for m in METRICS for p in PERIODS}
    keys.update({d.key: None for d in LIFETIME_SENSORS})
    icons = json.loads((ROOT / "icons.json").read_text())["entity"]["sensor"]
    assert set(keys) <= set(icons)
    for lang in ("strings.json", "translations/en.json", "translations/de.json"):
        sensors = json.loads((ROOT / lang).read_text())["entity"]["sensor"]
        for key, m in keys.items():
            assert sensors[key]["name"], (lang, key)
            if m is not None:
                named = set(sensors[key]["state_attributes"])
                assert {"start", "end", "open", "gaps", "state_quality", *corrected(m.needs)} == named
