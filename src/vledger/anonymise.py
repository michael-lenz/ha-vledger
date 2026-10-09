# SPDX-License-Identifier: BSD-3-Clause
"""Anonymising a data directory, so a real stream can become a test
fixture (QUA-01).

Every subject under a base is copied to another base, line by line through
the library's own writers, with two things changed and nothing else:

- **Positions move by a fixed offset** in degrees — the position role's
  ``latitude`` and ``longitude`` attributes, the sensor-pair roles' states,
  a charge point's configured position — the same offset for every
  subject, so a session still falls within its charge point's radius. The
  longitude part keeps every distance exactly; the latitude part keeps
  north–south distances and scales east–west ones by
  cos(φ + Δφ) / cos(φ), about 2 % per degree at 51° N. The derivations of
  a fixture are taken from the anonymised stream, so nothing compares
  against the original; a small latitude shift keeps the copy close to it.
- **Names are dropped**: a subject's configured ``name``; every entity id,
  replaced by ``<domain>.<role>`` (a suffix where one role had several);
  every zone other than ``home`` and ``not_home``, replaced by ``zone_1``,
  ``zone_2``… in order of first appearance; a receipt's ``place``,
  ``provider`` and ``note``.

Times, values, units, subject ids and receipt ids are kept: they are what
the derivations read, and none of them names anything.
"""

from __future__ import annotations

from pathlib import Path

from vledger import l0, layout, receipts
from vledger.layout import Subject

KEPT_ZONES = ("home", "not_home")
FREE_TEXT = ("place", "provider", "note")
UNREPORTED = ("unavailable", "unknown")


class Anonymiser:
    """One consistent renaming and one offset, across every subject copied."""

    def __init__(self, dlat: float, dlon: float):
        self.dlat, self.dlon = float(dlat), float(dlon)
        self.entities: dict[str, str] = {}
        self.zones: dict[str, str] = {}

    # --- the atoms ---------------------------------------------------------

    def latitude(self, value: float) -> float:
        out = round(float(value) + self.dlat, 7)
        if not -90 <= out <= 90:
            raise ValueError(f"latitude {value} shifted by {self.dlat} leaves the globe")
        return out

    def longitude(self, value: float) -> float:
        return round((float(value) + self.dlon + 180) % 360 - 180, 7)

    def entity(self, entity: str, hint: str) -> str:
        """The new id of an entity: its domain and the role it served."""
        if entity not in self.entities:
            domain = entity.split(".", 1)[0] if "." in entity else "sensor"
            name, n = f"{domain}.{hint}", 1
            while name in self.entities.values():
                n += 1
                name = f"{domain}.{hint}_{n}"
            self.entities[entity] = name
        return self.entities[entity]

    def zone(self, state: str) -> str:
        if state in KEPT_ZONES or state in UNREPORTED:
            return state
        return self.zones.setdefault(state, f"zone_{len(self.zones) + 1}")

    # --- the lines ---------------------------------------------------------

    def _reading(self, entry: dict) -> dict:
        """A state line or a snapshot entry: the same keys either way."""
        e = dict(entry)
        role = e.get("role")
        if "entity" in e:
            e["entity"] = self.entity(e["entity"], role or "entity")
        state = e.get("state")
        if role == "position":
            e["state"] = self.zone(state)
            attrs = dict(e.get("attrs") or {})
            if isinstance(attrs.get("latitude"), (int, float)):
                attrs["latitude"] = self.latitude(attrs["latitude"])
            if isinstance(attrs.get("longitude"), (int, float)):
                attrs["longitude"] = self.longitude(attrs["longitude"])
            if attrs:
                e["attrs"] = attrs
        elif role in ("position_latitude", "position_longitude") and state not in UNREPORTED:
            shift = self.latitude if role == "position_latitude" else self.longitude
            try:
                e["state"] = repr(shift(float(state)))
            except (TypeError, ValueError):
                pass    # not a number: the derivation reads it as nothing either way
        return e

    def _config(self, config: dict, kind: str) -> dict:
        c = dict(config)
        if "name" in c:
            c["name"] = kind
        roles = {}
        for role, spec in (c.get("roles") or {}).items():
            spec = dict(spec)
            if spec.get("entity"):
                spec["entity"] = self.entity(spec["entity"], role)
            roles[role] = spec
        if "roles" in c:
            c["roles"] = roles
        if isinstance(c.get("meter"), dict) and c["meter"].get("entity"):
            c["meter"] = dict(c["meter"], entity=self.entity(c["meter"]["entity"], "energy_meter"))
        if isinstance(c.get("latitude"), (int, float)):
            c["latitude"] = self.latitude(c["latitude"])
        if isinstance(c.get("longitude"), (int, float)):
            c["longitude"] = self.longitude(c["longitude"])
        return c

    def line(self, line: dict, subject: Subject) -> dict:
        kind = line.get("kind")
        if kind == "state":
            return self._reading(line)
        if kind == "start":
            return dict(line, snapshot=[self._reading(e) for e in line.get("snapshot") or []])
        if kind == "config":
            return dict(line, config=self._config(line["config"], subject.kind))
        return dict(line)

    def receipt(self, line: dict) -> dict:
        r = dict(line)
        for key in FREE_TEXT:
            if r.get(key) is not None:
                r[key] = None
        return r


def copy(base: Path, to: Path, dlat: float, dlon: float,
         only: Subject | None = None) -> dict[Subject, int]:
    """Copy every subject under ``base`` — or only ``only`` — anonymised to
    ``to``, which must not hold that subject yet. Returns the lines copied
    per subject, receipts included."""
    subjects = [only] if only else layout.subjects(base)
    for subject in subjects:
        if layout.subject_dir(to, subject).exists():
            raise ValueError(f"{layout.subject_dir(to, subject)} exists; anonymise into a fresh directory")
        if not layout.l0_files(base, subject):
            raise FileNotFoundError(f"no stream for {subject.dirname} under {base}")
    a = Anonymiser(dlat, dlon)
    counts: dict[Subject, int] = {}
    for subject in subjects:
        n = 0
        for path in layout.l0_files(base, subject):
            for r in l0.read_file(path):
                l0.append(to, subject, a.line(r.line, subject))
                n += 1
        for r in receipts.read(base, subject):
            receipts.append(to, subject, a.receipt(r.line))
            n += 1
        counts[subject] = n
    return counts
