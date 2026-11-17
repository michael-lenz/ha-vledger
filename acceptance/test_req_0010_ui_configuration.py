# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0010: configuration is possible entirely through the user interface
(config flow and options flow); YAML is not required.

Derived from the requirement, README.md ("Everything is configured in the UI;
no YAML", *Installing*) and the user guide's *In Home Assistant*: a vehicle in
four steps (name, roles, mapping only for an enumerated role, parameters), a
charge point in one step, and the entry's *Configure* as a menu over the same
steps plus thresholds, notifications, storage and, for a charge point, a new
tariff.

Home Assistant starts with no configuration of the integration at all, and the
tests do what the frontend does: they call the config and options flow
endpoints over HTTP and read the field labels the frontend shows from the
translations Home Assistant serves it, choosing every field by the label the user guide
names, never by an internal key."""

import asyncio
import pathlib

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

DOMAIN = "vledger"

pytestmark = [pytest.mark.req("REQ-0010")]


@pytest.fixture
async def ui(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client):
    """A Home Assistant with its frontend's API and nothing configured for the
    ledger: no YAML of it, no entry. The configuration directory is a
    temporary one, so the raw log lands there."""
    hass.config.config_dir = str(tmp_path)
    hass.config.latitude, hass.config.longitude = 52.52, 13.40
    assert await async_setup_component(hass, "config", {})
    # The frontend's own package (home-assistant-frontend) is not part of the
    # test stack; it is stood in for as set up, with nowhere to serve from.
    hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(lambda *_: None, [])
    hass.config.components.add("frontend")
    await hass.async_block_till_done()
    assert DOMAIN not in hass.config.as_dict().get("components", [])
    # Home Assistant's own server keeps its router open after it starts, since
    # an integration set up later in the UI adds paths to it (the http
    # component's start). The test client starts the server here, before any
    # entry exists, and which test client a test stack provides is not fixed:
    # the router is kept open as Home Assistant keeps it.
    hass.http.app.router.freeze = lambda: None
    return UI(hass, await hass_client())


class UI:
    """What a person's browser does: start a flow, read the form it is shown
    with its labels, fill it in by label, submit."""

    def __init__(self, hass, client):
        self.hass, self.client = hass, client

    async def labels(self, category):
        """The English labels the frontend shows, as Home Assistant serves
        them to it (the frontend's translations command answers with these)."""
        return await async_get_translations(self.hass, "en", category, [DOMAIN])

    async def start(self, path, handler):
        resp = await self.client.post(path, json={"handler": handler})
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def submit(self, path, flow_id, data):
        resp = await self.client.post(f"{path}/{flow_id}", json=data)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def config_flow(self):
        return Flow(self, "/api/config/config_entries/flow", "config",
                    await self.start("/api/config/config_entries/flow", DOMAIN),
                    await self.labels("config"))

    async def options_flow(self, entry_id):
        return Flow(self, "/api/config/config_entries/options/flow", "options",
                    await self.start("/api/config/config_entries/options/flow", entry_id),
                    await self.labels("options"))

    async def entries(self):
        resp = await self.client.get(f"/api/config/config_entries/entry?domain={DOMAIN}")
        assert resp.status == 200, await resp.text()
        return await resp.json()


class Flow:
    def __init__(self, ui, path, category, step, labels):
        self.ui, self.path, self.category, self.step, self.labels = ui, path, category, step, labels

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, self.category, "step",
                                         self.step["step_id"], *parts)))

    @property
    def title(self):
        return self._label("title")

    def options(self):
        """A menu's options, by the label shown."""
        assert self.step["type"] == "menu", self.step
        return {self._label("menu_options", o): o for o in self.step["menu_options"]}

    def fields(self):
        """A form's fields, by the label shown."""
        assert self.step["type"] == "form", self.step
        return {self._label("data", f["name"]): f for f in self.step["data_schema"]}

    def shown(self, label):
        """What a field shows when the form opens: its suggested or default value."""
        field = self.fields()[label]
        return field.get("description", {}).get("suggested_value", field.get("default"))

    async def choose(self, label):
        options = self.options()
        assert label in options, f"{label!r} not among {sorted(options, key=str)}"
        self.step = await self.ui.submit(self.path, self.step["flow_id"],
                                         {"next_step_id": options[label]})
        return self

    async def fill(self, values):
        """Fill in by label and submit. As in the browser, a field left
        alone is sent with what the form showed in it, and an empty one is
        not sent."""
        fields = self.fields()
        data = {}
        for label, field in fields.items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        missing = set(values) - set(fields)
        assert not missing, f"{sorted(missing)} not among {sorted(fields, key=str)}"
        self.step = await self.ui.submit(self.path, self.step["flow_id"], data)
        return self


def odometer(hass, entity="sensor.car_odometer"):
    hass.states.async_set(entity, "12345", {"unit_of_measurement": "km",
                                            "device_class": "distance",
                                            "state_class": "total_increasing"})
    return entity


async def add_vehicle(ui, name="Family car", roles=None, mapping=None, parameters=None):
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    assert flow.title == "Vehicle"
    await flow.fill({"Name": name})
    assert flow.title == "Roles"
    await flow.fill(roles or {"Odometer": odometer(ui.hass)})
    if mapping is not None:
        assert flow.title == "Mapping"
        await flow.fill(mapping)
    assert flow.title == "Parameters"
    await flow.fill(parameters or {})
    return flow.step


async def test_a_vehicle_is_set_up_in_the_ui_alone(ui):
    """Add integration → Vehicle Ledger → A vehicle: name, roles, parameters,
    and the entry runs: its raw log is written and Capture status shows it."""
    result = await add_vehicle(ui)
    assert result["type"] == "create_entry", result
    await ui.hass.async_block_till_done()

    entries = await ui.entries()
    assert [(e["title"], e["state"]) for e in entries] == [("Family car", "loaded")]

    # A first L1 is built at startup, shown as recomputing while it runs.
    for _ in range(100):
        status = [s.state for s in ui.hass.states.async_all("sensor")
                  if s.attributes.get("friendly_name", "").endswith("Capture status")]
        if status != ["recomputing"]:
            break
        await asyncio.sleep(0.05)
        await ui.hass.async_block_till_done()
    assert status == ["running"]
    log = pathlib.Path(ui.hass.config.path("vledger"))
    assert any(p.is_file() for p in log.rglob("*")), "no raw log under <config>/vledger/"


async def test_the_roles_step_asks_for_a_movement_role_in_the_ui(ui):
    """Every role is optional, but at least one movement role is required:
    the form says so instead of creating an entry."""
    flow = await ui.config_flow()
    await flow.choose("A vehicle")
    await flow.fill({"Name": "No movement"})
    ui.hass.states.async_set("sensor.car_soc", "80", {"unit_of_measurement": "%",
                                                       "device_class": "battery"})
    await flow.fill({"State of charge": "sensor.car_soc"})
    assert flow.step["type"] == "form" and flow.title == "Roles"
    assert flow.step["errors"], flow.step
    await flow.fill({"Odometer": odometer(ui.hass)})
    assert flow.title in ("Mapping", "Parameters")


async def test_an_enumerated_role_is_mapped_in_the_ui(ui):
    """A charging state reported as an enumeration brings the Mapping step,
    where the source's values that mean charging are ticked."""
    ui.hass.states.async_set("sensor.car_charging", "idle",
                             {"device_class": "enum",
                              "options": ["charging", "idle", "complete"]})
    result = await add_vehicle(
        ui, name="Hybrid",
        roles={"Odometer": odometer(ui.hass), "Charging state": "sensor.car_charging"},
        mapping={"Values that mean charging": ["charging"]},
        parameters={"Net battery capacity": 10.5})
    assert result["type"] == "create_entry", result
    await ui.hass.async_block_till_done()
    assert [e["state"] for e in await ui.entries()] == ["loaded"]


async def test_a_charge_point_is_set_up_in_the_ui_alone(ui):
    """A charge point in one step: name, location and radius on the map, an
    optional energy meter and charging power, the first tariff and its date."""
    flow = await ui.config_flow()
    await flow.choose("A charge point")
    assert flow.title == "Charge point"
    await flow.fill({"Name": "Home wallbox",
                     "Location and radius": {"latitude": 52.52, "longitude": 13.40,
                                             "radius": 50},
                     "Tariff": 0.32,
                     "Tariff valid from": "2026-01-01"})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()
    assert [(e["title"], e["state"]) for e in await ui.entries()] == [("Home wallbox", "loaded")]


async def test_a_vehicle_is_changed_in_the_options_alone(ui):
    """The entry's Configure: a menu over the same steps plus the thresholds;
    a saved value is what the form shows the next time, and the entry runs."""
    await add_vehicle(ui)
    await ui.hass.async_block_till_done()
    [entry] = await ui.entries()

    flow = await ui.options_flow(entry["entry_id"])
    assert flow.title == "What to change"
    shown = set(flow.options())
    assert {"Roles", "Parameters", "Thresholds and time constants",
            "Notifications", "Storage"} <= shown

    await flow.choose("Thresholds and time constants")
    before = flow.shown("Standstill (T_still)")
    assert before is not None
    changed = before * 2
    await flow.fill({"Standstill (T_still)": changed})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Thresholds and time constants")
    assert flow.shown("Standstill (T_still)") == changed
    assert [e["state"] for e in await ui.entries()] == ["loaded"]


async def test_a_role_is_assigned_in_the_options_alone(ui):
    """Roles are reassigned under Configure → Roles, pre-filled with the
    current assignment."""
    await add_vehicle(ui)
    await ui.hass.async_block_till_done()
    [entry] = await ui.entries()

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Roles")
    assert flow.shown("Odometer") == "sensor.car_odometer"
    ui.hass.states.async_set("sensor.car_temp", "12.5",
                             {"unit_of_measurement": "°C", "device_class": "temperature"})
    await flow.fill({"Odometer": "sensor.car_odometer",
                     "Outside temperature": "sensor.car_temp"})
    while flow.step["type"] == "form":  # a step that follows, such as the mapping
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()

    flow = await ui.options_flow(entry["entry_id"])
    await flow.choose("Roles")
    assert flow.shown("Outside temperature") == "sensor.car_temp"


async def test_a_tariff_is_added_in_the_options_alone(ui):
    """For a charge point, Configure offers the meter, the charging power and
    adding a tariff from a date."""
    flow = await ui.config_flow()
    await flow.choose("A charge point")
    await flow.fill({"Name": "Home wallbox",
                     "Location and radius": {"latitude": 52.52, "longitude": 13.40,
                                             "radius": 50},
                     "Tariff": 0.32, "Tariff valid from": "2026-01-01"})
    await ui.hass.async_block_till_done()
    [entry] = await ui.entries()

    flow = await ui.options_flow(entry["entry_id"])
    assert {"Energy meter", "Charging power", "Add a tariff from a date"} <= set(flow.options())
    await flow.choose("Add a tariff from a date")
    await flow.fill({"Tariff": 0.29, "Valid from": "2026-07-01"})
    assert flow.step["type"] == "create_entry", flow.step
    await ui.hass.async_block_till_done()
    assert [e["state"] for e in await ui.entries()] == ["loaded"]
