# SPDX-License-Identifier: BSD-3-Clause
"""The event entities (ADR-0016): the last trip, refuelling and charging
session and the count of what waits for a receipt — shown as L1 holds them,
read back with the library — and the last trip's consumption (ADR-0025)."""

import dataclasses
import json
from pathlib import Path

import pytest
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from test_l1writer import KM, T0, _drive, _standstill
from test_receipt_entry import _act, _settle, _setup, at

import vledger.cli  # noqa: F401 — the real derivations register first, so a stand-in replaces one
from vledger import charging, l1, receipts, refuellings, trips
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
ROOT = Path(__file__).resolve().parents[2] / "custom_components/vledger"


def refuelling(minutes, delta_l=40.0):
    return {"kind": "refuelling", "subject": V.id, "start": at(minutes), "end": at(minutes + 2),
            "quality": "measured", "position": {"latitude": 51.0, "longitude": 7.0},
            "sensor_delta_l": delta_l, "sensor_delta_quality": "estimated", "version": "x"}


def trip(minutes, km, **consumption):
    return {"kind": "trip", "subject": V.id, "start": at(minutes), "end": at(minutes + 30),
            "quality": "measured", "distance_km": km, "distance_quality": "measured",
            "distance_source": "odometer", "start_position": {"latitude": 51.0, "longitude": 7.0},
            "waypoints": [{"t": at(minutes), "latitude": 51.0, "longitude": 7.0}], "version": "x",
            **consumption}


def consumption(**given):
    """The consumption keys of a trip line (ADR-0025, point 1), null unless given."""
    keys = ("fuel_consumed_l", "fuel_consumed_quality", "fuel_consumed_source", "fuel_consumed_error_l",
            "fuel_l_per_100km", "fuel_l_per_100km_quality", "fuel_l_per_100km_error_pct",
            "battery_consumed_kwh", "battery_consumed_quality", "battery_consumed_error_kwh",
            "battery_kwh_per_100km", "battery_kwh_per_100km_quality", "battery_kwh_per_100km_error_pct")
    return {k: given.get(k) for k in keys}


def session(minutes, kwh):
    return {"kind": "charging", "subject": V.id, "start": at(minutes), "end": at(minutes + 60),
            "quality": "measured", "chargepoint": "foreign", "delta_soc_pct": 50.0,
            "grid_kwh": kwh, "grid_kwh_quality": "estimated", "version": "x"}


def _state(hass, entity_id):
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


async def test_which_entities_a_vehicle_gets(hass, vehicle_entry, phev_entry, chargepoint_entry):
    await _setup(hass, vehicle_entry, phev_entry, chargepoint_entry)
    ids = set(hass.states.async_entity_ids("sensor"))
    event_sensors = {"last_trip", "last_refuelling", "last_charging_session", "waiting_for_a_receipt",
                     "last_trip_fuel_consumption", "last_trip_electricity_consumption"}
    # Petrol only: no charging session and no electricity consumption to show.
    assert {i for i in ids if i.removeprefix("sensor.volvo_") in event_sensors} == {
        "sensor.volvo_last_trip", "sensor.volvo_last_refuelling", "sensor.volvo_waiting_for_a_receipt",
        "sensor.volvo_last_trip_fuel_consumption"}
    assert {i for i in ids if i.removeprefix("sensor.golf_") in event_sensors} == {
        f"sensor.golf_{k}" for k in event_sensors}
    assert not [i for i in ids if i.removeprefix("sensor.home_") in event_sensors]
    # Nothing derived yet: nothing to show, nothing waiting.
    trip_state = _state(hass, "sensor.volvo_last_trip")
    assert trip_state.state == "unknown" and "start" not in trip_state.attributes
    assert _state(hass, "sensor.volvo_waiting_for_a_receipt").state == "0"


async def test_a_completed_trip_is_shown_as_l1_holds_it(hass, vehicle_entry, tmp_path, freezer):
    freezer.move_to(T0)
    hass.states.async_set("sensor.volvo_odometer", "1000", KM)
    hass.states.async_set("device_tracker.volvo", "home",
                          {"latitude": 51.0, "longitude": 7.0, "gps_accuracy": 10})
    await _setup(hass, vehicle_entry)
    end = await _drive(hass, vehicle_entry, freezer, 60, 1000)
    await _standstill(hass, vehicle_entry, freezer, end + 20)
    assert _state(hass, "sensor.volvo_last_trip").state == "unknown"   # still open: not in L1
    await _standstill(hass, vehicle_entry, freezer, end + 40)

    [event] = l1.last(tmp_path, V, "trip")
    s = _state(hass, "sensor.volvo_last_trip")
    assert float(s.state) == event["distance_km"] == 14       # from the first movement on
    assert s.attributes["unit_of_measurement"] == "km"
    assert s.attributes["device_class"] == "distance"
    assert "state_class" not in s.attributes                    # one event, not a statistic
    expected = {k: v for k, v in event.items() if k not in ("kind", "subject", "version", "waypoints")}
    shown = {k: v for k, v in s.attributes.items() if k in event or k == "state_quality"}
    assert shown == {**expected, "state_quality": event["distance_quality"]}


async def test_the_last_refuelling_follows_its_receipt(hass, vehicle_entry, tmp_path, stand_in):
    stand_in["refuelling"] += [refuelling(0, 38.5), refuelling(60 * 24, 40.0)]
    await _setup(hass, vehicle_entry)
    s = _state(hass, "sensor.volvo_last_refuelling")
    assert float(s.state) == 40.0 and s.attributes["unit_of_measurement"] == "L"
    assert s.attributes["state_quality"] == "estimated"
    assert s.attributes["confirmation"] == "unconfirmed" and s.attributes["receipt"] is None
    waiting = _state(hass, "sensor.volvo_waiting_for_a_receipt")
    assert waiting.state == "2" and waiting.attributes["state_class"] == "measurement"
    assert waiting.attributes["refuelling_unconfirmed"] == 2
    assert waiting.attributes["refuelling_ambiguous"] == 0
    assert "charging_unconfirmed" not in waiting.attributes      # a petrol car

    line = await _act(hass, "add_refuelling_receipt", config_entry_id=vehicle_entry.entry_id,
                      from_candidate=at(60 * 24), quantity_l=41.2, full=True, total_price=72)
    await _settle(hass, vehicle_entry)
    s = _state(hass, "sensor.volvo_last_refuelling")
    assert float(s.state) == 41.2                               # the receipt's, before the sensor's
    assert s.attributes["state_quality"] == "receipt"
    assert s.attributes["receipt"] == line["id"] and s.attributes["sensor_delta_l"] == 40.0
    assert _state(hass, "sensor.volvo_waiting_for_a_receipt").state == "1"


async def test_display_follows_the_unit_system_and_attributes_stay_l1(hass, phev_entry, stand_in):
    hass.config.units = US_CUSTOMARY_SYSTEM
    stand_in["trip"].append(trip(0, 16.09344))
    stand_in["refuelling"].append(refuelling(60, 37.854118))
    stand_in["charging"].append(session(120, 10.5))
    await _setup(hass, phev_entry)

    s = _state(hass, "sensor.golf_last_trip")
    assert s.attributes["unit_of_measurement"] == "mi" and float(s.state) == pytest.approx(10, abs=0.05)
    assert s.attributes["distance_km"] == 16.09344             # the L1 line, unconverted
    assert "waypoints" not in s.attributes and "kind" not in s.attributes
    s = _state(hass, "sensor.golf_last_refuelling")
    assert s.attributes["unit_of_measurement"] == "gal" and float(s.state) == pytest.approx(10, abs=0.005)
    s = _state(hass, "sensor.golf_last_charging_session")
    assert s.attributes["unit_of_measurement"] == "kWh" and float(s.state) == 10.5
    assert s.attributes["state_quality"] == "estimated"
    # A session at a foreign charge point waits for its receipt too.
    waiting = _state(hass, "sensor.golf_waiting_for_a_receipt")
    assert waiting.state == "2" and waiting.attributes["charging_unconfirmed"] == 1


async def test_unavailable_while_l1_is_rebuilt(hass, vehicle_entry):
    await _setup(hass, vehicle_entry)
    capture = vehicle_entry.runtime_data.capture
    capture.set_recomputing(True)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.volvo_last_trip").state == "unavailable"
    assert _state(hass, "sensor.volvo_waiting_for_a_receipt").state == "unavailable"
    capture.set_recomputing(False)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.volvo_last_trip").state == "unknown"


async def test_the_last_trips_consumption_is_a_slice_of_its_line(hass, phev_entry, stand_in):
    """ADR-0025, point 5: the rate as the state, the keys it rests on as
    the attributes, no state class, the unit as it is."""
    stand_in["trip"].append(trip(0, 25.0, **consumption(
        fuel_consumed_l=1.75, fuel_consumed_quality="measured", fuel_consumed_source="trip_computer",
        fuel_consumed_error_l=0.113, fuel_l_per_100km=7.0, fuel_l_per_100km_quality="measured",
        fuel_l_per_100km_error_pct=6.4, battery_consumed_kwh=0.05, battery_consumed_quality="estimated",
        battery_consumed_error_kwh=0.208)))      # the battery figure is swallowed by its error
    await _setup(hass, phev_entry)
    s = _state(hass, "sensor.golf_last_trip_fuel_consumption")
    assert float(s.state) == 7.0 and s.attributes["unit_of_measurement"] == "L/100 km"
    assert "state_class" not in s.attributes and "device_class" not in s.attributes
    assert s.attributes["state_quality"] == "measured"
    assert s.attributes["fuel_consumed_source"] == "trip_computer"
    assert s.attributes["fuel_consumed_error_l"] == 0.113 and s.attributes["fuel_l_per_100km_error_pct"] == 6.4
    assert s.attributes["distance_km"] == 25.0 and s.attributes["start"] == at(0)
    assert "battery_consumed_kwh" not in s.attributes and "waypoints" not in s.attributes
    e = _state(hass, "sensor.golf_last_trip_electricity_consumption")
    assert e.state == "unknown" and e.attributes["unit_of_measurement"] == "kWh/100 km"
    assert e.attributes["battery_consumed_kwh"] == 0.05 and e.attributes["state_quality"] is None
    assert "fuel_consumed_l" not in e.attributes
    # The whole line stays the last trip's.
    assert _state(hass, "sensor.golf_last_trip").attributes["fuel_l_per_100km"] == 7.0


def test_positions_stay_out_of_the_recorder():
    from custom_components.vledger.sensor import LastEventSensor

    assert {"start_position", "end_position", "position"} <= LastEventSensor._unrecorded_attributes


def _keys(kind: str, dataclass) -> set[str]:
    """Every key an event of the kind can carry: its derivation's fields,
    and what a receipt or a waiting confirmation adds to them."""
    event = {f.name: None for f in dataclasses.fields(dataclass)}
    event["kind"] = kind
    if kind == "charging":      # what makes a receipt add the sensor's values beside its own
        event.update(grid_kwh=10.0, delta_soc_pct=50.0, battery_kwh=9.0)
    build = {"refuelling": lambda: receipts.refuelling(at(0), V, anchor=at(0), quantity_l=40, full=True,
                                                       total_price=70),
             "charging": lambda: receipts.charging(at(0), V, anchor=at(0), energy_kwh=10, total_price=5)}
    keys = set(event)
    if kind in build:
        keys |= set(receipts._carry(event, build[kind](), 15)) | set(receipts._waiting(event, ["r"]))
    return keys - {"kind", "subject", "version", "waypoints"} | {"state_quality"}


def test_every_attribute_has_a_name_in_both_languages():
    from custom_components.vledger.sensor import EVENT_SENSORS

    shapes = {"trip": trips.Trip, "refuelling": refuellings.Refuelling, "charging": charging.Session}
    from custom_components.vledger.sensor import CONSUMPTION_SENSORS

    for lang in ("strings.json", "translations/en.json", "translations/de.json"):
        sensors = json.loads((ROOT / lang).read_text())["entity"]["sensor"]
        for d in EVENT_SENSORS:
            named = set(sensors[d.translation_key]["state_attributes"])
            assert _keys(d.kind, shapes[d.kind]) <= named, (lang, d.kind)
        for d in CONSUMPTION_SENSORS:
            named = set(sensors[d.translation_key]["state_attributes"])
            assert set(d.attributes) | {"state_quality"} <= named, (lang, d.key)
        waiting = set(sensors["unconfirmed_candidates"]["state_attributes"])
        assert waiting == {f"{k}_{s}" for k in receipts.EVENT_KINDS for s in l1.WAITING}
