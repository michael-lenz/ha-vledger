# SPDX-License-Identifier: BSD-3-Clause
"""Vehicle Ledger — the Home Assistant integration.

A thin shell over the ``vledger`` library (ARC-02): capture, config and
options flows, entities, actions. No derivation lives here. One config
entry is one vehicle or one charge point: one :class:`Capture` per entry
writes its L0 stream (ADR-0008), and one :class:`L1Writer` beside it keeps
its L1 with the library's verbs (ADR-0009).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from vledger.layout import Subject

from .capture import Capture
from .const import (
    DATA_KIND,
    DATA_SUBJECT,
    DOMAIN,
    KIND_VEHICLE,
    OPT_BASE_PATH,
    SERVICE_RECOMPUTE,
)
from .l1view import L1View
from .l1writer import L1Writer
from .receipt_desk import ReceiptDesk
from .services import async_register

PLATFORMS = [Platform.BUTTON, Platform.DATETIME, Platform.NUMBER, Platform.SELECT,
             Platform.SENSOR, Platform.SWITCH, Platform.TEXT]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

RECOMPUTE_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


@dataclass
class Vledger:
    """What one config entry runs: the raw log and the derivation on disk,
    and for a vehicle the receipt desk (ADR-0015) and what its event
    entities display (ADR-0016)."""

    capture: Capture
    l1: L1Writer
    desk: ReceiptDesk | None
    view: L1View | None = None


type VledgerConfigEntry = ConfigEntry[Vledger]


def config_of(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """The configuration object the ``config`` line records: the options
    minus where they are stored, and for a vehicle Home Assistant's time
    zone, where its calendar months and years begin (ADR-0014, point 2) —
    Home Assistant's setting, not an option, so it is taken at every start."""
    config = {k: v for k, v in entry.options.items() if k != OPT_BASE_PATH}
    if entry.data[DATA_KIND] == KIND_VEHICLE:
        config["time_zone"] = hass.config.time_zone
    return config


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def recompute(call: ServiceCall) -> None:
        # The whole history, recomputed (ABL-02): of one entry, or of all.
        loaded = [e for e in hass.config_entries.async_entries(DOMAIN)
                  if e.state is ConfigEntryState.LOADED]
        wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if wanted is not None:
            loaded = [e for e in loaded if e.entry_id == wanted]
            if not loaded:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded",
                                             translation_placeholders={"entry": wanted})
        for e in loaded:
            await e.runtime_data.l1.async_recompute()

    hass.services.async_register(DOMAIN, SERVICE_RECOMPUTE, recompute, schema=RECOMPUTE_SCHEMA)
    # The receipt actions, likewise for the domain (ADR-0015, point 1).
    async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry) -> bool:
    subject = Subject(entry.data[DATA_KIND], entry.data[DATA_SUBJECT])
    base = Path(entry.options.get(OPT_BASE_PATH) or hass.config.path("vledger"))
    capture = Capture(hass, subject, base, config_of(hass, entry))
    writer = L1Writer(hass, capture)
    vehicle = subject.kind == KIND_VEHICLE
    desk = ReceiptDesk(hass, capture, writer) if vehicle else None
    view = L1View(hass, capture, writer) if vehicle else None
    entry.runtime_data = Vledger(capture, writer, desk, view)
    await capture.async_start()
    await writer.async_start()
    if desk:
        desk.async_start()
    if view:
        view.async_start()

    async def on_hass_stop(_: Event) -> None:
        await writer.async_stop()
        await capture.async_stop("shutdown")

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_hass_stop))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: VledgerConfigEntry) -> None:
    # Options changed: the stream gets stop (reload), start and the new
    # config line, by reloading the entry (ADR-0008, point 4).
    entry.runtime_data.capture.stop_reason = "reload"
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VledgerConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if entry.runtime_data.desk:
        entry.runtime_data.desk.async_stop()
    if entry.runtime_data.view:
        entry.runtime_data.view.async_stop()
    await entry.runtime_data.l1.async_stop()
    capture = entry.runtime_data.capture
    await capture.async_stop(capture.stop_reason or "unload")
    return ok
