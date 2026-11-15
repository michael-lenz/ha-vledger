# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0011: the user interface is available in English and German.

Derived from the requirement and the user guide's *In Home Assistant*: the
integration is added under *Settings → Devices & services*, a vehicle in four
steps and a charge point in one, changed under the entry's *Configure*, and it
brings entities such as *Capture status*. Everything of it a person reads in
Home Assistant — the flows' titles, menus, field labels and errors, the
entities' names, the actions, the repairs — Home Assistant serves the frontend
in the language the person chose, from what the integration provides.

The tests ask Home Assistant for those strings as the frontend does, in
English and in German, and set a vehicle up in an instance whose language is
German. They import nothing of the integration: Home Assistant loads it from
``custom_components/``.

The Lovelace card is not covered here: docs/developing.md has it checked by
hand, in English and in German, since no automated test draws it."""

import asyncio
import re

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

DOMAIN = "vledger"

pytestmark = [pytest.mark.req("REQ-0011")]

# Every category of strings the frontend asks Home Assistant for, for an
# integration (the frontend's translations command takes one at a time).
CATEGORIES = ("title", "config", "options", "entity", "services", "selector",
              "issues", "exceptions", "device_automation", "config_subentries")

PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


async def strings(hass, language, category):
    """What Home Assistant serves the frontend for the integration, in one
    language and one category, keyed as the frontend looks it up."""
    return await async_get_translations(hass, language, category, [DOMAIN])


@pytest.fixture
async def translated(hass: HomeAssistant, enable_custom_integrations):
    """The integration's strings in English and German, category by category."""
    out = {}
    for category in CATEGORIES:
        out[category] = (await strings(hass, "en", category),
                         await strings(hass, "de", category))
    assert any(en for en, _ in out.values()), "the integration serves no English strings"
    return out


def untranslated(key, de, en):
    """Home Assistant shows the English string wherever a language lacks
    one, so a German string that reads as the English one is either a word
    spelled alike in both languages ('Name') or English left untranslated.
    Of more than one word, it is the latter."""
    return de == en and len(str(en).split()) > 1


async def test_every_english_string_has_a_german_one(translated):
    """Whatever the English interface shows, the German one shows in German:
    no label, title, error or name falls back to English, and none is empty.
    The integration's title is its name, the same in every language."""
    for category, (en, de) in translated.items():
        empty = sorted(k for k, v in de.items() if not str(v).strip())
        assert not empty, f"{category}: empty German strings {empty}"
        if category == "title":
            continue
        same = sorted(f"{k}: {v!r}" for k, v in en.items() if untranslated(k, de.get(k), v))
        assert not same, f"{category}: English in the German interface:\n" + "\n".join(same)


async def test_german_strings_keep_what_is_filled_in(translated):
    """A placeholder the English string fills in — a name, a value, a
    count — the German one fills in too, so neither shows a raw brace."""
    for category, (en, de) in translated.items():
        for key, text in en.items():
            if key in de:
                assert (sorted(PLACEHOLDER.findall(str(de[key])))
                        == sorted(PLACEHOLDER.findall(str(text)))), (category, key, text, de[key])


class Flow:
    """A config or options flow as a person's browser drives it: each step is
    shown with its labels in the instance's language, filled in through the
    English label it corresponds to, and submitted."""

    def __init__(self, client, path, step, labels, english):
        self.client, self.path, self.step = client, path, step
        self.labels, self.english = labels, english

    def _key(self, *parts):
        return ".".join(("component", DOMAIN, self.kind, "step",
                         self.step["step_id"], *parts))

    @property
    def kind(self):
        return "options" if "options" in self.path else "config"

    def shown(self):
        """Every string the current step shows: its title, and its menu
        options or field labels, as (key, German, English)."""
        keys = [self._key("title")]
        if self.step["type"] == "menu":
            keys += [self._key("menu_options", o) for o in self.step["menu_options"]]
        elif self.step["type"] == "form":
            keys += [self._key("data", f["name"]) for f in self.step["data_schema"]]
        return [(k, self.labels.get(k), self.english.get(k)) for k in keys]

    def by_english(self, label):
        for k, _, en in self.shown():
            if en == label:
                return k.rsplit(".", 1)[1]
        raise AssertionError(f"{label!r} not shown on {self.step['step_id']}")

    async def submit(self, data):
        resp = await self.client.post(f"{self.path}/{self.step['flow_id']}", json=data)
        assert resp.status == 200, await resp.text()
        self.step = await resp.json()
        return self

    async def choose(self, label):
        return await self.submit({"next_step_id": self.by_english(label)})

    async def fill(self, values):
        data = {}
        for f in self.step["data_schema"]:
            default = f.get("description", {}).get("suggested_value", f.get("default"))
            if default is not None:
                data[f["name"]] = default
        for label, value in values.items():
            data[self.by_english(label)] = value
        return await self.submit(data)


def assert_german(flow, seen):
    """Every string of the current step is shown in German."""
    for key, de, en in flow.shown():
        assert de and not untranslated(key, de, en), f"{key}: {de!r} is not German"
        seen.append((key, de, en))


@pytest.fixture
async def german(hass: HomeAssistant, tmp_path, enable_custom_integrations, hass_client):
    """A Home Assistant whose language is German, with the configuration API
    and nothing configured for the ledger."""
    hass.config.config_dir = str(tmp_path)
    hass.config.language = "de"
    hass.config.latitude, hass.config.longitude = 52.52, 13.40
    assert await async_setup_component(hass, "config", {})
    # The frontend's own package (home-assistant-frontend) is not part of the
    # test stack; it is stood in for as set up, with nowhere to serve from.
    hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(lambda *_: None, [])
    hass.config.components.add("frontend")
    await hass.async_block_till_done()
    return hass, await hass_client()


async def start(hass, client, path, handler, kind):
    resp = await client.post(path, json={"handler": handler})
    assert resp.status == 200, await resp.text()
    return Flow(client, path, await resp.json(),
                await strings(hass, "de", kind), await strings(hass, "en", kind))


async def test_a_vehicle_is_set_up_in_german(german):
    """Add integration → Vehicle Ledger → a vehicle, in an instance set to
    German: every step's title, menu, field and error is labelled in German;
    the vehicle's entities are named in German."""
    hass, client = german
    hass.states.async_set("sensor.car_odometer", "12345",
                          {"unit_of_measurement": "km", "device_class": "distance",
                           "state_class": "total_increasing"})
    hass.states.async_set("sensor.car_soc", "80",
                          {"unit_of_measurement": "%", "device_class": "battery"})
    seen = []

    flow = await start(hass, client, "/api/config/config_entries/flow", DOMAIN, "config")
    assert_german(flow, seen)
    await flow.choose("A vehicle")
    assert_german(flow, seen)
    await flow.fill({"Name": "Familienauto"})
    assert_german(flow, seen)
    # No movement role: the form refuses, and says why in German.
    await flow.fill({"State of charge": "sensor.car_soc"})
    assert flow.step["type"] == "form" and flow.step["errors"], flow.step
    for error in flow.step["errors"].values():
        key = f"component.{DOMAIN}.config.error.{error}"
        assert flow.labels.get(key) and not untranslated(key, flow.labels[key], flow.english.get(key)), \
            f"{key}: no German error (English {flow.english.get(key)!r})"
        seen.append((key, flow.labels[key], flow.english.get(key)))
    await flow.fill({"Odometer": "sensor.car_odometer"})
    while flow.step["type"] == "form":  # the mapping, where it comes, and the parameters
        assert_german(flow, seen)
        await flow.fill({})
    assert flow.step["type"] == "create_entry", flow.step
    await hass.async_block_till_done()

    # What German shows is mostly not the English: the titles of the steps
    # walked through differ.
    titles = [(k, de, en) for k, de, en in seen if k.endswith(".title")]
    assert titles and all(de != en for _, de, en in titles), titles

    # The entities: each one's name is a German entity name of the
    # integration, not an English one.
    for _ in range(100):
        await hass.async_block_till_done()
        mine = [s for s in hass.states.async_all()
                if s.attributes.get("friendly_name", "").startswith("Familienauto")]
        if mine:
            break
        await asyncio.sleep(0.05)
    assert mine, "the vehicle has no entities"
    de_names = {v for k, v in (await strings(hass, "de", "entity")).items() if k.endswith(".name")}
    en_names = {v for k, v in (await strings(hass, "en", "entity")).items() if k.endswith(".name")}
    for state in mine:
        name = state.attributes["friendly_name"].removeprefix("Familienauto").strip()
        assert name in de_names, f"{state.entity_id}: {name!r} is not a German name"
        assert name not in en_names or len(name.split()) == 1, \
            f"{state.entity_id}: {name!r} is English"


async def test_the_options_are_shown_in_german(german):
    """The entry's Configure, in an instance set to German: the menu and
    every step reached from it are labelled in German."""
    hass, client = german
    hass.states.async_set("sensor.car_odometer", "12345",
                          {"unit_of_measurement": "km", "device_class": "distance",
                           "state_class": "total_increasing"})
    flow = await start(hass, client, "/api/config/config_entries/flow", DOMAIN, "config")
    await flow.choose("A vehicle")
    await flow.fill({"Name": "Familienauto"})
    await flow.fill({"Odometer": "sensor.car_odometer"})
    while flow.step["type"] == "form":
        await flow.fill({})
    entry_id = flow.step["result"]["entry_id"]
    await hass.async_block_till_done()

    seen = []
    menu = await start(hass, client, "/api/config/config_entries/options/flow", entry_id, "options")
    assert_german(menu, seen)
    for option in menu.step["menu_options"]:
        flow = await start(hass, client, "/api/config/config_entries/options/flow",
                           entry_id, "options")
        await flow.submit({"next_step_id": option})
        if flow.step["type"] in ("form", "menu"):
            assert_german(flow, seen)
    titles = [(k, de, en) for k, de, en in seen if k.endswith(".title")]
    assert titles and all(de != en for _, de, en in titles), titles
