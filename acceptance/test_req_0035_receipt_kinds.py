# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0035, as a user meets it: there are two kinds of receipt.

- A refuelling receipt: quantity (L), total or per-litre price, full tank
  (yes/no), anchor time; optional: place, fuel type, note.
- A charging receipt: billed energy (kWh), total price, anchor time;
  optional: place, provider, note.

Black-box: every step is the ``vledger`` CLI run as a separate process, the
way the user guide's *Receipts: ``vledger receipt``* shows it — ``receipt
add refuelling``, ``receipt add charging`` and ``receipt list``. ``add``
prints the line it wrote; ``list`` prints the receipts that count. Nothing
here imports the library.

The line's keys are the options' names without the dashes, as the user
guide says of the Home Assistant actions (``quantity_l``, ``total_price``,
``full``, ``anchor``, ``id``); the fuel type's and the note's options are
those ``vledger receipt add refuelling --help`` names (``--fuel``,
``--note``), which the user guide does not (ISSUE-0145).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0035")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"


def vledger(base: Path, *argv: str, expect: int | None = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    # The process runs in the data directory: a relative PYTHONPATH such as
    # the checks' ``src`` would no longer find vledger there.
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    if expect is not None:
        assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def lines(out: str) -> list[dict]:
    """The JSON objects among a command's output lines."""
    return [json.loads(s) for s in out.splitlines() if s.startswith("{")]


def add(base: Path, kind: str, *options: str) -> dict:
    """``receipt add <kind>`` for the vehicle; the one line it printed."""
    written = lines(vledger(base, "receipt", "add", kind, "--vehicle", VEHICLE, *options).stdout)
    assert len(written) == 1, written
    return written[0]


def listed(base: Path, *options: str) -> list[dict]:
    """The receipts ``receipt list`` prints for the vehicle."""
    return lines(vledger(base, "receipt", "list", "--vehicle", VEHICLE, *options).stdout)


def refused(base: Path, kind: str, *options: str) -> None:
    """``receipt add <kind>`` is refused, with a message, and nothing is kept."""
    p = vledger(base, "receipt", "add", kind, "--vehicle", VEHICLE, *options, expect=None)
    assert p.returncode != 0, (kind, options, p.stdout)
    assert p.stderr.strip(), "a refusal says why"
    assert lines(p.stdout) == []
    assert listed(base, "--all") == []


ANCHOR = "2026-10-12T16:40Z"
ANCHOR_UTC = "2026-10-12T16:40:00"


def same_instant(written: str, expected: str = ANCHOR_UTC) -> bool:
    """An anchor written in UTC is the instant typed, to the second."""
    return written.endswith("Z") and written.startswith(expected)


# --- A refuelling receipt ---------------------------------------------------


def test_a_refuelling_receipt_holds_quantity_total_price_full_tank_and_anchor(tmp_path):
    line = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "41.37", "--total-price", "72.36", "--full")
    assert line["kind"] == "refuelling"
    assert line["quantity_l"] == pytest.approx(41.37)
    assert line["total_price"] == pytest.approx(72.36)
    assert line["full"] is True
    assert same_instant(line["anchor"]), line["anchor"]
    assert line["id"]
    assert listed(tmp_path) == [line]


def test_a_refuelling_receipt_may_give_the_per_litre_price_instead_of_the_total(tmp_path):
    line = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "20", "--unit-price", "1.799", "--full")
    assert line["unit_price"] == pytest.approx(1.799)
    assert line.get("total_price") is None
    assert listed(tmp_path) == [line]


def test_a_refuelling_receipt_may_give_both_prices(tmp_path):
    line = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "40", "--unit-price", "1.799", "--total-price", "71.96", "--full")
    assert line["unit_price"] == pytest.approx(1.799)
    assert line["total_price"] == pytest.approx(71.96)


@pytest.mark.parametrize(("flag", "full"), [("--full", True), ("--partial", False)])
def test_full_tank_is_yes_or_no(tmp_path, flag, full):
    line = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "20", "--total-price", "36", flag)
    assert line["full"] is full
    assert listed(tmp_path)[0]["full"] is full


def test_a_refuelling_receipt_keeps_its_place_fuel_type_and_note(tmp_path):
    line = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "41.37", "--total-price", "72.36", "--full",
               "--place", "motorway services", "--fuel", "Super E10", "--note", "paid by card")
    assert line["place"] == "motorway services"
    assert line["fuel"] == "Super E10"
    assert line["note"] == "paid by card"
    assert listed(tmp_path) == [line]


def test_a_refuelling_receipt_needs_none_of_place_fuel_type_and_note(tmp_path):
    line = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "41.37", "--total-price", "72.36", "--full")
    for key in ("place", "fuel", "note"):
        assert line.get(key) is None, key


@pytest.mark.parametrize(("missing", "options"), [
    ("quantity", ["--anchor", ANCHOR, "--total-price", "72.36", "--full"]),
    ("price", ["--anchor", ANCHOR, "--quantity-l", "41.37", "--full"]),
    ("full tank", ["--anchor", ANCHOR, "--quantity-l", "41.37", "--total-price", "72.36"]),
    ("anchor time", ["--quantity-l", "41.37", "--total-price", "72.36", "--full"]),
])
def test_a_refuelling_receipt_without_a_required_field_is_refused(tmp_path, missing, options):
    refused(tmp_path, "refuelling", *options)


def test_full_and_partial_together_are_refused(tmp_path):
    refused(tmp_path, "refuelling", "--anchor", ANCHOR,
            "--quantity-l", "41.37", "--total-price", "72.36", "--full", "--partial")


# --- A charging receipt -----------------------------------------------------


def test_a_charging_receipt_holds_energy_total_price_and_anchor(tmp_path):
    line = add(tmp_path, "charging", "--anchor", ANCHOR,
               "--energy-kwh", "11.8", "--total-price", "6.49")
    assert line["kind"] == "charging"
    assert line["energy_kwh"] == pytest.approx(11.8)
    assert line["total_price"] == pytest.approx(6.49)
    assert same_instant(line["anchor"]), line["anchor"]
    assert line["id"]
    assert listed(tmp_path) == [line]


def test_a_charging_receipt_keeps_its_place_provider_and_note(tmp_path):
    line = add(tmp_path, "charging", "--anchor", ANCHOR,
               "--energy-kwh", "11.8", "--total-price", "6.49",
               "--place", "supermarket car park", "--provider", "roaming card",
               "--note", "slow charger")
    assert line["place"] == "supermarket car park"
    assert line["provider"] == "roaming card"
    assert line["note"] == "slow charger"
    assert listed(tmp_path) == [line]


def test_a_charging_receipt_needs_none_of_place_provider_and_note(tmp_path):
    line = add(tmp_path, "charging", "--anchor", ANCHOR,
               "--energy-kwh", "11.8", "--total-price", "6.49")
    for key in ("place", "provider", "note"):
        assert line.get(key) is None, key


@pytest.mark.parametrize(("missing", "options"), [
    ("energy", ["--anchor", ANCHOR, "--total-price", "6.49"]),
    ("total price", ["--anchor", ANCHOR, "--energy-kwh", "11.8"]),
    ("anchor time", ["--energy-kwh", "11.8", "--total-price", "6.49"]),
])
def test_a_charging_receipt_without_a_required_field_is_refused(tmp_path, missing, options):
    refused(tmp_path, "charging", *options)


# --- Two kinds, and no third ------------------------------------------------


def test_the_two_kinds_keep_their_own_fields(tmp_path):
    fuel = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "41.37", "--total-price", "72.36", "--full")
    charge = add(tmp_path, "charging", "--anchor", "2026-10-14T09:10Z",
                 "--energy-kwh", "11.8", "--total-price", "6.49")
    for key in ("energy_kwh", "provider"):
        assert fuel.get(key) is None, key
    for key in ("quantity_l", "unit_price", "full", "fuel"):
        assert charge.get(key) is None, key


def test_receipts_are_listed_by_kind(tmp_path):
    fuel = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "41.37", "--total-price", "72.36", "--full")
    charge = add(tmp_path, "charging", "--anchor", "2026-10-14T09:10Z",
                 "--energy-kwh", "11.8", "--total-price", "6.49")
    assert listed(tmp_path) == [fuel, charge]
    assert listed(tmp_path, "--kind", "refuelling") == [fuel]
    assert listed(tmp_path, "--kind", "charging") == [charge]


@pytest.mark.parametrize("kind", ["parking", "toll", "service"])
def test_there_is_no_third_kind_of_receipt(tmp_path, kind):
    refused(tmp_path, kind, "--anchor", ANCHOR, "--total-price", "5")


def test_receipts_are_kept_in_receipts_jsonl(tmp_path):
    fuel = add(tmp_path, "refuelling", "--anchor", ANCHOR,
               "--quantity-l", "41.37", "--total-price", "72.36", "--full")
    charge = add(tmp_path, "charging", "--anchor", "2026-10-14T09:10Z",
                 "--energy-kwh", "11.8", "--total-price", "6.49")
    files = list(tmp_path.rglob("receipts.jsonl"))
    assert len(files) == 1, files
    kept = [json.loads(s) for s in files[0].read_text(encoding="utf-8").splitlines() if s.strip()]
    assert kept == [fuel, charge]
    assert {r["kind"] for r in kept} == {"refuelling", "charging"}
