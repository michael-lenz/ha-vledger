// SPDX-License-Identifier: BSD-3-Clause
// The Vehicle Ledger card (ADR-0039): one vehicle's last trips, and receipt
// entry for the energy kinds it has. A hand-written ES module on plain
// HTMLElement, without a build step (point 3). It reads through
// vledger.read_events and writes through the receipt actions, and decides
// nothing about L1 itself (ARC-02); what can be decided without a DOM is
// in model.js, which is tested on its own.

// model.js is imported with this module's query, the integration's version
// (point 2), so a release reloads both past the browser's cache.
const M = await import(new URL(`./model.js${new URL(import.meta.url).search}`, import.meta.url).href);

// The candidates a form offers, as the entity form offers them (ADR-0015).
const CANDIDATES = 10;

const STYLE = `
  ha-card { padding: 16px; }
  h2 { margin: 0 0 8px; font-size: 1.1em; font-weight: 500; }
  h3 { margin: 16px 0 8px; font-size: 1em; font-weight: 500; }
  .message { color: var(--secondary-text-color); margin: 4px 0; }
  .error { color: var(--error-color); }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 4px 4px 4px 0; vertical-align: top; }
  th {
    text-align: left; font-weight: 500; font-size: 0.9em; color: var(--secondary-text-color);
    border-bottom: 1px solid var(--divider-color, #ccc);
  }
  th.number, td.number { text-align: right; white-space: nowrap; }
  .flag { color: var(--secondary-text-color); font-size: 0.85em; margin-left: 4px; }
  form { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
  label { display: grid; gap: 2px; color: var(--secondary-text-color); font-size: 0.9em; }
  label.check { display: flex; align-items: center; gap: 8px; color: var(--primary-text-color); }
  input, select, button { font: inherit; color: var(--primary-text-color); }
  input:not([type=checkbox]), select {
    box-sizing: border-box; width: 100%; padding: 6px 8px;
    background: var(--card-background-color); border: 1px solid var(--divider-color, #ccc); border-radius: 4px;
  }
  button {
    justify-self: end; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer;
    background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
  }
  button[disabled] { opacity: 0.5; cursor: default; }
`;

// A DOM node from a tag, its properties and its children; text is always
// a text node, so nothing from L1 is ever parsed as markup.
function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined) {
      continue;
    }
    if (key.startsWith("on")) {
      node.addEventListener(key.slice(2), value);
    } else if (key === "class") {
      node.className = value;
    } else {
      node[key] = value;
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined) {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

class VledgerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._forms = {};      // per kind: what is typed, kept across redraws
    this._candidates = {}; // per kind: the waiting events, newest first
    this._trips = null;
    this._key = null;      // the event entities' last update, read for
    this._reads = 0;       // the latest read; an older one never lands after it
    this._layout = null;
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("vledger-card: a configuration is required");
    }
    this._config = config;
    this._key = null;
    this._layout = null;
    if (this._hass) {
      this._update();
    }
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    const shown = M.sections(this._config, this._kinds ?? M.RECEIPT_KINDS);
    return 1 + (shown.trips ? 2 + Math.min(M.tripCount(this._config), 10) : 0) + 6 * shown.kinds.length;
  }

  static getConfigElement() {
    return document.createElement("vledger-card-editor");
  }

  static getStubConfig(hass) {
    const trip = Object.values(hass.entities || {}).find(
      (e) => e.platform === M.DOMAIN && e.translation_key === M.ENTITY_KEYS.trip);
    return { device: trip?.device_id ?? "", trips: M.DEFAULT_TRIPS };
  }

  get _strings() {
    return M.stringsFor(this._hass?.locale?.language ?? this._hass?.language);
  }

  get _ctx() {
    return {
      language: this._hass?.locale?.language ?? this._hass?.language,
      timeZone: this._hass?.config?.time_zone,
      unitSystem: this._hass?.config?.unit_system,
      currency: this._hass?.config?.currency,
      strings: this._strings,
    };
  }

  // --- following the hass object -------------------------------------

  _update() {
    if (!this._config) {
      return;
    }
    const s = this._strings;
    const device = this._config.device;
    if (!device) {
      this._show(s.no_device);
      return;
    }
    const entities = M.vehicleEntities(this._hass.entities, device);
    if (!entities.trip) {
      this._show(s.not_a_vehicle);
      return;
    }
    this._entities = entities;
    this._entry = M.entryOf(this._hass.devices?.[device]);
    this._kinds = M.RECEIPT_KINDS.filter((kind) => entities[kind]);
    this._shown = M.sections(this._config, this._kinds);
    const layout = [device, this._ctx.language, this._shown.trips, ...this._shown.kinds].join("|");
    if (layout !== this._layout) {
      this._layout = layout;
      this._build();
    }
    if (this._shown.none) {
      return;
    }
    if (M.rebuilding(this._hass.states, entities)) {
      this._status(s.unavailable);
      return;
    }
    const key = M.refreshKey(this._hass.states, entities);
    if (key !== this._key) {
      this._key = key;
      this._read();
    }
  }

  async _read() {
    const read = ++this._reads;
    const call = (kind, last, waiting) =>
      this._hass.callWS(M.readEvents(this._entry, kind, last, waiting)).then((r) => r.response.events);
    const { trips: showTrips, kinds } = this._shown;
    try {
      const [trips, ...waiting] = await Promise.all([
        showTrips ? call("trip", M.tripCount(this._config), false) : null,
        ...kinds.map((kind) => call(kind, CANDIDATES, true)),
      ]);
      if (read !== this._reads) {
        return;
      }
      this._trips = trips;
      kinds.forEach((kind, i) => { this._candidates[kind] = waiting[i]; });
      this._status("");
      this._drawTrips();
      kinds.forEach((kind) => this._drawCandidates(kind));
    } catch (err) {
      if (read === this._reads) {
        this._key = null; // read again on the next update
        this._status(M.format(this._strings.read_failed, { reason: await this._message(err) }), true);
      }
    }
  }

  // --- drawing ----------------------------------------------------------

  _frame(...children) {
    this.shadowRoot.replaceChildren(
      h("style", { textContent: STYLE }),
      h("ha-card", {}, children));
  }

  _show(text) {
    this._layout = null;
    this._key = null;
    this._frame(h("p", { class: "message" }, text));
  }

  _build() {
    const s = this._strings;
    this._statusNode = h("p", { class: "message" });
    this._tripsNode = this._shown.trips ? h("div") : null;
    this._formNodes = {};
    this._eventNodes = {};
    const title = h("h2", {}, this._hass.devices?.[this._config.device]?.name_by_user
      ?? this._hass.devices?.[this._config.device]?.name ?? s.card_name);
    if (this._shown.none) {
      this._frame(title, h("p", { class: "message" }, s.nothing_shown));
      return;
    }
    const forms = this._shown.kinds.map((kind) => {
      this._forms[kind] ??= this._fresh(kind);
      const node = h("div");
      this._formNodes[kind] = node;
      this._drawForm(kind);
      return [h("h3", {}, s[`receipt_${kind}`]), node];
    });
    this._frame(
      title,
      this._statusNode,
      this._tripsNode ? [h("h3", {}, s.trips), this._tripsNode] : null,
      forms);
    this._drawTrips();
  }

  _status(text, error = false) {
    if (this._statusNode) {
      this._statusNode.textContent = text;
      this._statusNode.className = error ? "message error" : "message";
    }
  }

  _drawTrips() {
    if (!this._tripsNode || this._trips === null) {
      return;
    }
    const s = this._strings;
    if (!this._trips.length) {
      this._tripsNode.replaceChildren(h("p", { class: "message" }, s.no_trips));
      return;
    }
    const rows = this._trips.map((line) => {
      const r = M.tripRow(line, this._ctx);
      return h("tr", {},
        h("td", {}, r.when),
        h("td", {}, `${r.from} → ${r.to}`),
        h("td", { class: "number" }, r.distance, r.flag ? h("span", { class: "flag" }, r.flag) : null),
        h("td", { class: "number", title: r.costTitle }, r.cost,
          r.costFlag ? h("span", { class: "flag" }, r.costFlag) : null));
    });
    const head = h("tr", {},
      h("th", {}, s.column_when),
      h("th", {}, s.column_route),
      h("th", { class: "number" }, s.column_distance),
      h("th", { class: "number" }, s.column_cost));
    this._tripsNode.replaceChildren(h("table", {}, h("thead", {}, head), h("tbody", {}, rows)));
  }

  _fresh(kind) {
    return { ...M.emptyForm(kind), time: M.localInputNow(this._ctx.timeZone) };
  }

  _drawForm(kind) {
    const s = this._strings;
    const form = this._forms[kind];
    const set = (name, value) => { form[name] = value; };
    const event = h("select", { onchange: (e) => set("event", e.target.value) });
    const time = h("input", {
      type: "datetime-local", value: form.time, oninput: (e) => set("time", e.target.value),
    });
    const fields = M.FIELDS[kind].map((f) => {
      if (f.type === "boolean") {
        return h("label", { class: "check" },
          h("input", { type: "checkbox", checked: Boolean(form[f.name]),
            onchange: (e) => set(f.name, e.target.checked) }),
          s[f.label]);
      }
      return h("label", {}, f.unit ? `${s[f.label]} (${f.unit})` : s[f.label],
        h("input", {
          type: f.type === "number" ? "number" : "text",
          step: "any", min: f.type === "number" ? "0" : undefined,
          inputMode: f.type === "number" ? "decimal" : "text",
          value: form[f.name], oninput: (e) => set(f.name, e.target.value),
        }));
    });
    const message = h("p", { class: "message" });
    const button = h("button", { type: "submit" }, s[`enter_${kind}`]);
    const node = h("form", {
      onsubmit: (e) => { e.preventDefault(); this._submit(kind, button, message); },
    },
    h("label", {}, s.event, event),
    h("label", {}, s.time, time),
    fields, message, button);
    this._eventNodes = { ...this._eventNodes, [kind]: event };
    this._formNodes[kind].replaceChildren(node);
    this._drawCandidates(kind);
  }

  _drawCandidates(kind) {
    const select = this._eventNodes?.[kind];
    if (!select) {
      return;
    }
    const form = this._forms[kind];
    const options = (this._candidates[kind] || []).map((line) => M.candidateOption(kind, line, this._ctx));
    // A candidate gone from L1 (its receipt was entered) falls back to the
    // typed time, as the entity form's select does.
    if (!options.some((o) => o.value === form.event)) {
      form.event = M.MANUAL;
    }
    select.replaceChildren(
      h("option", { value: M.MANUAL, selected: form.event === M.MANUAL }, this._strings.event_manual),
      ...options.map((o) => h("option", { value: o.value, selected: form.event === o.value }, o.label)));
  }

  async _submit(kind, button, message) {
    const s = this._strings;
    const built = M.receiptPayload(kind, this._entry, this._forms[kind], s);
    message.className = "message error";
    if (built.missing) {
      message.textContent = M.format(s.missing, { fields: built.missing.join(", ") });
      return;
    }
    button.disabled = true;
    try {
      await this._hass.callWS({
        type: "call_service", domain: M.DOMAIN, service: built.service,
        service_data: built.data, return_response: true,
      });
      this._forms[kind] = this._fresh(kind);
      this._drawForm(kind);
      const done = this._formNodes[kind].querySelector("p.message");
      done.className = "message";
      done.textContent = s.entered;
    } catch (err) {
      message.textContent = await this._message(err); // the form keeps what was typed
    } finally {
      button.disabled = false;
    }
  }

  // An action's refusal in the instance's language, from the integration's
  // strings.json (ADR-0039, point 8); the server's text where there is none.
  async _message(err) {
    if (err?.translation_key && err?.translation_domain && this._hass.loadBackendTranslation) {
      try {
        const localize = await this._hass.loadBackendTranslation("exceptions", err.translation_domain);
        const text = localize(
          `component.${err.translation_domain}.exceptions.${err.translation_key}.message`,
          err.translation_placeholders || {});
        if (text) {
          return text;
        }
      } catch {
        // fall through to the server's text
      }
    }
    return err?.message || String(err);
  }
}

// The card's editor (ADR-0039, point 7; ADR-0041, point 5): the vehicle,
// the sections, and the number of trips while the list is shown, through
// Home Assistant's own selectors, so no YAML is needed.
class VledgerCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._draw();
  }

  set hass(hass) {
    this._hass = hass;
    this._draw();
  }

  _draw() {
    if (!this._hass || !this._config) {
      return;
    }
    const s = M.stringsFor(this._hass.locale?.language ?? this._hass.language);
    if (!this._device) {
      this._device = document.createElement("ha-selector");
      this._device.selector = { device: { filter: { integration: M.DOMAIN, model: "vehicle" } } };
      this._device.addEventListener("value-changed", (e) => this._changed("device", e.detail.value));
      this._trips = document.createElement("ha-selector");
      this._trips.selector = { number: { min: 1, max: M.MAX_EVENTS, mode: "box" } };
      this._trips.addEventListener("value-changed", (e) => this._changed("trips", e.detail.value));
      this._switches = Object.fromEntries(["trips", ...M.RECEIPT_KINDS].map((name) => {
        const node = document.createElement("ha-selector");
        node.selector = { boolean: {} };
        node.addEventListener("value-changed", (e) => this._changed(`show_${name}`, e.detail.value));
        return [name, node];
      }));
      const style = h("style", { textContent: "div { display: grid; gap: 16px; } [hidden] { display: none !important; }" });
      this.replaceChildren(style, h("div", {}, this._device, this._switches.trips, this._trips,
        M.RECEIPT_KINDS.map((kind) => this._switches[kind])));
    }
    const shown = M.sections(this._config, M.RECEIPT_KINDS);
    this._trips.hidden = !shown.trips;
    for (const [node, key, label, value] of [
      [this._device, "device", s.editor_device, this._config.device ?? ""],
      [this._trips, "trips", s.editor_trips, M.tripCount(this._config)],
      ...["trips", ...M.RECEIPT_KINDS].map((name) => [this._switches[name], `show_${name}`,
        s[`editor_show_${name}`], name === "trips" ? shown.trips : shown.kinds.includes(name)]),
    ]) {
      node.hass = this._hass;
      node.label = label;
      node.value = value;
      node.required = key === "device";
    }
  }

  _changed(key, value) {
    this._config = { ...this._config, [key]: value };
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config: this._config }, bubbles: true, composed: true,
    }));
  }
}

if (!customElements.get("vledger-card")) {
  customElements.define("vledger-card", VledgerCard);
  customElements.define("vledger-card-editor", VledgerCardEditor);
  const s = M.stringsFor(navigator.language);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "vledger-card",
    name: s.card_name,
    description: s.card_description,
    documentationURL: "https://github.com/michael-lenz/ha-vledger/blob/main/docs/user-guide.md",
  });
}
