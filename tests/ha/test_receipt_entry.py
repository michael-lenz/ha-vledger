# SPDX-License-Identifier: BSD-3-Clause
"""Receipt entry in Home Assistant (ADR-0015): the three actions and the
dashboard form, read back with the library."""

from datetime import timedelta

import pytest
from custom_components.vledger.const import DOMAIN
from homeassistant.exceptions import ServiceValidationError

import vledger.derivations  # noqa: F401 — the real derivations register first, so a stand-in replaces one
from vledger import clock, l1, receipts
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
P = Subject("vehicle", "b8d2")
T0 = clock.parse("2026-10-09T10:00:00Z")


def at(minutes: float) -> str:
    return clock.to_text(T0 + timedelta(minutes=minutes))


def refuelling(minutes, delta_l=40.0):
    return {"kind": "refuelling", "subject": V.id, "start": at(minutes), "end": at(minutes + 2),
            "quality": "measured", "sensor_delta_l": delta_l, "version": "x"}


@pytest.fixture
def detected(monkeypatch):
    """A stand-in refuelling derivation: the events it is given."""
    events: list[dict] = []
    monkeypatch.setitem(l1.DERIVATIONS, "refuelling", lambda base, subject, since: list(events))
    return events


async def _settle(hass, *entries):
    """Let every capture drain and every run its writer started finish."""
    for _ in range(3):
        await hass.async_block_till_done(wait_background_tasks=True)
        for entry in entries:
            await entry.runtime_data.capture.async_flushed()
    await hass.async_block_till_done(wait_background_tasks=True)


async def _setup(hass, *entries):
    for entry in entries:
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, *entries)


async def _act(hass, service, **data):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


def _current(base, subject=V):
    return receipts.ledger(base, subject).current()


# --- the actions -------------------------------------------------------

async def test_the_actions_add_and_cancel_and_answer_with_the_line(hass, vehicle_entry, tmp_path):
    await _setup(hass, vehicle_entry)
    line = await _act(hass, "add_refuelling_receipt", config_entry_id=vehicle_entry.entry_id,
                      anchor=at(0), quantity_l=41.37, full=True, total_price=72.36, place="A9")
    assert line["kind"] == "refuelling" and line["exact"] is False
    assert line["anchor"] == at(0) and line["quantity_l"] == 41.37 and line["place"] == "A9"
    assert [r["id"] for r in _current(tmp_path)] == [line["id"]]

    fixed = await _act(hass, "add_refuelling_receipt", config_entry_id=vehicle_entry.entry_id,
                       anchor=at(0), quantity_l=41.73, full=True, unit_price=1.749,
                       replaces=line["id"])
    charge = await _act(hass, "add_charging_receipt", config_entry_id=vehicle_entry.entry_id,
                        anchor=at(60), energy_kwh=11.8, total_price=6.49, provider="roaming")
    assert {r["id"] for r in _current(tmp_path)} == {fixed["id"], charge["id"]}

    gone = await _act(hass, "cancel_receipt", config_entry_id=vehicle_entry.entry_id,
                      receipt=fixed["id"], note="duplicate")
    assert gone["kind"] == "cancel" and gone["cancels"] == fixed["id"]
    assert [r["id"] for r in _current(tmp_path)] == [charge["id"]]


async def test_an_action_takes_the_candidates_time_exactly(hass, vehicle_entry, tmp_path, detected):
    detected += [refuelling(0), refuelling(30)]
    await _setup(hass, vehicle_entry)
    line = await _act(hass, "add_refuelling_receipt", config_entry_id=vehicle_entry.entry_id,
                      from_candidate=at(30), quantity_l=40, full=True, total_price=70)
    assert line["exact"] is True and line["anchor"] == at(30)


async def test_a_refusal_writes_nothing_and_says_why(hass, vehicle_entry, tmp_path, detected):
    detected.append(refuelling(0))
    await _setup(hass, vehicle_entry)
    cases = [
        ({"from_candidate": at(31), "quantity_l": 4, "full": True, "total_price": 1}, "no refuelling event"),
        ({"quantity_l": 4, "full": True, "total_price": 1}, "say when"),
        ({"anchor": at(0), "from_candidate": at(0), "quantity_l": 4, "full": True, "total_price": 1},
         "exactly one"),
        ({"anchor": at(0), "quantity_l": 4, "full": True}, "total price"),
        ({"anchor": at(0), "quantity_l": 0, "full": True, "total_price": 1}, "positive"),
        ({"anchor": at(0), "quantity_l": 4, "full": True, "total_price": 1, "replaces": "nope"},
         "no receipt nope"),
    ]
    for data, reason in cases:
        with pytest.raises(ServiceValidationError) as e:
            await _act(hass, "add_refuelling_receipt", config_entry_id=vehicle_entry.entry_id, **data)
        assert e.value.translation_key == "refused"
        assert reason in e.value.translation_placeholders["reason"]
    with pytest.raises(ServiceValidationError) as e:
        await _act(hass, "cancel_receipt", config_entry_id=vehicle_entry.entry_id, receipt="nope")
    assert e.value.translation_key == "refused"
    assert _current(tmp_path) == []


async def test_the_actions_name_a_loaded_vehicle(hass, vehicle_entry, chargepoint_entry):
    await _setup(hass, vehicle_entry, chargepoint_entry)
    data = {"anchor": at(0), "energy_kwh": 5, "total_price": 2}
    for entry_id, key in ((chargepoint_entry.entry_id, "not_a_vehicle"), ("nothing", "entry_not_loaded")):
        with pytest.raises(ServiceValidationError) as e:
            await _act(hass, "add_charging_receipt", config_entry_id=entry_id, **data)
        assert e.value.translation_key == key
    # Unloaded, the action is still there and says so (ADR-0015, 1).
    assert await hass.config_entries.async_unload(vehicle_entry.entry_id)
    with pytest.raises(ServiceValidationError) as e:
        await _act(hass, "add_charging_receipt", config_entry_id=vehicle_entry.entry_id, **data)
    assert e.value.translation_key == "entry_not_loaded"


# --- the form ----------------------------------------------------------

async def _set(hass, domain, entity_id, **data):
    service = {"number": "set_value", "datetime": "set_value", "text": "set_value",
               "select": "select_option"}[domain]
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


async def _press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_the_forms_follow_the_parameters(hass, vehicle_entry, phev_entry, chargepoint_entry):
    await _setup(hass, vehicle_entry, phev_entry, chargepoint_entry)
    ids = hass.states.async_entity_ids()
    volvo = [i for i in ids if "volvo_refuelling" in i or "volvo_charging_receipt" in i
             or "volvo_enter" in i]
    # The form's entities, not the sensor counting what waits for a receipt.
    golf = [i for i in ids if "golf_" in i and "receipt" in i and not i.startswith("sensor.")]
    assert len(volvo) == 9                         # petrol only: the refuelling form
    assert len(golf) == 16                         # a plug-in hybrid: both forms
    assert not [i for i in ids if "home_" in i and "receipt" in i]
    assert hass.states.get("switch.golf_refuelling_receipt_full_tank").state == "on"
    assert hass.states.get("select.golf_charging_receipt_event").state == "manual"


async def test_the_form_enters_a_receipt_and_clears(hass, vehicle_entry, tmp_path):
    await _setup(hass, vehicle_entry)
    await _set(hass, "number", "number.volvo_refuelling_receipt_litres", value=41.37)
    await _set(hass, "number", "number.volvo_refuelling_receipt_total_price", value=72.36)
    await _set(hass, "text", "text.volvo_refuelling_receipt_place", value="A9")
    await hass.services.async_call("switch", "turn_off",
                                   {"entity_id": "switch.volvo_refuelling_receipt_full_tank"}, blocking=True)

    # No time yet: refused, and the form keeps what was typed.
    with pytest.raises(ServiceValidationError):
        await _press(hass, "button.volvo_enter_refuelling_receipt")
    assert hass.states.get("number.volvo_refuelling_receipt_litres").state == "41.37"
    assert _current(tmp_path) == []

    await _set(hass, "datetime", "datetime.volvo_refuelling_receipt_time", datetime=at(0))
    await _press(hass, "button.volvo_enter_refuelling_receipt")
    (r,) = _current(tmp_path)
    assert (r["anchor"], r["exact"], r["quantity_l"], r["total_price"], r["full"], r["place"]) == \
        (at(0), False, 41.37, 72.36, False, "A9")
    assert r["unit_price"] is None and r["note"] is None
    assert hass.states.get("number.volvo_refuelling_receipt_litres").state == "unknown"
    assert hass.states.get("text.volvo_refuelling_receipt_place").state == ""
    assert hass.states.get("switch.volvo_refuelling_receipt_full_tank").state == "on"
    assert hass.states.get("datetime.volvo_refuelling_receipt_time").state == "unknown"


async def test_the_form_offers_the_unconfirmed_candidates(hass, vehicle_entry, tmp_path, detected):
    detected += [refuelling(0, delta_l=20.0), refuelling(300, delta_l=41.4)]
    # The writer's first rebuild puts both into L1, and the select reads L1 (ARC-05).
    await _setup(hass, vehicle_entry)

    select = "select.volvo_refuelling_receipt_event"
    options = hass.states.get(select).attributes["options"]
    assert options[0] == "manual" and len(options) == 3
    assert options[1].endswith("41.4 L") and options[2].endswith("20.0 L")   # newest first

    await _set(hass, "select", select, option=options[1])
    await _set(hass, "number", "number.volvo_refuelling_receipt_litres", value=41.4)
    await _set(hass, "number", "number.volvo_refuelling_receipt_total_price", value=72)
    await _press(hass, "button.volvo_enter_refuelling_receipt")
    (r,) = _current(tmp_path)
    assert r["anchor"] == at(300) and r["exact"] is True

    # The candidate it was entered from is offered no more, even before L1 is rebuilt.
    assert hass.states.get(select).attributes["options"] == ["manual", options[2]]
    assert hass.states.get(select).state == "manual"

    # The receipt asked the writer to derive: L1 carries it, and the select still agrees.
    await _settle(hass, vehicle_entry)
    late, early = sorted(l1.read(tmp_path, V, "refuelling"), key=lambda e: e["start"], reverse=True)
    assert late["receipt"] == r["id"] and late["confirmation"] == "receipt"
    assert early["confirmation"] == "unconfirmed"
    assert hass.states.get(select).attributes["options"] == ["manual", options[2]]
