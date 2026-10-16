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
export const DEFAULT_ENERGY_EVENTS = 10;
export const MAX_EVENTS = 50; // what vledger.read_events answers at most

// A list's length from the card's configuration: at least one line, at
// most what the action answers, the default where it is not a number.
function listLength(value, fallback) {
  const n = Number.parseInt(value ?? fallback, 10);
  return Number.isFinite(n) ? Math.min(Math.max(n, 1), MAX_EVENTS) : fallback;
}

// The trips in the list, from the card's configuration.
export function tripCount(config) {
  return listLength(config?.trips, DEFAULT_TRIPS);
}

// The refuellings and charging sessions in their list (ADR-0058, point 4).
export function energyCount(config) {
  return listLength(config?.energy_events, DEFAULT_ENERGY_EVENTS);
}

// The sections the card draws (ADR-0041): each `show_*` option is true
// unless set to false, and can only take a section away — a receipt form
// stays limited to the kinds the vehicle has. The list of refuellings and
// charging sessions is off unless `show_energy` is true, so an existing
// configuration renders as before, and lists the kinds the vehicle has
// (ADR-0058, points 1 and 4). What is not drawn is not read.
export function sections(config, kinds) {
  const on = (name) => config?.[`show_${name}`] !== false;
  const shown = {
    trips: on("trips"),
    energy: config?.show_energy === true ? [...kinds] : [],
    kinds: kinds.filter((kind) => on(kind)),
  };
  shown.none = !shown.trips && !shown.energy.length && !shown.kinds.length;
  return shown;
}

// --- strings (ADR-0039, point 8; ADR-0001: English the source) ----------

export const STRINGS = {
  en: {
    card_name: "Vehicle Ledger",
    card_description: "A vehicle's last trips, refuellings and charging sessions, and receipt entry for them.",
    editor_device: "Vehicle",
    editor_trips: "Trips in the list",
    editor_energy_events: "Refuellings and charging sessions in the list",
    editor_show_trips: "Show the trip list",
    editor_show_energy: "Show the refuellings and charging sessions",
    editor_show_refuelling: "Show the refuelling receipt",
    editor_show_charging: "Show the charging receipt",
    nothing_shown: "The card's settings show no section for this vehicle.",
    no_device: "Choose a vehicle in the card's settings.",
    not_a_vehicle: "This device is not a Vehicle Ledger vehicle. Charge points have no trips or receipts.",
    unavailable: "The derivation is being rebuilt; the card follows when it is done.",
    trips: "Trips",
    no_trips: "No trips yet.",
    column_when: "Start",
    column_route: "Route",
    column_distance: "Distance",
    column_cost: "Cost",
    cost_fuel: "Fuel",
    cost_electricity: "Electricity",
    energy_refuelling: "Refuellings",
    energy_charging: "Charging sessions",
    energy_both: "Refuellings and charging sessions",
    no_energy: "None yet.",
    column_place: "Place",
    column_quantity: "Quantity",
    column_price: "Price",
    confirmation_unconfirmed: "unconfirmed",
    confirmation_ambiguous: "ambiguous",
    confirmation_chargepoint: "charge point",
    implausible: "implausible",
    deviation: "Deviation from the sensor: {pct} %",
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
    no_zone: "no zone",
  },
  de: {
    card_name: "Vehicle Ledger",
    card_description: "Die letzten Fahrten, Tank- und Ladevorgänge eines Fahrzeugs und die Belegerfassung dafür.",
    editor_device: "Fahrzeug",
    editor_trips: "Fahrten in der Liste",
    editor_energy_events: "Tank- und Ladevorgänge in der Liste",
    editor_show_trips: "Fahrtenliste anzeigen",
    editor_show_energy: "Tank- und Ladevorgänge anzeigen",
    editor_show_refuelling: "Tankbeleg anzeigen",
    editor_show_charging: "Ladebeleg anzeigen",
    nothing_shown: "Die Einstellungen der Karte zeigen für dieses Fahrzeug keinen Abschnitt.",
    no_device: "Wähle in den Einstellungen der Karte ein Fahrzeug.",
    not_a_vehicle: "Dieses Gerät ist kein Fahrzeug von Vehicle Ledger. Ladepunkte haben keine Fahrten und keine Belege.",
    unavailable: "Die Ableitung wird neu berechnet; die Karte folgt, sobald es fertig ist.",
    trips: "Fahrten",
    no_trips: "Noch keine Fahrten.",
    column_when: "Beginn",
    column_route: "Route",
    column_distance: "Strecke",
    column_cost: "Kosten",
    cost_fuel: "Kraftstoff",
    cost_electricity: "Strom",
    energy_refuelling: "Tankvorgänge",
    energy_charging: "Ladevorgänge",
    energy_both: "Tank- und Ladevorgänge",
    no_energy: "Noch keine.",
    column_place: "Ort",
    column_quantity: "Menge",
    column_price: "Preis",
    confirmation_unconfirmed: "unbestätigt",
    confirmation_ambiguous: "mehrdeutig",
    confirmation_chargepoint: "Ladepunkt",
    implausible: "unplausibel",
    deviation: "Abweichung vom Sensor: {pct} %",
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
    no_zone: "keine Zone",
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

// An amount in the instance's currency: a key spelled eur holds one, not
// the euro (ADR-0029). Without a currency, or with one Intl does not know,
// the number alone. `digits` fixes the decimals, for a unit price; without
// it the currency's own.
export function formatMoney(value, language, currency, digits) {
  if (typeof value !== "number") {
    return "—";
  }
  try {
    if (currency) {
      const fixed = digits === undefined ? {} : { minimumFractionDigits: digits, maximumFractionDigits: digits };
      return new Intl.NumberFormat(language || "en", { style: "currency", currency, ...fixed }).format(value);
    }
  } catch {
    // An unknown code: the number alone, below.
  }
  return formatNumber(value, language, digits ?? 2);
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

// A trip end's zone as the list shows it. Home Assistant's not_home is
// outside every zone, not away from home: the other zones are not home.
export function zoneLabel(zone, strings) {
  if (!zone) {
    return "—";
  }
  return zone === "not_home" ? strings.no_zone : zone;
}

// One trip line of vledger.read_events, as the list shows it.
export function tripRow(line, ctx) {
  const { language, timeZone, unitSystem, strings } = ctx;
  const d = toDisplay(line.distance_km, "km", unitSystem);
  return {
    start: line.start,
    when: formatTime(line.start, timeZone),
    from: zoneLabel(line.start_zone, strings),
    to: zoneLabel(line.end_zone, strings),
    distance: d.value === null ? "—" : `${formatNumber(d.value, language)} ${d.unit}`,
    flag: qualityFlag(line.distance_quality, strings),
    ...tripCost(line, ctx),
  };
}

// A trip's cost as the list shows it (ADR-0043, point 3): the sum, its
// flag while it has a value, and the split by energy as the cell's title —
// only the energies the vehicle uses, which carry a quality.
function tripCost(line, ctx) {
  const { language, currency, strings } = ctx;
  const known = typeof line.cost_eur === "number";
  const parts = [["fuel_cost_eur", "cost_fuel"], ["electricity_cost_eur", "cost_electricity"]]
    .filter(([key]) => line[`${key}_quality`] != null)
    .map(([key, label]) => `${strings[label]}: ${formatMoney(line[key], language, currency)}`);
  return {
    cost: formatMoney(line.cost_eur, language, currency),
    costFlag: known ? qualityFlag(line.cost_eur_quality, strings) : null,
    costTitle: parts.join(" · "),
  };
}

// --- the refuellings and charging sessions (ADR-0058) ----------------------

// The list's heading: the kinds the vehicle has.
export function energyHeading(kinds, strings) {
  return kinds.length === 1 ? strings[`energy_${kinds[0]}`] : strings.energy_both;
}

// The answers of vledger.read_events per kind, as one list: newest first,
// the newest `n` of them, each line with the kind it was read as — the
// action's lines do not carry it (ADR-0058, point 1).
export function energyEvents(answers, n) {
  return Object.entries(answers)
    .flatMap(([kind, lines]) => (lines || []).map((line) => ({ kind, line })))
    .sort((a, b) => Date.parse(b.line.start) - Date.parse(a.line.start))
    .slice(0, n);
}

// An event's confirmation beside its time, translated as L1 holds it; a
// receipt is left to the numbers' flags. Whether an event waits is the
// library's (l1.is_waiting), and nothing here decides it (ADR-0058, point 3).
export function confirmationFlag(confirmation, strings) {
  if (!confirmation || confirmation === "receipt") {
    return null;
  }
  return strings[`confirmation_${confirmation}`] ?? confirmation;
}

// Where an event was: the name the ledger gives the place — a refuelling's
// zone, a session's charge point (FAH-07) — and where it gives none, the
// receipt's place as typed (ADR-0058, point 2).
function energyPlace(kind, line, strings) {
  const named = kind === "refuelling"
    ? (line.zone && line.zone !== "not_home" ? line.zone : null)
    : line.chargepoint_name;
  const typed = typeof line.place === "string" ? line.place.trim() : "";
  if (named) {
    return named;
  }
  if (typed) {
    return typed;
  }
  return kind === "refuelling" ? zoneLabel(line.zone, strings) : "—";
}

// A refuelling's price per litre, or per gallon where the list shows gallons.
function unitPrice(value, ctx) {
  const { language, unitSystem, currency } = ctx;
  if (typeof value !== "number") {
    return "";
  }
  const gal = unitSystem?.volume === "gal";
  return `${formatMoney(gal ? value * L_PER_GAL : value, language, currency, 3)}/${gal ? "gal" : "L"}`;
}

// One refuelling or charging session of vledger.read_events, as the list
// shows it (ADR-0058, points 2 and 3): every figure an L1 key with L1's
// quality beside it. A refuelling's litres are its receipt's where one
// carries them, else the sensor's (BEL-07); a session's energy and cost
// already are the receipt's where it has one.
export function energyRow(kind, line, ctx) {
  const { language, timeZone, unitSystem, currency, strings } = ctx;
  const fuel = kind === "refuelling";
  const [amount, amountQuality] = !fuel ? [line.grid_kwh, line.grid_kwh_quality]
    : typeof line.quantity_l === "number" ? [line.quantity_l, line.quantity_quality]
      : [line.sensor_delta_l, line.sensor_delta_quality];
  const [price, priceQuality] = fuel ? [line.price, line.price_quality] : [line.cost_eur, line.cost_quality];
  const q = toDisplay(amount, fuel ? "L" : "kWh", unitSystem);
  const implausible = line.implausible === true;
  const quantityFlags = [
    q.value === null ? null : qualityFlag(amountQuality, strings),
    implausible ? strings.implausible : null,
  ].filter(Boolean);
  return {
    kind,
    start: line.start,
    when: formatTime(line.start, timeZone),
    status: confirmationFlag(line.confirmation, strings),
    place: energyPlace(kind, line, strings),
    quantity: q.value === null ? "—" : `${formatNumber(q.value, language)} ${q.unit}`,
    quantityFlag: quantityFlags.length ? quantityFlags.join(" · ") : null,
    quantityTitle: implausible && typeof line.deviation_pct === "number"
      ? format(strings.deviation, { pct: formatNumber(line.deviation_pct, language) }) : "",
    price: formatMoney(price, language, currency),
    priceFlag: typeof price === "number" ? qualityFlag(priceQuality, strings) : null,
    priceTitle: fuel ? unitPrice(line.unit_price, ctx) : "",
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
