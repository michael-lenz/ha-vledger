# SPDX-License-Identifier: BSD-3-Clause
"""The export action (ADR-0017): ``vledger.export`` renders one kind of a
vehicle's L1 as CSV, JSON or GPX into the instance's local media directory,
under ``vledger/<vehicle>/``, one file per kind unless a name is given,
overwritten atomically — and answers with where it landed.

What it writes is what ``vledger export`` writes: the selection and the
rendering are the library's (:func:`export.selected`, :func:`export.render`),
read under the L1 writer's lock so a rebuild never swaps the directory
underneath. Nothing is derived here, and nothing is written anywhere but
that folder.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from vledger import clock, export

from .const import DOMAIN, EXPORT_FOLDER

#: What a bare file name must not hold: a path of any kind.
_FORBIDDEN = ("/", "\\", os.sep)


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key,
                                  translation_placeholders=placeholders)


def media_dir(hass: HomeAssistant) -> Path:
    """The instance's local media directory: ``media_dirs["local"]``, or the
    first configured one."""
    dirs = hass.config.media_dirs
    where = dirs.get("local") or next(iter(dirs.values()), None)
    if where is None:
        raise _error("no_media_dir")
    return Path(where)


def folder(hass: HomeAssistant, name: str) -> Path:
    """``<media>/vledger/<vehicle>/``, the vehicle by its name slugified."""
    return media_dir(hass) / EXPORT_FOLDER / slugify(name)


def check_filename(name: str, fmt: str) -> str:
    """A bare name with the format's extension; refused otherwise."""
    if (not name or name in (".", "..") or any(sep in name for sep in _FORBIDDEN)
            or not name.endswith("." + fmt) or len(name) <= len(fmt) + 1):
        raise _error("bad_filename", filename=name, format=fmt)
    return name


def _write(target: Path, text: str) -> None:
    """Write beside the file, then rename over it — the old export stays
    whole until the new one is complete. Blocking."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


async def async_export(hass: HomeAssistant, entry, *, fmt: str, kind: str | None = None,
                       since: datetime | None = None, until: datetime | None = None,
                       filename: str | None = None) -> dict:
    """The action's work for a loaded vehicle entry; the response."""
    if fmt == "gpx":
        if kind is not None:
            raise _error("kind_not_for_gpx", kind=kind)
        kind = "trip"
    elif kind is None:
        raise _error("kind_required", format=fmt)
    capture, writer = entry.runtime_data.capture, entry.runtime_data.l1
    name = check_filename(filename, fmt) if filename else export.filename(fmt, kind)
    target = folder(hass, capture.config.get("name") or capture.subject.id) / name
    if not await hass.async_add_executor_job(hass.config.is_allowed_path, str(target)):
        raise _error("path_not_allowed", path=str(target))
    bounds = [clock.to_text(dt_util.as_utc(t)) if t else None for t in (since, until)]

    def job() -> tuple[int, str | None]:
        events, due = export.selected(capture.base, capture.subject, kind, *bounds)
        _write(target, export.render(fmt, kind, events))
        return len(events), due

    try:
        count, due = await writer.async_read(job)
    except export.NoL1:
        raise _error("no_l1", name=capture.config.get("name") or capture.subject.id) from None
    response = {"path": str(target), "count": count, "current": due is None}
    if due:
        response["reason"] = due
    return response
