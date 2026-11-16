# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0024: a state record contains the schema version; the vehicle or charge
point id; the Home Assistant timestamp (UTC, ISO 8601, milliseconds); the
role; the entity_id; the raw value; the source unit; the role-relevant
attributes; and the source's own measurement time, where the source supplies
one.

Derived from the requirement, README.md ("The raw log's layout, version 5";
"Every change of a source entity you assign to a role becomes one record in a
raw log") and the user guide's *Where the data is* (a stream is named by
``--vehicle ID`` or ``--chargepoint ID``; "Times are UTC ISO 8601; a verb
that writes takes ``--t`` and defaults to now") and *The raw log: vledger l0*:
the verbs the integration writes a stream with, "usable by hand to build
one"; "only the attributes relevant to the role are kept — ``battery`` above
is dropped, and the verb prints the line it wrote"; "A state is always a
string"; ``--measured-at`` takes a time; ``l0 read`` prints the stream's
lines and ``l0 validate`` "names the schema versions the stream holds".

The keys are read as the verb prints the line it wrote, which is how a user
of the command meets them. Every test drives the ``vledger`` command as a
program; nothing of ``vledger`` is imported. The record as Home Assistant's
capture writes it is the other module, test_req_0024_state_record_ha.py."""

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0024")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# ISO 8601 in UTC to the millisecond, as the requirement asks for it.
UTC_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(Z|\+00:00)$")


def vledger(base: Path, *argv: str, expect: int = 0) -> str:
    """``vledger <argv> --base <base>`` in a process of its own."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def write_state(base: Path, *argv: str, subject=("--vehicle", "a7c1")) -> dict:
    """``vledger l0 state``: the line it says it wrote."""
    out = vledger(base, "l0", "state", *subject, *argv)
    printed = [json.loads(x[x.index("{"):]) for x in out.splitlines() if "{" in x]
    assert len(printed) == 1, out
    return printed[0]


def read_states(base: Path, subject=("--vehicle", "a7c1")) -> list[dict]:
    """The state lines ``vledger l0 read`` prints."""
    lines = [json.loads(x) for x in vledger(base, "l0", "read", *subject).splitlines()
             if x.strip()]
    return [ln for ln in lines if ln.get("kind") == "state"]


def schema_versions(base: Path, subject=("--vehicle", "a7c1")) -> list:
    report = json.loads(vledger(base, "l0", "validate", *subject, "--json"))
    return report["versions"]


def instant(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


@pytest.fixture
def base(tmp_path):
    return tmp_path


# --- the tests ----------------------------------------------------------------

def test_a_state_record_holds_every_item_the_requirement_names(base):
    """One odometer reading, written with everything a source can give: read
    back, its line carries the schema version, the vehicle's id, the time in
    UTC to the millisecond, the role, the entity id, the value as the source
    held it, the source's unit and the source's own measurement time."""
    written = write_state(base, "--role", "odometer", "--entity", "sensor.volvo_odometer",
                          "--state", "76543.21", "--unit", "mi",
                          "--t", "2026-10-09T08:15:30.250Z",
                          "--measured-at", "2026-10-09T08:14:58.000Z")

    [line] = read_states(base)
    assert line == written
    assert schema_versions(base) == [5]
    assert line["v"] == 5
    assert line["subject"] == "a7c1"
    assert UTC_MS.match(line["t"]), line["t"]
    assert instant(line["t"]) == dt.datetime(2026, 10, 9, 8, 15, 30, 250000, dt.UTC)
    assert line["role"] == "odometer"
    assert line["entity"] == "sensor.volvo_odometer"
    assert line["state"] == "76543.21"
    assert line["unit"] == "mi"
    assert instant(line["measured_at"]) == dt.datetime(2026, 10, 9, 8, 14, 58, tzinfo=dt.UTC)


def test_a_charge_point_s_state_record_names_the_charge_point(base):
    """A charge point's meter reading is a state record of the charge point's
    own stream, carrying the charge point's id where a vehicle's carries the
    vehicle's."""
    cp = ("--chargepoint", "c3d9")
    written = write_state(base, "--role", "energy_meter", "--entity", "sensor.wallbox_energy",
                          "--state", "1834.562", "--unit", "kWh",
                          "--t", "2026-10-09T19:02:11.004Z", subject=cp)

    [line] = read_states(base, cp)
    assert line == written
    assert line["v"] in schema_versions(base, cp)
    assert line["subject"] == "c3d9"
    assert (line["role"], line["entity"], line["state"], line["unit"]) == (
        "energy_meter", "sensor.wallbox_energy", "1834.562", "kWh")


@pytest.mark.parametrize("raw", ["0123.40", "1e3", "12 345,6", "not_home",
                                 "unavailable", "unknown", ""])
def test_the_value_is_kept_raw_as_the_source_reported_it(base, raw):
    """The value is the source's string, unchanged: no number made of it, no
    padding or exponent lost, and the states of an entity that is not
    reporting kept as such."""
    write_state(base, "--role", "odometer", "--entity", "sensor.volvo_odometer",
                "--state", raw, "--unit", "km", "--t", "2026-10-09T08:15:30.250Z")

    [line] = read_states(base)
    assert line["state"] == raw
    assert isinstance(line["state"], str)


@pytest.mark.parametrize(("role", "entity", "raw", "unit"), [
    ("odometer", "sensor.car_odometer", "630", "mi"),
    ("fuel_level", "sensor.car_fuel", "11.2", "gal"),
    ("fuel_level", "sensor.car_fuel_pct", "48", "%"),
    ("outside_temperature", "sensor.car_outside", "72", "°F"),
    ("soc", "sensor.car_battery", "81", "%"),
])
def test_the_unit_is_the_source_s_and_the_value_is_not_converted(base, role, entity, raw, unit):
    """A reading in a unit L1 does not use is logged in that unit: the record
    keeps the source unit beside the source's value, and normalising is left
    to the derivation."""
    write_state(base, "--role", role, "--entity", entity, "--state", raw, "--unit", unit,
                "--t", "2026-10-09T08:15:30.250Z")

    [line] = read_states(base)
    assert (line["state"], line["unit"]) == (raw, unit)


def test_the_role_relevant_attributes_are_kept_and_no_others(base):
    """The user guide's own example: a position with latitude, longitude,
    accuracy and the tracker's battery. The record keeps the three that
    place the vehicle and drops the battery."""
    written = write_state(base, "--role", "position", "--entity", "device_tracker.volvo",
                          "--state", "not_home",
                          "--attr", "latitude=48.1371", "--attr", "longitude=11.5754",
                          "--attr", "gps_accuracy=12", "--attr", "battery=80",
                          "--t", "2026-10-09T08:15:30.250Z")

    [line] = read_states(base)
    assert line == written
    kept = {k: v for k, v in line.items() if isinstance(v, dict)}
    assert len(kept) == 1, line
    [attrs] = kept.values()
    assert {k: float(v) for k, v in attrs.items()} == {
        "latitude": 48.1371, "longitude": 11.5754, "gps_accuracy": 12.0}
    assert "battery" not in json.dumps(line)


def test_the_measurement_time_is_kept_where_the_source_gives_one_and_only_there(base):
    """Two readings, one with the source's own measurement time and one
    without: the first record keeps it, distinct from Home Assistant's time,
    and the second has no such time at all."""
    write_state(base, "--role", "odometer", "--entity", "sensor.volvo_odometer",
                "--state", "100", "--unit", "km", "--t", "2026-10-09T08:15:30.250Z",
                "--measured-at", "2026-10-09T07:59:00.125Z")
    write_state(base, "--role", "odometer", "--entity", "sensor.volvo_odometer",
                "--state", "101", "--unit", "km", "--t", "2026-10-09T08:20:30.250Z")

    with_it, without = read_states(base)
    assert instant(with_it["measured_at"]) == dt.datetime(2026, 10, 9, 7, 59, 0, 125000, dt.UTC)
    assert instant(with_it["t"]) != instant(with_it["measured_at"])
    assert "measured_at" not in without
    times = [v for v in without.values() if isinstance(v, str) and re.match(r"^\d{4}-\d\d-\d\dT", v)]
    assert times == [without["t"]], without


def test_a_record_written_now_carries_the_time_in_utc_to_the_millisecond(base):
    """Without ``--t`` the verb writes the time of now, as the integration
    does: UTC, ISO 8601, milliseconds."""
    before = dt.datetime.now(dt.UTC)
    write_state(base, "--role", "odometer", "--entity", "sensor.volvo_odometer",
                "--state", "100", "--unit", "km")
    after = dt.datetime.now(dt.UTC)

    [line] = read_states(base)
    assert UTC_MS.match(line["t"]), line["t"]
    assert before - dt.timedelta(milliseconds=1) <= instant(line["t"]) <= after


@pytest.mark.xfail(strict=True, reason="ISSUE-0125")
@pytest.mark.parametrize(("given", "expected"), [
    ("2026-10-09T08:16:00Z", dt.datetime(2026, 10, 9, 8, 16, tzinfo=dt.UTC)),
    ("2026-10-09T10:15:30.250+02:00", dt.datetime(2026, 10, 9, 8, 15, 30, 250000, dt.UTC)),
])
def test_a_time_given_is_written_in_utc_with_milliseconds(base, given, expected):
    """A time given with ``--t`` is the same instant in the record, spelt as
    the requirement asks: in UTC and to the millisecond, whether it was typed
    without milliseconds or in another zone."""
    write_state(base, "--role", "odometer", "--entity", "sensor.volvo_odometer",
                "--state", "100", "--unit", "km", "--t", given)

    [line] = read_states(base)
    assert instant(line["t"]) == expected
    assert UTC_MS.match(line["t"]), line["t"]
