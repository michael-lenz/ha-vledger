# SPDX-License-Identifier: BSD-3-Clause
"""What the browser does, shared by the acceptance tests that meet the
integration in Home Assistant: a Home Assistant with its frontend's API and
nothing of the ledger configured (``open_frontend``), and the setup and
options screens driven by the labels a person reads on them (``Browser``,
``Flow``).

A module imports it only after ``pytest.importorskip`` has found the test
stack of the ``ha`` extra, since it imports Home Assistant. It holds no
requirement's test and nothing of ``vledger`` or of the integration: what a
module needs beyond it, it keeps itself, a ``Browser`` or ``Flow`` of its own
subclassing these where it adds steps."""

from homeassistant.components import frontend
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

DOMAIN = "vledger"
FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"
ENTRIES = "/api/config/config_entries/entry"


async def open_frontend(hass, config_dir, hass_client, *components):
    """Set Home Assistant up as an instance with its frontend, nothing of the
    ledger configured, and start the HTTP server the browser talks to; return
    the browser's client.

    The configuration directory is ``config_dir``, so the raw log lands under
    it, in ``<config>/vledger/``. ``components`` are set up besides the
    configuration API (``"diagnostics"``, say). What a module sets on
    ``hass.config`` before the call — a language, a media folder — stays."""
    hass.config.config_dir = str(config_dir)
    hass.config.latitude, hass.config.longitude = 52.52, 13.40
    for component in ("config", *components):
        assert await async_setup_component(hass, component, {})
    # The frontend's own package (home-assistant-frontend) is not part of the
    # test stack; it is stood in for as set up, with nowhere to serve from.
    hass.data[frontend.DATA_EXTRA_MODULE_URL] = frontend.UrlManager(lambda *_: None, [])
    hass.config.components.add("frontend")
    await hass.async_block_till_done()
    # Home Assistant's own server keeps its router open after it starts, since
    # an integration set up later in the UI adds paths to it (the http
    # component's start). The test client starts the server here, before any
    # entry exists, and which test client starts it depends on the order
    # pytest loads its plugins in: pytest-aiohttp's freezes the router, the
    # Home Assistant plugin's does not (ISSUE-0122). The router is kept open
    # as Home Assistant keeps it.
    hass.http.app.router.freeze = lambda: None
    return await hass_client()


class Browser:
    """The frontend's calls to the configuration API, each answered 200."""

    def __init__(self, hass, client):
        self.hass, self.client = hass, client

    async def post(self, path, data=None):
        resp = await self.client.post(path, json=data)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def get(self, path):
        resp = await self.client.get(path)
        assert resp.status == 200, await resp.text()
        return await resp.json()

    async def entries(self):
        """The ledger's config entries, as Settings → Devices & services
        lists them."""
        return await self.get(f"{ENTRIES}?domain={DOMAIN}")

    async def flow(self, path, category, handler):
        """Start a flow and show its first step with its English labels."""
        labels = await async_get_translations(self.hass, "en", category, [DOMAIN])
        return Flow(self, path, category, await self.post(path, {"handler": handler}), labels)

    async def config_flow(self):
        """Add integration → Vehicle Ledger."""
        return await self.flow(FLOW, "config", DOMAIN)

    async def options_flow(self, entry_id):
        """Configure on an entry."""
        return await self.flow(OPTIONS, "options", entry_id)


class Flow:
    """One flow as the browser shows it: a menu or a form, by its labels."""

    def __init__(self, browser, path, category, step, labels):
        self.ui, self.path, self.category = browser, path, category
        self.step, self.labels = step, labels

    def _label(self, *parts):
        return self.labels.get(".".join(("component", DOMAIN, self.category, "step",
                                         self.step["step_id"], *parts)))

    @property
    def title(self):
        return self._label("title")

    def fields(self):
        """The form's fields by their labels."""
        assert self.step["type"] == "form", self.step
        return {self._label("data", f["name"]): f for f in self.step["data_schema"]}

    def field(self, name):
        """A field's label by the name the user guide gives it, whatever its
        case: the label that reads so, or else the one label that reads so and
        adds a parenthesis ("Position (device tracker)")."""
        labels = [lab for lab in self.fields() if lab]
        found = [lab for lab in labels if lab == name]
        found = found or [lab for lab in labels if lab.casefold() == name.casefold()]
        found = found or [lab for lab in labels
                          if lab.casefold().startswith(name.casefold() + " (")]
        assert len(found) == 1, f"{name!r} not among {sorted(self.fields(), key=str)}"
        return found[0]

    def shown(self, label):
        """What the form shows in a field before anybody touches it."""
        field = self._field(label)
        return field.get("description", {}).get("suggested_value", field.get("default"))

    def _field(self, label):
        fields = self.fields()
        return fields[label] if label in fields else fields[self.field(label)]

    def offered(self, label):
        """The values a field offers to choose from."""
        [(_, config)] = self._field(label)["selector"].items()
        return [o["value"] if isinstance(o, dict) else o for o in config.get("options", [])]

    def menu(self):
        """The menu's options by their labels."""
        assert self.step["type"] == "menu", self.step
        return {self._label("menu_options", o): o for o in self.step["menu_options"]}

    async def choose(self, label):
        options = self.menu()
        assert label in options, f"{label!r} not among {sorted(options, key=str)}"
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}",
                                       {"next_step_id": options[label]})

    async def fill(self, values):
        """Fill in by label and submit. As in the browser, a field left alone
        is sent with what the form showed in it, and an empty one is not
        sent."""
        values = {self.field(name): value for name, value in values.items()}
        data = {}
        for label, field in self.fields().items():
            value = values[label] if label in values else self.shown(label)
            if value is not None:
                data[field["name"]] = value
        self.step = await self.ui.post(f"{self.path}/{self.step['flow_id']}", data)
