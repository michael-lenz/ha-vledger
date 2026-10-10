# SPDX-License-Identifier: BSD-3-Clause
"""Exports: renderings of L1 for other tools (CLI-03, ABL-05).

JSON Lines is the L1 of record (ADR-0009); what this module writes is
derived from it and from nothing else, and never by the live path: CSV and
JSON of one kind of event, GPX of the trips. Every export reads the files
in ``l1/`` as they are — it derives nothing — so it is as current as L1.

An export is made by the verb and by the Home Assistant action alike
(ADR-0017): both select with :func:`selected` and render with
:func:`render`, so neither can produce what the other cannot.

A CSV has one row per event and a column order fixed per kind, not taken
from the data, so two exports of the same kind line up whatever they
hold. A nested value is flattened: a position becomes its ``<key>.t``,
``<key>.latitude``, ``<key>.longitude`` and ``<key>.accuracy_m``
columns, ``refined_by`` its ``.start`` and ``.end``, a list of ids one cell
of ids separated by spaces. A trip's waypoints are the GPX's and stay out
of the CSV.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree as ET

from vledger import __version__, clock, l1, periods
from vledger.layout import Subject

FORMATS = ("csv", "json", "gpx")

ENVELOPE = ("kind", "subject", "start", "end", "quality", "version")
POSITION = ("t", "latitude", "longitude", "accuracy_m")

#: What a matched refuelling or charging session carries besides its
#: detection (receipts-format.md, l1-format.md *Receipts in events*).
_CONFIRMATION = ("receipt", "confirmation", "contenders")
_PLAUSIBILITY = ("deviation_pct", "implausible")

#: The columns of each kind, in order, before flattening. A key an event
#: holds and this does not name is appended after them, sorted — kept, never
#: dropped — and the tests fail on one, so the list is kept complete.
COLUMNS: dict[str, tuple[str, ...]] = {
    "trip": ENVELOPE + (
        "distance_km", "distance_quality", "distance_source",
        "start_position", "end_position", "start_zone", "end_zone",
        "outside_temperature_c", "delta_soc_pct", "delta_fuel_l",
        "refined_by", "movements_while_plugged"),
    "refuelling": ENVELOPE + (
        "position", "zone", "level_before_l", "level_after_l", "settled_at",
        "sensor_delta_l", "sensor_delta_quality", "price_suggestion", "flap_opened_at",
        *_CONFIRMATION,
        "quantity_l", "quantity_quality", "price", "unit_price", "price_quality",
        "full", "place", "fuel", "note", *_PLAUSIBILITY),
    "charging": ENVELOPE + (
        "source", "soc_start_pct", "soc_end_pct", "delta_soc_pct", "position",
        "chargepoint", "chargepoint_name", "battery_kwh",
        "grid_kwh", "grid_kwh_quality", "grid_kwh_source", "meter_attributable",
        "tariff_eur_per_kwh", "cost_eur", "cost_quality", "kwh_per_pct",
        "charging_loss_kwh", "movements_while_charging",
        *_CONFIRMATION,
        "sensor_grid_kwh", "sensor_grid_kwh_quality", "sensor_grid_kwh_source",
        "place", "provider", "note", *_PLAUSIBILITY),
    "period": ENVELOPE + ("period", "open") + tuple(
        k for m in periods.METRICS for k in (m, m + "_quality")) + (
        "fuel_level_corrected", "soc_corrected", "gaps", *periods.CONSUMPTION),
}

#: Keys whose value is a position (series.fix_dict), and the one dict that is not.
POSITIONS = {"start_position", "end_position", "position"}
SUBKEYS = {"refined_by": ("start", "end")}

#: Left out of the CSV: what another format carries better.
NOT_IN_CSV = {"waypoints"}


class NoL1(Exception):
    """Nothing to render: L1 has not been derived."""


def status(base: Path, subject: Subject) -> str | None:
    """Why L1 is not current, or ``None`` when it is; :class:`NoL1` without
    one. An export renders L1 as it is and derives nothing, so a stale one
    is reported, not repaired."""
    if l1.read_manifest(base, subject) is None:
        raise NoL1(f"no L1 for {subject.dirname}: derive all --write first")
    return l1.rebuild_due(base, subject)


def selected(base: Path, subject: Subject, kind: str, since: str | None = None,
             until: str | None = None) -> tuple[list[dict], str | None]:
    """One kind's events as L1 holds them, within ``since`` and ``until``
    by their start, and :func:`status`'s note."""
    note = status(base, subject)
    return within(l1.read(base, subject, kind), since, until), note


def render(fmt: str, kind: str, events: list[dict]) -> str:
    """The events in one of :data:`FORMATS`; GPX takes the trips."""
    if fmt == "csv":
        return to_csv(kind, events)
    if fmt == "json":
        return to_json(events)
    if fmt == "gpx":
        return to_gpx(events)
    raise ValueError(f"unknown export format {fmt!r}; one of {FORMATS}")


def filename(fmt: str, kind: str) -> str:
    """The default file of a kind in a format: its L1 file's name with the
    format's extension — ``trips.csv``, ``periods.json``, ``trips.gpx``."""
    return f"{Path(l1.FILES[kind]).stem}.{fmt}"


def within(events: Iterable[dict], since: str | None = None,
           until: str | None = None) -> list[dict]:
    """The events whose ``start`` lies in [since, until], both optional."""
    lo = clock.parse(since) if since else None
    hi = clock.parse(until) if until else None
    out = []
    for e in events:
        t = clock.parse(e["start"])
        if (lo is None or t >= lo) and (hi is None or t <= hi):
            out.append(e)
    return out


# --- CSV -------------------------------------------------------------------

def _subkeys(key: str) -> tuple[str, ...] | None:
    return POSITION if key in POSITIONS else SUBKEYS.get(key)


def columns(kind: str, events: list[dict]) -> list[str]:
    """The header: the kind's declared columns, flattened, then any key no
    declaration names, sorted."""
    declared = COLUMNS.get(kind, ENVELOPE)
    extra = sorted({k for e in events for k in e} - set(declared) - NOT_IN_CSV)
    out = []
    for key in (*declared, *extra):
        sub = _subkeys(key)
        out.extend(f"{key}.{s}" for s in sub) if sub else out.append(key)
    return out


def _cell(value) -> str:
    """A scalar as JSON spells it — ``true``, ``1.5`` — a string as itself,
    nothing for ``null``; a list of scalars separated by spaces."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        return " ".join(_cell(v) for v in value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def row(event: dict, header: list[str]) -> list[str]:
    out = []
    for col in header:
        key, _, sub = col.partition(".")
        value = event.get(key)
        if sub:
            value = value.get(sub) if isinstance(value, dict) else None
        out.append(_cell(value))
    return out


def to_csv(kind: str, events: list[dict]) -> str:
    header = columns(kind, events)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for e in events:
        w.writerow(row(e, header))
    return buf.getvalue()


# --- JSON ------------------------------------------------------------------

def to_json(events: list[dict]) -> str:
    """One JSON array of the events, each exactly as L1 holds it."""
    return json.dumps(events, indent=2, ensure_ascii=False) + "\n"


# --- GPX -------------------------------------------------------------------

GPX_NS = "http://www.topografix.com/GPX/1/1"
GPX_SCHEMA = "http://www.topografix.com/GPX/1/1/gpx.xsd"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def to_gpx(trips: list[dict]) -> str:
    """GPX 1.1: one track per trip, named by its start, holding one segment
    of its waypoints with their times — the fix before it moved first. A
    trip without a waypoint is a track with an empty segment, so the count
    of tracks is the count of trips."""
    ET.register_namespace("", GPX_NS)
    ET.register_namespace("xsi", XSI_NS)

    def el(parent, tag, text=None, **attrs):
        e = ET.SubElement(parent, f"{{{GPX_NS}}}{tag}", attrs)
        if text is not None:
            e.text = text
        return e

    root = ET.Element(f"{{{GPX_NS}}}gpx", {
        "version": "1.1", "creator": f"vledger {__version__}",
        f"{{{XSI_NS}}}schemaLocation": f"{GPX_NS} {GPX_SCHEMA}"})
    for n, trip in enumerate(trips, 1):
        trk = el(root, "trk")
        el(trk, "name", trip["start"])
        km = trip.get("distance_km")
        desc = f"{trip['start']} to {trip['end']}, quality {trip['quality']}"
        if km is not None:
            desc += f", {km} km ({trip.get('distance_source')})"
        el(trk, "desc", desc)
        el(trk, "number", str(n))
        seg = el(trk, "trkseg")
        for w in trip.get("waypoints") or []:
            pt = el(seg, "trkpt", lat=repr(float(w["latitude"])), lon=repr(float(w["longitude"])))
            el(pt, "time", w["t"])
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"
