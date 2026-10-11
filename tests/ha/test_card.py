# SPDX-License-Identifier: BSD-3-Clause
"""The Lovelace card's Home Assistant side (ADR-0039): the module served
and added to every frontend, and vledger.read_events, which is all the
card reads. The card's own logic is tested by tests/frontend/."""

import json
from pathlib import Path

import pytest
from custom_components.vledger import card
from custom_components.vledger.const import DOMAIN
from homeassistant.components import frontend
from homeassistant.exceptions import ServiceValidationError
from test_event_entities import refuelling, trip
from test_receipt_entry import _act, _settle, _setup, at

ROOT = Path(__file__).parents[2] / "custom_components" / DOMAIN


async def _read(hass, entry, **data):
    return await _act(hass, "read_events", config_entry_id=entry.entry_id, **data)


async def test_the_card_is_served_and_loaded_in_every_frontend(hass, hass_client, vehicle_entry):
    await _setup(hass, vehicle_entry)
    version = json.loads((ROOT / "manifest.json").read_text())["version"]
    url = f"/{DOMAIN}/frontend/vledger-card.js?v={version}"
    assert hass.data[frontend.DATA_EXTRA_MODULE_URL].urls == frozenset({url})
    client = await hass_client()
    for name in ("vledger-card.js", "model.js"):
        response = await client.get(f"/{DOMAIN}/frontend/{name}?v={version}")
        assert response.status == 200
        assert await response.text() == (card.DIRECTORY / name).read_text()
    assert (await client.get(f"/{DOMAIN}/frontend/../manifest.json")).status == 404


async def test_read_events_answers_the_last_lines_newest_first_as_the_entities_show_them(
        hass, vehicle_entry, stand_in):
    stand_in["trip"] += [trip(0, 12.5), trip(60, 3.0), trip(120, 7.25)]
    await _setup(hass, vehicle_entry)
    answer = await _read(hass, vehicle_entry, kind="trip", last=2)
    assert [e["start"] for e in answer["events"]] == [at(120), at(60)]
    line = answer["events"][0]
    assert line["distance_km"] == 7.25 and line["start_position"] == {"latitude": 51.0, "longitude": 7.0}
    assert not {"kind", "subject", "version", "waypoints"} & line.keys()   # ADR-0016, point 3
    state = hass.states.get("sensor.volvo_last_trip")
    assert {k: v for k, v in state.attributes.items() if k in line} == line
    assert len((await _read(hass, vehicle_entry, kind="trip"))["events"]) == 3   # default 10
    assert await _read(hass, vehicle_entry, kind="charging") == {"events": []}   # no such file


async def test_read_events_waiting_is_the_librarys_test(hass, vehicle_entry, stand_in):
    """The card's candidates: is_waiting decides, nothing in JavaScript.
    The library's own test covers the ambiguous ones."""
    stand_in["refuelling"] += [refuelling(0), refuelling(60), refuelling(120), refuelling(180)]
    await _setup(hass, vehicle_entry)
    await _act(hass, "add_refuelling_receipt", config_entry_id=vehicle_entry.entry_id,
               from_candidate=at(60), quantity_l=40.0, full=True, total_price=70.0)
    await _settle(hass, vehicle_entry)
    answer = await _read(hass, vehicle_entry, kind="refuelling", waiting=True)
    assert [e["start"] for e in answer["events"]] == [at(180), at(120), at(0)]
    assert {e["confirmation"] for e in answer["events"]} == {"unconfirmed"}
    answer = await _read(hass, vehicle_entry, kind="refuelling", last=2, waiting=True)
    assert [e["start"] for e in answer["events"]] == [at(180), at(120)]
    every = await _read(hass, vehicle_entry, kind="refuelling")
    assert [e["confirmation"] for e in every["events"]][2] == "receipt"


async def test_read_events_names_a_loaded_vehicle_and_keeps_to_its_bounds(
        hass, vehicle_entry, chargepoint_entry):
    await _setup(hass, vehicle_entry, chargepoint_entry)
    with pytest.raises(ServiceValidationError) as e:
        await _read(hass, chargepoint_entry, kind="trip")
    assert e.value.translation_key == "not_a_vehicle"
    with pytest.raises(ServiceValidationError) as e:
        await _act(hass, "read_events", config_entry_id="nope", kind="trip")
    assert e.value.translation_key == "entry_not_loaded"
    for bad in ({"kind": "period"}, {"kind": "trip", "last": 0}, {"kind": "trip", "last": 51}):
        with pytest.raises(Exception, match="value must be|not a valid value"):
            await _read(hass, vehicle_entry, **bad)
    # An answer is what it is for: the action is never called without one.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "read_events",
                                       {"config_entry_id": vehicle_entry.entry_id, "kind": "trip"},
                                       blocking=True)


def test_the_action_has_its_strings_and_icon():
    for lang in ("strings.json", "translations/en.json", "translations/de.json"):
        service = json.loads((ROOT / lang).read_text())["services"]["read_events"]
        assert service["name"] and service["description"]
        assert set(service["fields"]) == {"config_entry_id", "kind", "last", "waiting"}
    assert json.loads((ROOT / "icons.json").read_text())["services"]["read_events"]
