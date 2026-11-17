# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0036, as a user meets it: receipts are stored separately from L0,
append-only; corrections and cancellations are new receipts referring to the
receipt concerned.

Black-box: every step is the ``vledger`` CLI run as a separate process, the
way the user guide's *Receipts: vledger receipt* shows it — a stream written
with ``vledger l0``, receipts entered, corrected (``--replaces``) and
cancelled with ``vledger receipt``, and read back with ``receipt list`` and
``receipt list --all``. Nothing here imports the library.

What the user can see of it:

- a receipt lands in ``receipts.jsonl`` next to L0, and the raw log is left
  as it was: no file of it changes, and ``l0 read`` shows no receipt;
- every receipt, correction and cancellation only adds to the receipts file:
  what was there before stays, byte for byte, at its start;
- a correction and a cancellation are receipts of their own, with an id of
  their own, naming the receipt they concern; the receipt concerned stays in
  ``receipt list --all`` and leaves ``receipt list``;
- a receipt that is no longer current cannot be corrected or cancelled
  again, and the refusal writes nothing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0036")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"


def at(minutes: int) -> str:
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    h, m = divmod(6 * 60 + minutes, 60)
    return f"2026-10-05T{h:02d}:{m:02d}:00.000Z"


def vledger(base: Path, *argv: str, expect: int | None = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``.
    ``expect=None`` asks only for a failure: an exit other than 0."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    # The process runs in another directory than the tests: a relative
    # PYTHONPATH such as the checks' ``src`` would no longer find vledger,
    # so the tree's own src goes first, as an absolute path.
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    if expect is None:
        assert p.returncode != 0, (argv, p.stdout, p.stderr)
    else:
        assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def json_lines(text: str) -> list[dict]:
    """The JSON Lines of a verb's output; a count or heading beside them is
    not a receipt."""
    return [json.loads(line) for line in text.splitlines() if line.lstrip().startswith("{")]


def values(line: dict) -> set[str]:
    """Every string a line holds, at any depth."""
    out: set[str] = set()
    stack: list = [line]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str):
            out.add(v)
    return out


CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "fuel_level": {"entity": "sensor.fuel_level"},
    },
}


def write_stream(base: Path) -> None:
    """A short stream as the integration would have logged it."""
    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": "1000", "unit": "km", "since": at(-120)},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": "20.5", "unit": "L", "since": at(-120)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60), "--homeassistant", "2026.9.4",
            "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60), "--config", json.dumps(CONFIG))
    for minutes, level in ((10, "20.1"), (50, "60.2"), (90, "58.0")):
        vledger(base, "l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", "fuel_level",
                "--entity", "sensor.fuel_level", "--state", level, "--unit", "L")


def refuelling(base: Path, *extra: str, litres: str = "41.37") -> dict:
    p = vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(45),
                "--quantity-l", litres, "--total-price", "72.36", "--full", *extra)
    [line] = json_lines(p.stdout)
    return line


def charging(base: Path) -> dict:
    p = vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(200),
                "--energy-kwh", "11.8", "--total-price", "6.49", "--provider", "roaming card")
    [line] = json_lines(p.stdout)
    return line


def files(base: Path) -> dict[str, bytes]:
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


def receipts_file(base: Path) -> Path:
    """The one ``receipts.jsonl`` the user guide puts next to L0."""
    found = [p for p in base.rglob("receipts.jsonl") if "l1" not in p.relative_to(base).parts]
    assert len(found) == 1, found
    return found[0]


def listed(base: Path, *flags: str) -> list[dict]:
    return json_lines(vledger(base, "receipt", "list", "--vehicle", VEHICLE, *flags).stdout)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    write_stream(tmp_path)
    return tmp_path


def test_a_receipt_is_kept_in_a_file_of_its_own_and_l0_is_left_as_it_was(base):
    raw_log = files(base)
    l0_read = vledger(base, "l0", "read", "--vehicle", VEHICLE).stdout
    first = refuelling(base, "--place", "motorway services")
    second = charging(base)

    after = files(base)
    assert {k: after[k] for k in raw_log} == raw_log, "a receipt changed a file of the raw log"
    added = set(after) - set(raw_log)
    assert added == {str(receipts_file(base).relative_to(base))}, added
    assert vledger(base, "l0", "read", "--vehicle", VEHICLE).stdout == l0_read
    for name, content in after.items():
        if name not in added:
            assert first["id"].encode() not in content and second["id"].encode() not in content, name
    stored = json_lines(receipts_file(base).read_text(encoding="utf-8"))
    assert [r["id"] for r in stored] == [first["id"], second["id"]]


def test_every_receipt_only_adds_to_the_receipts_file(base):
    refuelling(base)
    seen = receipts_file(base).read_bytes()
    for enter in (lambda: charging(base), lambda: refuelling(base, litres="12.5")):
        enter()
        now = receipts_file(base).read_bytes()
        assert len(now) > len(seen) and now.startswith(seen)
        seen = now


def test_a_correction_is_a_new_receipt_naming_the_one_it_corrects(base):
    wrong = refuelling(base)
    before = receipts_file(base).read_bytes()

    right = refuelling(base, "--replaces", wrong["id"], litres="41.73")

    assert right["id"] != wrong["id"]
    assert wrong["id"] in values(right)
    assert receipts_file(base).read_bytes().startswith(before)
    every = listed(base, "--all")
    assert wrong in every and right in every
    assert [r["id"] for r in listed(base)] == [right["id"]]


def test_a_cancellation_is_a_new_receipt_naming_the_one_it_cancels(base):
    kept = charging(base)
    twice = refuelling(base)
    before = receipts_file(base).read_bytes()

    vledger(base, "receipt", "cancel", twice["id"], "--vehicle", VEHICLE, "--note", "entered twice")

    assert receipts_file(base).read_bytes().startswith(before)
    every = listed(base, "--all")
    assert twice in every and kept in every
    [cancel] = [r for r in every if r["id"] not in (twice["id"], kept["id"])]
    assert twice["id"] in values(cancel)
    assert [r["id"] for r in listed(base)] == [kept["id"]]


def test_a_receipt_that_is_no_longer_current_is_neither_corrected_nor_cancelled(base):
    old = refuelling(base)
    new = refuelling(base, "--replaces", old["id"], litres="41.73")
    vledger(base, "receipt", "cancel", new["id"], "--vehicle", VEHICLE)
    before = receipts_file(base).read_bytes()

    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--replaces", old["id"],
            "--anchor", at(45), "--quantity-l", "40.0", "--total-price", "72.36", "--full", expect=None)
    vledger(base, "receipt", "cancel", old["id"], "--vehicle", VEHICLE, expect=None)
    vledger(base, "receipt", "cancel", new["id"], "--vehicle", VEHICLE, expect=None)
    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--replaces", new["id"],
            "--anchor", at(45), "--quantity-l", "40.0", "--total-price", "72.36", "--full", expect=None)

    assert receipts_file(base).read_bytes() == before
    assert listed(base) == []
