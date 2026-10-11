// SPDX-License-Identifier: BSD-3-Clause
// The card's model (ADR-0039, point 9), with Node's own runner and no
// packages: node --test tests/frontend/
import assert from "node:assert/strict";
import { test } from "node:test";

import * as M from "../../custom_components/vledger/frontend/model.js";

const en = M.STRINGS.en;
const metric = { length: "km", volume: "L" };
const us = { length: "mi", volume: "gal" };
const ctx = { language: "en", timeZone: "Europe/Berlin", unitSystem: metric, strings: en };

test("the German table has exactly the English keys, and every label has a string", () => {
  assert.deepEqual(Object.keys(M.STRINGS.de).sort(), Object.keys(en).sort());
  for (const table of Object.values(M.STRINGS)) {
    for (const [key, text] of Object.entries(table)) {
      assert.ok(text.trim(), key);
    }
  }
  for (const kind of M.RECEIPT_KINDS) {
    for (const f of M.FIELDS[kind]) {
      assert.ok(en[f.label], f.label);
    }
    for (const key of [`receipt_${kind}`, `enter_${kind}`]) {
      assert.ok(en[key], key);
    }
  }
});

test("a language tag picks its table, English where there is none", () => {
  assert.equal(M.stringsFor("de-CH"), M.STRINGS.de);
  assert.equal(M.stringsFor("DE"), M.STRINGS.de);
  assert.equal(M.stringsFor("fr"), M.STRINGS.en);
  assert.equal(M.stringsFor(undefined), M.STRINGS.en);
  assert.equal(M.format(en.missing, { fields: "Litres, Time" }), "Missing: Litres, Time.");
  assert.equal(M.format("{a} {b}", { a: 1 }), "1 {b}");
});

test("distance and volume follow the unit system, everything else stays as L1 holds it", () => {
  assert.deepEqual(M.toDisplay(16.09344, "km", metric), { value: 16.09344, unit: "km" });
  const mi = M.toDisplay(16.09344, "km", us);
  assert.equal(mi.unit, "mi");
  assert.ok(Math.abs(mi.value - 10) < 1e-9);
  const gal = M.toDisplay(37.85411784, "L", us);
  assert.equal(gal.unit, "gal");
  assert.ok(Math.abs(gal.value - 10) < 1e-9);
  assert.deepEqual(M.toDisplay(11, "kWh", us), { value: 11, unit: "kWh" });
  assert.deepEqual(M.toDisplay(null, "km", us), { value: null, unit: "km" });
});

test("a trip row: local start time, zones, distance, and a flag only when not measured", () => {
  const line = {
    start: "2026-10-09T10:00:00Z", start_zone: "Home", end_zone: null,
    distance_km: 12.345, distance_quality: "measured",
  };
  assert.deepEqual(M.tripRow(line, ctx), {
    start: line.start, when: "2026-10-09 12:00", from: "Home", to: "—", distance: "12.3 km", flag: null,
  });
  const estimated = M.tripRow({ ...line, distance_quality: "estimated" }, { ...ctx, unitSystem: us });
  assert.equal(estimated.flag, "estimated");
  assert.equal(estimated.distance, "7.7 mi");
  const de = M.tripRow({ ...line, distance_km: null }, { ...ctx, language: "de", strings: M.STRINGS.de });
  assert.equal(de.when, "2026-10-09 12:00");
  assert.equal(de.distance, "—");
  assert.equal(M.qualityFlag("incomplete", M.STRINGS.de), "unvollständig");
  assert.equal(M.qualityFlag("something new", en), "something new");
});

test("outside every zone is no zone, not away from home", () => {
  const line = { start: "2026-10-09T10:00:00Z", start_zone: "not_home", end_zone: "Z9", distance_km: 1 };
  const row = M.tripRow(line, ctx);
  assert.equal(row.from, "no zone");
  assert.equal(row.to, "Z9");
  assert.equal(M.tripRow(line, { ...ctx, language: "de", strings: M.STRINGS.de }).from, "keine Zone");
});

test("a candidate is offered by its start, labelled as the entity form labels it", () => {
  const line = { start: "2026-10-09T10:00:00Z", sensor_delta_l: 41.37, grid_kwh: 11 };
  assert.deepEqual(M.candidateOption("refuelling", line, ctx),
    { value: line.start, label: "2026-10-09 12:00 · 41.4 L" });
  assert.deepEqual(M.candidateOption("charging", line, ctx),
    { value: line.start, label: "2026-10-09 12:00 · 11.0 kWh" });
  assert.equal(M.candidateOption("refuelling", { start: line.start }, ctx).label, "2026-10-09 12:00");
});

test("the time field starts at now in the instance's zone, as the actions read a time", () => {
  const now = new Date("2026-10-09T22:30:00Z");
  assert.equal(M.localInputNow("Europe/Berlin", now), "2026-10-10T00:30");
  assert.equal(M.localInputNow("America/New_York", now), "2026-10-09T18:30");
});

test("a refuelling form becomes the action's data, typed time or candidate", () => {
  const form = { ...M.emptyForm("refuelling"), time: "2026-10-09T12:00", quantity_l: "41,37",
    total_price: "72.36", place: "  A9 " };
  assert.deepEqual(M.receiptPayload("refuelling", "entry1", form, en), {
    service: "add_refuelling_receipt",
    data: { config_entry_id: "entry1", anchor: "2026-10-09T12:00", quantity_l: 41.37, total_price: 72.36,
      full: true, place: "A9" },
  });
  const fromCandidate = M.receiptPayload("refuelling", "entry1",
    { ...form, event: "2026-10-09T10:00:00Z", full: false }, en);
  assert.equal(fromCandidate.data.from_candidate, "2026-10-09T10:00:00Z");
  assert.equal(fromCandidate.data.full, false);
  assert.ok(!("anchor" in fromCandidate.data));
});

test("what a form lacks is named and nothing is sent; the rest is the action's to check", () => {
  assert.deepEqual(M.receiptPayload("refuelling", "e", M.emptyForm("refuelling"), en),
    { missing: ["Time", "Litres"] });
  assert.deepEqual(M.receiptPayload("charging", "e", { ...M.emptyForm("charging"), time: "2026-10-09T12:00" }, en),
    { missing: ["Energy", "Total price"] });
  assert.deepEqual(M.receiptPayload("refuelling", "e",
    { ...M.emptyForm("refuelling"), time: "2026-10-09T12:00", quantity_l: "40", unit_price: "abc" }, en),
  { missing: ["Price per litre"] });
  const charging = M.receiptPayload("charging", "e",
    { ...M.emptyForm("charging"), time: "2026-10-09T12:00", energy_kwh: "11", total_price: "6.5" }, en);
  assert.deepEqual(charging, { service: "add_charging_receipt",
    data: { config_entry_id: "e", anchor: "2026-10-09T12:00", energy_kwh: 11, total_price: 6.5 } });
});

test("the read action's call", () => {
  assert.deepEqual(M.readEvents("e", "trip", 10), {
    type: "call_service", domain: "vledger", service: "read_events",
    service_data: { config_entry_id: "e", kind: "trip", last: 10, waiting: false }, return_response: true,
  });
  assert.equal(M.tripCount({}), M.DEFAULT_TRIPS);
  assert.equal(M.tripCount({ trips: 500 }), M.MAX_EVENTS);
  assert.equal(M.tripCount({ trips: 0 }), 1);
  assert.equal(M.tripCount({ trips: "x" }), M.DEFAULT_TRIPS);
});

test("the sections: every option on unless set false, and none can add a kind the vehicle lacks", () => {
  const both = ["refuelling", "charging"];
  assert.deepEqual(M.sections({}, both), { trips: true, kinds: both, none: false });
  assert.deepEqual(M.sections(undefined, both), { trips: true, kinds: both, none: false });
  assert.deepEqual(M.sections({ show_trips: false, show_charging: false }, both),
    { trips: false, kinds: ["refuelling"], none: false });
  assert.deepEqual(M.sections({ show_charging: true }, ["refuelling"]),
    { trips: true, kinds: ["refuelling"], none: false });
  assert.deepEqual(M.sections({ show_trips: false, show_refuelling: false }, ["refuelling"]),
    { trips: false, kinds: [], none: true });
  assert.deepEqual(M.sections({ show_trips: false }, []), { trips: false, kinds: [], none: true });
});

test("the vehicle's entities, its entry, and when to read again", () => {
  const entities = {
    "sensor.v_trip": { entity_id: "sensor.v_trip", device_id: "d1", platform: "vledger", translation_key: "last_trip" },
    "sensor.v_fuel": { entity_id: "sensor.v_fuel", device_id: "d1", platform: "vledger", translation_key: "last_refuelling" },
    "sensor.v_wait": { entity_id: "sensor.v_wait", device_id: "d1", platform: "vledger", translation_key: "unconfirmed_candidates" },
    "button.v_enter": { entity_id: "button.v_enter", device_id: "d1", platform: "vledger", translation_key: "refuelling_receipt_enter" },
    "sensor.other": { entity_id: "sensor.other", device_id: "d2", platform: "vledger", translation_key: "last_trip" },
    "sensor.alien": { entity_id: "sensor.alien", device_id: "d1", platform: "other", translation_key: "last_charging_session" },
  };
  const ids = M.vehicleEntities(entities, "d1");
  assert.deepEqual(ids, { trip: "sensor.v_trip", refuelling: "sensor.v_fuel", waiting: "sensor.v_wait" });
  assert.deepEqual(M.vehicleEntities(entities, "d3"), {});

  assert.equal(M.entryOf({ primary_config_entry: "a", config_entries: ["b", "a"] }), "a");
  assert.equal(M.entryOf({ config_entries: ["b"] }), "b");
  assert.equal(M.entryOf(undefined), null);

  const states = {
    "sensor.v_trip": { state: "12.3", last_updated: "t1" },
    "sensor.v_fuel": { state: "40", last_updated: "t1" },
    "sensor.v_wait": { state: "1", last_updated: "t1" },
  };
  const key = M.refreshKey(states, ids);
  assert.equal(M.refreshKey(structuredClone(states), ids), key);
  const later = { ...states, "sensor.v_wait": { state: "0", last_updated: "t2" } };
  assert.notEqual(M.refreshKey(later, ids), key);
  assert.equal(M.rebuilding(states, ids), false);
  assert.equal(M.rebuilding({ ...states, "sensor.v_trip": { state: "unavailable" } }, ids), true);
});
