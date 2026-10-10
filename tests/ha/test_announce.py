# SPDX-License-Identifier: BSD-3-Clause
"""Events and notifications (ADR-0020): what an incremental run appends is
fired on the bus and, for a vehicle with a notify target, sent to a person;
a rebuild fires nothing."""

import logging

from custom_components.vledger.const import OPT_NOTIFY_TARGET
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_mock_service,
)
from test_receipt_entry import _act, _settle, _setup, at

from vledger import l1
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")


def refuelling(minutes, delta_l=40.0, price=None):
    return {"kind": "refuelling", "subject": V.id, "start": at(minutes), "end": at(minutes + 2),
            "quality": "measured", "position": {"latitude": 51.0, "longitude": 7.0},
            "sensor_delta_l": delta_l, "sensor_delta_quality": "estimated",
            "price_suggestion": price, "version": "x"}


def trip(minutes, km=12.0):
    return {"kind": "trip", "subject": V.id, "start": at(minutes), "end": at(minutes + 30),
            "quality": "measured", "distance_km": km, "distance_quality": "measured",
            "start_position": {"latitude": 51.0, "longitude": 7.0},
            "end_position": {"latitude": 51.1, "longitude": 7.1},
            "waypoints": [{"t": at(minutes), "latitude": 51.0, "longitude": 7.0}], "version": "x"}


def _local(minutes) -> str:
    return dt_util.as_local(dt_util.parse_datetime(at(minutes))).strftime("%Y-%m-%d %H:%M")


async def _run(hass, entry):
    """One incremental run of the writer, as a receipt or heartbeat asks for."""
    entry.runtime_data.l1.async_derive_soon()
    await _settle(hass, entry)


def _target(hass, entry, target):
    hass.config_entries.async_update_entry(entry, options={**entry.options, OPT_NOTIFY_TARGET: target})


async def test_an_incremental_run_fires_what_is_new_and_a_rebuild_nothing(hass, vehicle_entry, stand_in):
    events = async_capture_events(hass, "vledger_event")
    candidates = async_capture_events(hass, "vledger_candidate")
    stand_in["trip"].append(trip(0))
    await _setup(hass, vehicle_entry)                       # the first run: a rebuild
    assert events == [] and candidates == []

    stand_in["trip"].append(trip(60))
    stand_in["refuelling"].append(refuelling(120, 38.5, price=1.799))
    await _run(hass, vehicle_entry)
    assert [(e.data["kind"], e.data["start"]) for e in events] == [("trip", at(60)), ("refuelling", at(120))]
    (c,) = candidates
    assert c.data == events[1].data
    assert c.data["config_entry_id"] == vehicle_entry.entry_id and c.data["vehicle"] == "Volvo"
    assert c.data["subject"] == V.id and c.data["confirmation"] == "unconfirmed"
    assert c.data["sensor_delta_l"] == 38.5 and c.data["price_suggestion"] == 1.799
    for e in events:            # the L1 line as the entities show it, without positions
        assert not {"version", "waypoints", "position", "start_position", "end_position"} & set(e.data)
    assert events[0].data["distance_km"] == 12.0

    await _run(hass, vehicle_entry)                         # nothing completed since
    assert len(events) == 2
    await hass.services.async_call("vledger", "recompute", {}, blocking=True)
    await _settle(hass, vehicle_entry)
    assert len(events) == 2                                 # history, not news
    # A receipt confirms the candidate: a changed event, not a new one.
    await _act(hass, "add_refuelling_receipt", config_entry_id=vehicle_entry.entry_id,
               from_candidate=at(120), quantity_l=38.9, full=True, total_price=70)
    await _settle(hass, vehicle_entry)
    assert len(events) == 2 and len(candidates) == 1


async def test_a_candidate_is_notified_with_an_action_on_the_companion_app(hass, vehicle_entry, stand_in):
    calls = async_mock_service(hass, "notify", "mobile_app_phone")
    await _setup(hass, vehicle_entry)
    base = vehicle_entry.runtime_data.capture.base
    before = l1.config_hash(base, V)
    _target(hass, vehicle_entry, "notify.mobile_app_phone")
    await _settle(hass, vehicle_entry)                      # the options reload the entry
    assert vehicle_entry.runtime_data.announcer.target == "notify.mobile_app_phone"
    assert l1.config_hash(base, V) == before                # not in the config line: no rebuild due

    stand_in["trip"].append(trip(0))
    stand_in["refuelling"].append(refuelling(60, 38.5, price=1.799))
    await _run(hass, vehicle_entry)
    (call,) = calls                                         # the trip waits for nobody
    assert call.data["title"] == "Volvo: refuelling detected"
    assert call.data["message"] == f"Started {_local(60)}, about 38.5 L. Suggested price: 1.799 per litre."
    device = dr.async_get(hass).async_get_device(identifiers={("vledger", V.id)})
    assert call.data["data"] == {
        "tag": f"vledger-{V.id}-refuelling-{at(60)}",
        "actions": [{"action": "URI", "title": "Enter receipt", "uri": f"/config/devices/device/{device.id}"}]}


async def test_more_than_three_candidates_are_one_summary_and_other_targets_get_no_action(
        hass, vehicle_entry, stand_in):
    calls = async_mock_service(hass, "notify", "family")
    vehicle_entry.add_to_hass(hass)
    _target(hass, vehicle_entry, "notify.family")
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)

    stand_in["refuelling"].append(refuelling(0, delta_l=None))
    await _run(hass, vehicle_entry)
    (call,) = calls
    assert call.data["message"] == f"Started {_local(0)}."   # no quantity, no price: nothing invented
    assert "actions" not in call.data["data"]

    stand_in["refuelling"].extend(refuelling(60 * 24 * d) for d in (1, 2, 3, 4))
    await _run(hass, vehicle_entry)
    assert len(calls) == 2
    assert calls[1].data["title"] == "Volvo: 4 events wait for a receipt"
    assert calls[1].data["data"] == {"tag": f"vledger-{V.id}-summary"}


async def test_a_refusing_or_missing_target_is_logged_and_the_events_still_fire(
        hass, vehicle_entry, stand_in, caplog):
    def refuse(call):
        raise HomeAssistantError("offline")

    hass.services.async_register("notify", "mobile_app_phone", refuse)
    candidates = async_capture_events(hass, "vledger_candidate")
    vehicle_entry.add_to_hass(hass)
    _target(hass, vehicle_entry, "notify.mobile_app_phone")
    assert await hass.config_entries.async_setup(vehicle_entry.entry_id)
    await _settle(hass, vehicle_entry)
    stand_in["refuelling"].append(refuelling(0))
    with caplog.at_level(logging.WARNING):
        await _run(hass, vehicle_entry)
    assert len(candidates) == 1
    assert "refused a notification" in caplog.text
    assert len(list(l1.read(vehicle_entry.runtime_data.capture.base, V, "refuelling"))) == 1


async def test_the_options_set_and_clear_the_target(hass, vehicle_entry, chargepoint_entry):
    async_mock_service(hass, "notify", "mobile_app_phone")
    async_mock_service(hass, "notify", "send_message")
    await _setup(hass, vehicle_entry, chargepoint_entry)
    assert chargepoint_entry.runtime_data.announcer is None

    async def step(entry, data=None):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert "notifications" in result["menu_options"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "notifications"})
        if data is None:
            return result
        result = await hass.config_entries.options.async_configure(result["flow_id"], data)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await _settle(hass, entry)
        return result

    result = await step(vehicle_entry)
    (field,) = result["data_schema"].schema
    options = result["data_schema"].schema[field].config["options"]
    assert "notify.mobile_app_phone" in options and "notify.send_message" not in options
    await step(vehicle_entry, {OPT_NOTIFY_TARGET: "notify.mobile_app_phone"})
    assert vehicle_entry.options[OPT_NOTIFY_TARGET] == "notify.mobile_app_phone"
    await step(vehicle_entry, {})
    assert OPT_NOTIFY_TARGET not in vehicle_entry.options

    result = await hass.config_entries.options.async_init(chargepoint_entry.entry_id)
    assert "notifications" not in result["menu_options"]
