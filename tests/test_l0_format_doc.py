# SPDX-License-Identifier: BSD-3-Clause
"""docs/l0-format.md states a charge point's configuration as the code
writes and reads it: the example's keys are those ``vconfig.chargepoint``
writes, the key table names each of them, and the example's tariffs are
read by ``tariff_at`` (ISSUE-0140)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from vledger import config as vconfig

DOC = Path(__file__).resolve().parents[1] / "docs" / "l0-format.md"


def _config_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("### `config`")
    return text[start:text.index("\n## ", start)]


def _chargepoint_example() -> dict:
    blocks = re.findall(r"```json\n(.*?)```", _config_section(), re.DOTALL)
    lines = [json.loads(b) for b in blocks if '"tariffs"' in b]
    assert len(lines) == 1, "one charge point example in l0-format.md's config"
    return lines[0]["config"]


def _table_keys(section: str) -> set[str]:
    keys = set()
    for row in re.findall(r"^\| (.+?) \|", section, re.MULTILINE):
        keys.update(re.findall(r"`([a-z_]+)`", row))
    return keys


def test_the_example_is_what_the_code_writes_for_a_charge_point():
    cp = _chargepoint_example()
    written = vconfig.chargepoint(cp["name"], cp["latitude"], cp["longitude"], cp["radius_m"],
                                  cp["tariffs"], cp["meter"], cp["power_kw"])
    assert written == cp
    assert vconfig.is_chargepoint(cp)


def test_the_key_table_names_every_key_of_a_charge_point():
    section = _config_section()
    table = section[section.index("A charge point"):]
    assert set(_chargepoint_example()) <= _table_keys(table)
    # A tariff entry's own keys are given where the table names tariffs.
    assert "`from`" in table and "`eur_per_kwh`" in table


def test_the_example_tariffs_are_read_as_the_text_says():
    tariffs = _chargepoint_example()["tariffs"]
    assert all(set(e) == {"from", "eur_per_kwh"} for e in tariffs)
    first = min(tariffs, key=lambda e: e["from"])
    assert vconfig.tariff_at(tariffs, f"{first['from']}T00:00:00.000Z") == first
    assert vconfig.tariff_at(tariffs, "2000-01-01T00:00:00.000Z") is None
