# SPDX-License-Identifier: BSD-3-Clause
"""docs/l1-format.md names every key of a trip line as the code writes and
prints it — the stored keys of ``trips.Trip`` and the cost keys
``trips.with_costs`` adds, each beside its quality — and states the
quality each distance source gives (ISSUE-0174)."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from vledger import trips

DOC = Path(__file__).resolve().parents[1] / "docs" / "l1-format.md"


def _trips_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## Trips")
    return text[start:text.index("\n## ", start + 1)]


def _table_keys(section: str) -> set[str]:
    keys = set()
    for row in re.findall(r"^\| (.+?) \|", section, re.MULTILINE):
        keys.update(re.findall(r"`([a-z0-9_]+)`", row))
    return keys


ENVELOPE = {"kind", "subject", "start", "end", "quality", "version"}


def test_the_trip_table_names_every_key_a_trip_line_stores():
    stored = {f.name for f in dataclasses.fields(trips.Trip)}
    section = _trips_section()
    assert stored - ENVELOPE - _table_keys(section) == set()
    # The envelope's keys are named in the sentence before the table.
    assert all(f"`{k}`" in section[:section.index("| key |")] for k in ENVELOPE)


def test_the_trip_table_names_every_cost_key_and_its_quality():
    costs = set(trips.COST_KEYS) | {k + "_quality" for k in trips.COST_KEYS}
    assert costs - _table_keys(_trips_section()) == set()


def test_the_table_gives_each_distance_source_its_quality():
    section = _trips_section()
    row = next(r for r in section.splitlines()
               if r.startswith("| ") and "`distance_quality`" in r.split(" | ")[0])
    for source, quality in (("odometer", "measured"), ("trip_counter", "measured"),
                            ("waypoints", "estimated")):
        assert re.search(rf"`{source}`[^|]*?`{quality}`", row), (source, quality)
