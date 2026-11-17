# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0034 with the ``vledger`` command: written L0 records are never
modified.

Derived from the requirement ("The system never modifies L0 records once
written"), README.md ("Every change of a source entity you assign to a role
becomes one record in a raw log (L0): JSON Lines, append-only, one stream per
vehicle and per charge point, rotated monthly"; L1 is "recomputable from
scratch at any time" from L0, receipts and configuration) and the user guide:
*The raw log: ``vledger l0``* (the writing verbs ``start``, ``config``,
``state``, ``heartbeat`` and ``stop``, each with ``--t``; the reading,
checking, counting, gaps and anonymising verbs), *The derivations*, *Receipts*
("kept in ``receipts.jsonl`` next to L0"), *The derivation on disk* (L1,
"files next to L0 under ``l1/``"; ``l1 clean`` deletes it), *Exports* and
*Reports*.

What a written record is, a user sees as bytes in the files a writing verb
leaves under the data directory. So "never modified" is checked on those
bytes: a write of the raw log only ever adds to them, so every file's earlier
content stays its prefix, and no file goes; a month that is over keeps its
file byte for byte; and no verb that reads the raw log — or derives from it,
takes a receipt, writes or deletes L1, exports, reports or anonymises —
changes a byte of it, nor drops a file of it. Which file holds which month,
and what is in a line, the user guide leaves to the design and is not looked
at. Nothing of ``vledger`` is imported: the command runs in a process of its
own."""

import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.req("REQ-0034")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
CHARGEPOINT = "c3d4"
T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
OCTOBER = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
SNAPSHOT = [
    {"role": "odometer", "entity": "sensor.volvo_odometer", "state": "123456",
     "unit": "km", "since": "2026-09-27T22:41:10Z"},
    {"role": "position", "entity": "device_tracker.volvo", "state": "home",
     "since": "2026-09-27T22:41:10Z"},
    {"role": "fuel_level", "entity": "sensor.volvo_fuel", "state": "21.5",
     "unit": "L", "since": "2026-09-27T22:41:10Z"},
]


def vledger(base, *argv, ok=True):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    if ok:
        assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def files(base):
    """Every file under the data directory, by its path, with its bytes."""
    return {str(p.relative_to(base)): p.read_bytes()
            for p in sorted(pathlib.Path(base).rglob("*")) if p.is_file()}


def assert_only_added(before, after):
    """Every file of ``before`` is still there, its bytes the start of what
    it holds now."""
    for name, old in before.items():
        assert name in after, f"{name} is gone"
        assert after[name][:len(old)] == old, f"{name} was changed, not added to"


def assert_untouched(before, after):
    for name, old in before.items():
        assert name in after, f"{name} is gone"
        assert after[name] == old, f"{name} was changed"


def state(base, t, role, entity, value, *extra, subject=("--vehicle", VEHICLE)):
    vledger(base, "l0", "state", *subject, "--t", iso(t), "--role", role,
            "--entity", entity, "--state", value, *extra)


def writes(base):
    """The raw log of a vehicle and of a charge point as the integration
    writes it, one verb at a time, across a crash, an orderly stop and the
    turn of a month: a trip away from home and back, a rise of the fuel
    level at an unchanged odometer, heartbeats in between. Each write comes
    with the time it is given, so that a test can stop at a month's end."""
    v = ("--vehicle", VEHICLE)
    cp = ("--chargepoint", CHARGEPOINT)
    snapshot = json.dumps(SNAPSHOT)

    def start(t, subject, snap):
        return t, lambda: vledger(base, "l0", "start", *subject, "--t", iso(t),
                                  "--homeassistant", "2026.10.1", "--snapshot", snap)

    def heartbeat(t, lines):
        return t, lambda: vledger(base, "l0", "heartbeat", *v, "--t", iso(t),
                                  "--lines", str(lines))

    def stop(t, subject):
        return t, lambda: vledger(base, "l0", "stop", *subject, "--t", iso(t),
                                  "--reason", "shutdown")

    def config(t):
        return t, lambda: vledger(base, "l0", "config", *v, "--t", iso(t),
                                  "--config", json.dumps({"name": "Volvo"}))

    def at(t, role, entity, value, *extra):
        return t, lambda: state(base, t, role, entity, value, *extra)

    t = T0
    yield start(t, v, snapshot)
    yield config(t + MINUTE)
    yield start(t + MINUTE, cp, "[]")
    yield at(t + 10 * MINUTE, "position", "device_tracker.volvo", "not_home",
             "--attr", "latitude=48.1371", "--attr", "longitude=11.5754",
             "--attr", "gps_accuracy=12")
    for k in range(1, 4):
        yield at(t + (10 + 5 * k) * MINUTE, "odometer", "sensor.volvo_odometer",
                 str(123456 + 4 * k), "--unit", "km")
    yield at(t + 30 * MINUTE, "position", "device_tracker.volvo", "home")
    yield at(t + 31 * MINUTE, "fuel_level", "sensor.volvo_fuel", "19.9", "--unit", "L")
    yield heartbeat(t + HOUR, 6)
    # Home Assistant dies here: no stop, and the next start is a new session.
    t = T0 + 5 * HOUR
    yield start(t, v, snapshot)
    yield at(t + 10 * MINUTE, "fuel_level", "sensor.volvo_fuel", "61.0", "--unit", "L")
    yield at(t + 20 * MINUTE, "fuel_level", "sensor.volvo_fuel", "61.2", "--unit", "L")
    yield heartbeat(t + HOUR, 2)
    yield stop(t + 2 * HOUR, v)
    yield stop(t + 2 * HOUR, cp)
    # The month turns: both streams go on in October.
    t = OCTOBER
    yield start(t, v, snapshot)
    yield start(t, cp, "[]")
    yield at(t + 10 * MINUTE, "odometer", "sensor.volvo_odometer", "123470", "--unit", "km")
    yield heartbeat(t + HOUR, 1)


def write_all(base):
    for _, write in writes(base):
        write()
    return files(base)


def test_every_write_only_adds_to_the_raw_log(tmp_path):
    seen = {}
    for _, write in writes(tmp_path):
        write()
        now = files(tmp_path)
        assert_only_added(seen, now)
        seen = now
    assert seen, "the writing verbs wrote nothing under the data directory"


def test_a_state_line_with_an_earlier_time_is_added_not_put_in_between(tmp_path):
    before = write_all(tmp_path)
    state(tmp_path, OCTOBER + 5 * MINUTE, "odometer", "sensor.volvo_odometer",
          "123468", "--unit", "km")
    assert_only_added(before, files(tmp_path))


def test_a_month_that_is_over_keeps_its_file_byte_for_byte(tmp_path):
    september = [w for t, w in writes(tmp_path) if t < OCTOBER]
    october = [w for t, w in writes(tmp_path) if t >= OCTOBER]
    for write in september:
        write()
    at_end_of_september = files(tmp_path)
    for write in october:
        write()
    after = files(tmp_path)
    assert set(after) - set(at_end_of_september), \
        "October's lines went into no file of their own"
    assert_untouched(at_end_of_september, after)


def test_one_stream_s_writes_leave_another_s_files_as_they_were(tmp_path):
    before = write_all(tmp_path)
    chargepoint = {k: v for k, v in before.items() if CHARGEPOINT in k}
    assert chargepoint, "the charge point's stream is not under the data directory"
    state(tmp_path, datetime(2026, 10, 2, 9, 0, tzinfo=UTC), "odometer",
          "sensor.volvo_odometer", "123480", "--unit", "km")
    vledger(tmp_path, "l0", "heartbeat", "--vehicle", VEHICLE,
            "--t", "2026-10-02T10:00:00Z", "--lines", "1")
    assert_untouched(chargepoint, files(tmp_path))


READING = [
    ("l0", "read", "--vehicle", VEHICLE),
    ("l0", "read", "--vehicle", VEHICLE, "--kind", "state", "--role", "odometer"),
    ("l0", "read", "--chargepoint", CHARGEPOINT),
    ("l0", "validate", "--vehicle", VEHICLE),
    ("l0", "validate", "--vehicle", VEHICLE, "--json"),
    ("l0", "stats", "--vehicle", VEHICLE, "--json"),
    ("l0", "gaps", "--vehicle", VEHICLE, "--now", "2026-10-02T09:00:00Z", "--json"),
]

DERIVING = [
    ("derive", "trips", "--vehicle", VEHICLE),
    ("derive", "refuellings", "--vehicle", VEHICLE),
    ("derive", "charging", "--vehicle", VEHICLE),
    ("derive", "periods", "--vehicle", VEHICLE),
    ("derive", "trips", "--vehicle", VEHICLE, "--write"),
    ("derive", "refuellings", "--vehicle", VEHICLE, "--write"),
    ("derive", "charging", "--vehicle", VEHICLE, "--write"),
    ("derive", "all", "--vehicle", VEHICLE, "--write"),
    ("derive", "periods", "--vehicle", VEHICLE, "--write"),
    ("l1", "update", "--vehicle", VEHICLE),
    ("l1", "read", "--vehicle", VEHICLE, "--kind", "trip"),
    ("l1", "read", "--vehicle", VEHICLE, "--kind", "period", "--current"),
    ("export", "csv", "--vehicle", VEHICLE, "--kind", "trip"),
    ("export", "json", "--vehicle", VEHICLE, "--kind", "refuelling"),
    ("report", "metrics", "--vehicle", VEHICLE, "--json"),
    ("derive", "match", "--vehicle", VEHICLE),
]


@pytest.mark.parametrize("argv", READING, ids=" ".join)
def test_reading_checking_and_counting_change_nothing_of_the_raw_log(tmp_path, argv):
    before = write_all(tmp_path)
    p = vledger(tmp_path, *argv, ok=False)
    assert p.returncode in (0, 1), (argv, p.returncode, p.stderr)
    assert_untouched(before, files(tmp_path))


@pytest.mark.parametrize("argv", DERIVING, ids=" ".join)
def test_deriving_writing_l1_exporting_and_reporting_change_nothing_of_the_raw_log(
        tmp_path, argv):
    before = write_all(tmp_path)
    if argv[0] in ("export", "report") or argv[:2] == ("derive", "periods") and "--write" in argv:
        # "An export derives nothing — run derive all --write first."
        vledger(tmp_path, "derive", "all", "--vehicle", VEHICLE, "--write")
    vledger(tmp_path, *argv)
    assert_untouched(before, files(tmp_path))


def test_l1_clean_deletes_l1_and_nothing_of_the_raw_log(tmp_path):
    before = write_all(tmp_path)
    vledger(tmp_path, "derive", "all", "--vehicle", VEHICLE, "--write")
    assert set(files(tmp_path)) > set(before), "derive all --write wrote no L1"
    vledger(tmp_path, "l1", "clean", "--vehicle", VEHICLE)
    assert_untouched(before, files(tmp_path))


def test_l1_kept_live_beside_a_growing_stream_only_adds_to_the_raw_log(tmp_path):
    before = write_all(tmp_path)
    vledger(tmp_path, "l1", "update", "--vehicle", VEHICLE)
    t = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    state(tmp_path, t, "position", "device_tracker.volvo", "not_home")
    for k in range(1, 3):
        state(tmp_path, t + 5 * k * MINUTE, "odometer", "sensor.volvo_odometer",
              str(123470 + 3 * k), "--unit", "km")
    state(tmp_path, t + 20 * MINUTE, "position", "device_tracker.volvo", "home")
    vledger(tmp_path, "l0", "heartbeat", "--vehicle", VEHICLE,
            "--t", iso(t + 3 * HOUR), "--lines", "4")
    grown = files(tmp_path)
    assert_only_added(before, grown)
    raw = {k: v for k, v in grown.items() if k in before}
    vledger(tmp_path, "l1", "update", "--vehicle", VEHICLE)
    assert_untouched(raw, files(tmp_path))


def test_receipts_added_corrected_and_cancelled_change_nothing_of_the_raw_log(tmp_path):
    before = write_all(tmp_path)
    added = vledger(tmp_path, "receipt", "add", "refuelling", "--vehicle", VEHICLE,
                    "--anchor", "2026-09-28T13:15Z", "--quantity-l", "39.6",
                    "--total-price", "69.30", "--full", "--place", "motorway services")
    receipt = json.loads(added.stdout.strip().splitlines()[-1])["id"]
    corrected = vledger(tmp_path, "receipt", "add", "refuelling", "--vehicle", VEHICLE,
                        "--replaces", receipt, "--anchor", "2026-09-28T13:15Z",
                        "--quantity-l", "39.9", "--total-price", "69.82", "--full")
    current = json.loads(corrected.stdout.strip().splitlines()[-1])["id"]
    vledger(tmp_path, "receipt", "cancel", current, "--vehicle", VEHICLE,
            "--note", "entered twice")
    vledger(tmp_path, "receipt", "list", "--vehicle", VEHICLE, "--all")
    assert_untouched(before, files(tmp_path))


def test_anonymising_copies_and_leaves_the_raw_log_as_it_was(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    before = write_all(base)
    vledger(base, "l0", "anonymise", "--shift", "0.05,-3.2", "--to",
            str(tmp_path / "fixture"))
    assert files(tmp_path / "fixture"), "anonymise wrote no copy"
    assert_untouched(before, files(base))
