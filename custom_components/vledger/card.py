# SPDX-License-Identifier: BSD-3-Clause
"""The Lovelace card (ADR-0039, point 2): the ``frontend/`` directory served
as a static path, and the card's module added to every frontend, so nobody
adds a Lovelace resource by hand. The module's URL carries the
integration's version, which the card passes on to what it imports: a
release reloads the card past the browser's cache, and a card can never
run against an integration of another version."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

URL_BASE = f"/{DOMAIN}/frontend"
MODULE = "vledger-card.js"
DIRECTORY = Path(__file__).parent / "frontend"


async def async_register(hass: HomeAssistant) -> str:
    """Serve the card and load it in every frontend; the module's URL."""
    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(DIRECTORY), cache_headers=True)])
    url = f"{URL_BASE}/{MODULE}?v={version}"
    add_extra_js_url(hass, url)
    return url
