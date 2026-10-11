// SPDX-License-Identifier: BSD-3-Clause
// The Vehicle Ledger card's model (ADR-0039, point 3): everything the card
// decides without a DOM, as pure functions — the action payloads built
// from a form, the unit conversion, the formatting and the string tables.
// Tested with Node's own runner (tests/frontend/); nothing here touches the
// browser, Home Assistant's frontend or L1. What a candidate is, and every
// value shown, is the library's (ARC-02): the card reads them through
// vledger.read_events and writes through the receipt actions.

export const DOMAIN = "vledger";

// The vehicle's event entities by translation key (ADR-0016). Which of
// them a device has says which kinds the card shows: a trip entity makes
// it a vehicle, the other two follow the receipt forms' gate.
export const ENTITY_KEYS = {
  trip: "last_trip",
  refuelling: "last_refuelling",
  charging: "last_charging_session",
  waiting: "unconfirmed_candidates",
};

export const RECEIPT_KINDS = ["refuelling", "charging"];

export const DEFAULT_TRIPS = 10;
export const MAX_EVENTS = 50; // what vledger.read_events answers at most

// The trips in the list, from the card's configuration.
export function tripCount(config) {
  const n = Number.parseInt(config?.trips ?? DEFAULT_TRIPS, 10);
  return Number.isFinite(n) ? Math.min(Math.max(n, 1), MAX_EVENTS) : DEFAULT_TRIPS;
}

// --- strings (ADR-0039, point 8; ADR-0001: English the source) ----------

export const STRINGS = {
  en: {
    card_name: "Vehicle Ledger",
    card_description: "A vehicle's last trips, and receipt entry for its refuellings and charging sessions.",
    editor_device: "Vehicle",
    editor_trips: "Trips in the list",
    no_device: "Choose a vehicle in the card's settings.",
    not_a_vehicle: "This device is not a Vehicle Ledger vehicle. Charge points have no trips or receipts.",
    unavailable: "The derivation is being rebuilt; the card follows when it is done.",
    trips: "Trips",
    no_trips: "No trips yet.",
    receipt_refuelling: "Refuelling receipt",
    receipt_charging: "Charging receipt",
    event: "Event",
    event_manual: "Enter time below",
    time: "Time",
    quantity_refuelling: "Litres",
    quantity_charging: "Energy",
    total_price: "Total price",
    unit_price: "Price per litre",
    full: "Full tank",
    place: "Place",
    note: "Note",
    enter_refuelling: "Enter refuelling receipt",
    enter_charging: "Enter charging receipt",
    entered: "Receipt entered.",
    missing: "Missing: {fields}.",
    read_failed: "Could not read the derivation: {reason}",
    quality_estimated: "estimated",
    quality_incomplete: "incomplete",
    quality_receipt: "receipt",
  },
  de: {
    card_name: "Vehicle Ledger",
    card_description: "Die letzten Fahrten eines Fahrzeugs und die Belegerfassung für seine Tank- und Ladevorgänge.",
    editor_device: "Fahrzeug",
    editor_trips: "Fahrten in der Liste",
    no_device: "Wähle in den Einstellungen der Karte ein Fahrzeug.",
    not_a_vehicle: "Dieses Gerät ist kein Fahrzeug von Vehicle Ledger. Ladepunkte haben keine Fahrten und keine Belege.",
    unavailable: "Die Ableitung wird neu berechnet; die Karte folgt, sobald es fertig ist.",
    trips: "Fahrten",
    no_trips: "Noch keine Fahrten.",
    receipt_refuelling: "Tankbeleg",
    receipt_charging: "Ladebeleg",
    event: "Ereignis",
    event_manual: "Zeit unten eingeben",
    time: "Zeit",
    quantity_refuelling: "Liter",
    quantity_charging: "Energie",
    total_price: "Gesamtpreis",
    unit_price: "Literpreis",
    full: "Volltankung",
    place: "Ort",
    note: "Notiz",
    enter_refuelling: "Tankbeleg erfassen",
    enter_charging: "Ladebeleg erfassen",
    entered: "Beleg erfasst.",
    missing: "Es fehlt: {fields}.",
    read_failed: "Die Ableitung ließ sich nicht lesen: {reason}",
    quality_estimated: "geschätzt",
    quality_incomplete: "unvollständig",
    quality_receipt: "Beleg",
  },
};

// The table for a language tag ("de", "de-CH"), English where there is none.
export function stringsFor(language) {
  const base = String(language || "en").toLowerCase().split("-")[0];
  return STRINGS[base] ?? STRINGS.en;
}

export function format(text, values = {}) {
  return text.replace(/\{(\w+)\}/g, (all, key) => (key in values ? String(values[key]) : all));
}

// --- units (FZG-09; ADR-0039, point 6) -----------------------------------

const KM_PER_MI = 1.609344;
const L_PER_GAL = 3.785411784; // the US gallon, which Home Assistant's US customary system uses

// A value in its L1 unit, shown in the instance's unit system: distance
// and volume convert, everything else is shown as L1 holds it.
export function toDisplay(value, unit, unitSystem) {
  if (typeof value !== "number") {
    return { value: null, unit };
  }
  if (unit === "km" && unitSystem?.length === "mi") {
    return { value: value / KM_PER_MI, unit: "mi" };
  }
  if (unit === "L" && unitSystem?.volume === "gal") {
    return { value: value / L_PER_GAL, unit: "gal" };
  }
  return { value, unit };
}

export function formatNumber(value, language, digits = 1) {
  if (typeof value !== "number") {
    return "—";
  }
  return new Intl.NumberFormat(language || "en", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

// --- time -------------------------------------------------------------------

// A moment's parts in the instance's time zone, on a 24-hour clock.
function localParts(moment, timeZone) {
  return Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: timeZone || "UTC",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(moment).map((p) => [p.type, p.value]),
  );
}

// A UTC time from L1, as the instance's local date and time, laid out as
// the entity form's candidate labels lay it out (receipt_desk._label), so
// the card and the form name a candidate alike.
export function formatTime(utc, timeZone) {
  const t = new Date(utc);
  if (Number.isNaN(t.getTime())) {
    return "—";
  }
  const p = localParts(t, timeZone);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

// Now, in the instance's time zone, as a datetime-local input holds it
// ("2026-10-11T14:30"). The actions read a time without an offset as the
// instance's local time (ADR-0015, point 1), so this is what the time
// field starts from and what it sends.
export function localInputNow(timeZone, now = new Date()) {
  const p = localParts(now, timeZone);
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

// --- what the list shows ----------------------------------------------------

// The flag beside a number that is not measured (ADR-0016, point 3), or null.
export function qualityFlag(quality, strings) {
  if (!quality || quality === "measured") {
    return null;
  }
  return strings[`quality_${quality}`] ?? quality;
}

// One trip line of vledger.read_events, as the list shows it.
export function tripRow(line, ctx) {
  const { language, timeZone, unitSystem, strings } = ctx;
  const d = toDisplay(line.distance_km, "km", unitSystem);
  return {
    start: line.start,
    when: formatTime(line.start, timeZone),
    from: line.start_zone || "—",
    to: line.end_zone || "—",
    distance: d.value === null ? "—" : `${formatNumber(d.value, language)} ${d.unit}`,
    flag: qualityFlag(line.distance_quality, strings),
  };
}

// The sensor's quantity a candidate's label shows, as the entity form's
// select shows it (receipt_desk._SENSOR_QUANTITY).
const SENSOR_QUANTITY = { refuelling: ["sensor_delta_l", "L"], charging: ["grid_kwh", "kWh"] };

// A waiting event of vledger.read_events, as the candidate select offers it.
export function candidateOption(kind, line, ctx) {
  const { language, timeZone, unitSystem } = ctx;
  const [key, unit] = SENSOR_QUANTITY[kind];
  const when = formatTime(line.start, timeZone);
  const q = toDisplay(line[key], unit, unitSystem);
  return {
    value: line.start,
    label: q.value === null ? when : `${when} · ${formatNumber(q.value, language)} ${q.unit}`,
  };
}

// --- what the form sends (ADR-0039, point 6; ADR-0015, point 1) ------------

export const MANUAL = "";

// The fields of each kind's form, in order, and which of them the action
// requires. The names are the action's fields.
export const FIELDS = {
  refuelling: [
    { name: "quantity_l", type: "number", label: "quantity_refuelling", unit: "L", required: true },
    { name: "total_price", type: "number", label: "total_price" },
    { name: "unit_price", type: "number", label: "unit_price" },
    { name: "full", type: "boolean", label: "full", initial: true },
    { name: "place", type: "text", label: "place" },
    { name: "note", type: "text", label: "note" },
  ],
  charging: [
    { name: "energy_kwh", type: "number", label: "quantity_charging", unit: "kWh", required: true },
    { name: "total_price", type: "number", label: "total_price", required: true },
    { name: "place", type: "text", label: "place" },
    { name: "note", type: "text", label: "note" },
  ],
};

export const SERVICES = { refuelling: "add_refuelling_receipt", charging: "add_charging_receipt" };

// A cleared form: the event back to the typed time, the full tank on.
export function emptyForm(kind) {
  const form = { event: MANUAL, time: "" };
  for (const f of FIELDS[kind]) {
    form[f.name] = f.initial ?? (f.type === "boolean" ? false : "");
  }
  return form;
}

function number(text) {
  const t = String(text ?? "").trim().replace(",", ".");
  if (t === "") {
    return null;
  }
  const n = Number(t);
  return Number.isFinite(n) ? n : Number.NaN;
}

// The action's data for what a form holds — or, when something it requires
// is empty, the labels of what is missing, and nothing is sent. Anything
// else the action checks itself, and its refusal is what the card shows.
export function receiptPayload(kind, entryId, form, strings) {
  const data = { config_entry_id: entryId };
  const missing = [];
  if (form.event === MANUAL) {
    if (!form.time) {
      missing.push(strings.time);
    } else {
      data.anchor = form.time;
    }
  } else {
    data.from_candidate = form.event;
  }
  for (const f of FIELDS[kind]) {
    const raw = form[f.name];
    if (f.type === "boolean") {
      data[f.name] = Boolean(raw);
      continue;
    }
    if (f.type === "number") {
      const n = number(raw);
      if (n === null || Number.isNaN(n)) {
        if (f.required || Number.isNaN(n)) {
          missing.push(strings[f.label]);
        }
        continue;
      }
      data[f.name] = n;
      continue;
    }
    const t = String(raw ?? "").trim();
    if (t !== "") {
      data[f.name] = t;
    } else if (f.required) {
      missing.push(strings[f.label]);
    }
  }
  return missing.length ? { missing } : { service: SERVICES[kind], data };
}

// The vledger.read_events call for one kind (ADR-0039, point 4).
export function readEvents(entryId, kind, last, waiting = false) {
  return {
    type: "call_service",
    domain: DOMAIN,
    service: "read_events",
    service_data: { config_entry_id: entryId, kind, last, waiting },
    return_response: true,
  };
}

// --- which vehicle, and when to read again (ADR-0039, points 5 and 7) ------

// The device's event entities, by kind, from the frontend's entity registry.
export function vehicleEntities(entities, deviceId) {
  const out = {};
  for (const e of Object.values(entities || {})) {
    if (e.device_id !== deviceId || e.platform !== DOMAIN) {
      continue;
    }
    for (const [kind, key] of Object.entries(ENTITY_KEYS)) {
      if (e.translation_key === key) {
        out[kind] = e.entity_id;
      }
    }
  }
  return out;
}

// The config entry the actions target, from the device.
export function entryOf(device) {
  return device?.primary_config_entry ?? device?.config_entries?.[0] ?? null;
}

// What changes after every writer run: the event entities' last update.
// The card reads again when this key changes, and not otherwise.
export function refreshKey(states, entityIds) {
  return Object.values(entityIds)
    .sort()
    .map((id) => `${id}@${states?.[id]?.last_updated ?? ""}`)
    .join("|");
}

// Whether the ledger is being rebuilt (ADR-0016, point 7): an event entity
// the device has is unavailable.
export function rebuilding(states, entityIds) {
  return Object.values(entityIds).some((id) => states?.[id]?.state === "unavailable");
}
