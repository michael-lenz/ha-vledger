# SPDX-License-Identifier: BSD-3-Clause
"""Vehicle Ledger — the Home Assistant integration.

A thin shell over the ``vledger`` library (ARC-02): capture, config and
options flows, entities. No derivation lives here. One config entry is one
vehicle or one charge point, and one :class:`Capture` per entry writes its
L0 stream (ADR-0007).
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from vledger.layout import Subject

from .capture import Capture
from .const import DATA_KIND, DATA_SUBJECT, OPT_BASE_PATH

PLATFORMS = [Platform.SENSOR]

type VledgerConfigEntry = ConfigEntry[Capture]


def config_of(entry: ConfigEntry) -> dict:
    """The configuration object the ``config`` line records: the options
    minus where they are stored."""
    return {k: v for k, v in entry.options.items() if k != OPT_BASE_PATH}


async def async_setup_entry(hass: HomeAssistant, entry: VledgerConfigEntry) -> bool:
    subject = Subject(entry.data[DATA_KIND], entry.data[DATA_SUBJECT])
    base = Path(entry.options.get(OPT_BASE_PATH) or hass.config.path("vledger"))
    capture = Capture(hass, subject, base, config_of(entry))
    entry.runtime_data = capture
    await capture.async_start()

    async def on_hass_stop(_: Event) -> None:
        await capture.async_stop("shutdown")

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_hass_stop))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: VledgerConfigEntry) -> None:
    # Options changed: the stream gets stop (reload), start and the new
    # config line, by reloading the entry (ADR-0007, point 4).
    entry.runtime_data.stop_reason = "reload"
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VledgerConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    capture = entry.runtime_data
    await capture.async_stop(capture.stop_reason or "unload")
    return ok
