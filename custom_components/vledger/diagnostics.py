# SPDX-License-Identifier: BSD-3-Clause
"""Home Assistant diagnostics for one vehicle or charge point (HAI-07).

The numbers the diagnostic entities show, plus what only a full count of
the stream can give — the measured sampling and change intervals per role
(ADR-0010), the last value of each role, every capture gap, the values the
state mapping does not list — counted with the library (``vledger l0 stats``) off the event
loop. Positions are redacted wherever they appear.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from vledger import clock, stats

from . import VledgerConfigEntry

#: Keys that carry a position: a tracker's attributes, a charge point's place.
TO_REDACT = {"latitude", "longitude"}

#: Roles whose state itself is a coordinate.
POSITION_ROLES = ("position_latitude", "position_longitude")


async def async_get_config_entry_diagnostics(hass: HomeAssistant,
                                             entry: VledgerConfigEntry) -> dict:
    capture = entry.runtime_data
    counted = await hass.async_add_executor_job(stats.scan, capture.base, capture.subject)
    counted.finder.close(clock.to_text(clock.now()))
    stream = stats.to_dict(counted)
    for role in POSITION_ROLES:
        if role in stream["last_states"]:
            stream["last_states"][role]["state"] = REDACTED
    return async_redact_data({
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "capture": capture.counts(),
        "stream": stream,
    }, TO_REDACT)
