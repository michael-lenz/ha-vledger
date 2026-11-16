# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0020: the role assignment can be changed at any time without loss of
history.

Derived from the requirement and the user documentation:

- the user guide's *In Home Assistant*: roles are picked in the vehicle's
  second step, and the entry's *Configure* is a menu over the same steps,
  pre-filled; saving reloads the entry, "which the stream records as stop,
  start and the new configuration"; "an entity that is gone for good is
  replaced in the options";
- *Where the data is* and *The raw log*: the stream is read back with
  ``vledger l0 read``, checked with ``vledger l0 validate``;
- *The derivations*: ``vledger derive trips``, and "a gap after the
  standstill has elapsed leaves the completed trip as it was".

What the user can see of "without loss of history": every line the stream
held before the change is still read back after it, unchanged; what was
derived from the time before the change is derived the same after it; and
the role goes on being captured from the entity it now names.

This module does it with the ``vledger`` command alone, writing a stream the
way the integration writes one (the verbs of the user guide's *Writing*),
with the role's entity replaced between two trips and in the middle of one;
test_req_0020_role_reassignment_ha.py does it in Home Assistant. Nothing of
``vledger`` is imported."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.req("REQ-0020")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
VEHICLE = "b3d9"
HOME = ("51.0000", "7.0000")
ROAD = [("51.0300", "7.0200"), ("51.0600", "7.0500"), ("51.0900", "7.0700")]
SHOP = ("51.1000", "7.0800")
OLD, NEW = "sensor.old_odometer", "sensor.new_odometer"


# --- the vledger command -----------------------------------------------------

def vledger(base, *argv, expect=0):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    # A relative PYTHONPATH such as the checks' ``src`` holds wherever the
    # process starts; the tree's own src goes first all the same.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def jsonl(text):
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def at(minutes):
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    h, m = divmod(6 * 60 + minutes, 60)
    return f"2026-10-05T{h:02d}:{m:02d}:00.000Z"


def config(odometer):
    return {"name": "Test car",
            "roles": {"odometer": {"entity": odometer},
                      "position": {"entity": "device_tracker.car"}},
            "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360}}


def start(base, minutes, odometer, km, where, zone):
    """The stream's start as the integration writes it, with the snapshot of
    every role's entity, and the configuration right after it."""
    snapshot = [
        {"role": "odometer", "entity": odometer, "state": str(km), "unit": "km",
         "since": at(minutes - 5)},
        {"role": "position", "entity": "device_tracker.car", "state": zone, "since": at(minutes - 5),
         "attrs": {"latitude": float(where[0]), "longitude": float(where[1]), "gps_accuracy": 0}},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(minutes),
            "--homeassistant", "2026.10.1", "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(minutes),
            "--config", json.dumps(config(odometer)))


def reassign(base, minutes, odometer, km, where, zone):
    """The odometer role given to another entity, as saving the options
    records it: stop, start and the new configuration."""
    vledger(base, "l0", "stop", "--vehicle", VEHICLE, "--t", at(minutes), "--reason", "reload")
    start(base, minutes, odometer, km, where, zone)


def drive(base, minutes, odometer, km, where, zone="not_home"):
    vledger(base, "l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", "odometer",
            "--entity", odometer, "--state", str(km), "--unit", "km")
    vledger(base, "l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", "position",
            "--entity", "device_tracker.car", "--state", zone,
            "--attr", f"latitude={where[0]}", "--attr", f"longitude={where[1]}",
            "--attr", "gps_accuracy=0")


def read(base, *argv):
    return jsonl(vledger(base, "l0", "read", "--vehicle", VEHICLE, *argv))


def trips(base):
    return jsonl(vledger(base, "derive", "trips", "--vehicle", VEHICLE))


def to_the_shop(base):
    """A drive on the old odometer, and the car parked at the shop."""
    start(base, -60, OLD, 1000, HOME, "home")
    drive(base, 15, OLD, 1007, ROAD[0])
    drive(base, 30, OLD, 1015, ROAD[1])
    drive(base, 45, OLD, 1022, SHOP)


def home_again(base, odometer):
    """The drive back, after more than T_still parked."""
    drive(base, 115, odometer, 1029, ROAD[2])
    drive(base, 130, odometer, 1037, ROAD[1])
    drive(base, 145, odometer, 1044, HOME, zone="home")
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(300), "--lines", "0")


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "vledger"
    b.mkdir()
    return b


def test_the_lines_before_a_reassignment_are_all_read_back_after_it(base):
    """Replacing the odometer's entity takes nothing away from the stream:
    what ``l0 read`` printed before the change it still prints, line for
    line, followed by the stop, the start, the new configuration and what
    the new entity reported."""
    to_the_shop(base)
    before = read(base)

    reassign(base, 100, NEW, 1022, SHOP, "not_home")
    home_again(base, NEW)
    after = read(base)

    assert after[:len(before)] == before
    kinds = [ln["kind"] for ln in after[len(before):]]
    assert kinds[:3] == ["stop", "start", "config"], kinds
    vledger(base, "l0", "validate", "--vehicle", VEHICLE)  # exit code 0: no error


def test_a_role_keeps_its_whole_history_across_entities(base):
    """The role's history is one, whichever entity reported it: reading the
    odometer role gives the old entity's readings and then the new one's, in
    order, and the latest configuration names the new entity."""
    to_the_shop(base)
    reassign(base, 100, NEW, 1022, SHOP, "not_home")
    home_again(base, NEW)

    odometer = [(ln["entity"], ln["state"])
                for ln in read(base, "--kind", "state", "--role", "odometer")]
    assert odometer == [(OLD, "1007"), (OLD, "1015"), (OLD, "1022"),
                        (NEW, "1029"), (NEW, "1037"), (NEW, "1044")]
    configs = [ln for ln in read(base) if ln["kind"] == "config"]
    assert [c["config"]["roles"]["odometer"]["entity"] for c in configs] == [OLD, NEW]


def test_what_was_derived_before_a_reassignment_is_derived_the_same_after_it(base):
    """A trip completed before the change is the same trip after it, and the
    drive after it is a trip of its own: two trips, the first exactly as it
    was derived before the change."""
    to_the_shop(base)
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(95), "--lines", "0")
    [first] = trips(base)
    assert first["distance_km"], first

    reassign(base, 100, NEW, 1022, SHOP, "not_home")
    home_again(base, NEW)

    derived = trips(base)
    assert len(derived) == 2, derived
    assert derived[0] == first
    assert derived[1]["start"] > at(100) and derived[1]["distance_km"], derived[1]


def test_a_role_can_be_reassigned_in_the_middle_of_a_drive(base):
    """At any time means while driving too: the readings of the old entity
    up to the change and of the new one after it are all in the stream, and
    the stream is one the writer could have written."""
    start(base, -60, OLD, 1000, HOME, "home")
    drive(base, 15, OLD, 1007, ROAD[0])
    drive(base, 30, OLD, 1015, ROAD[1])
    reassign(base, 32, NEW, 1016, ROAD[1], "not_home")
    drive(base, 45, NEW, 1022, SHOP)
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(120), "--lines", "0")

    odometer = [(ln["entity"], ln["state"])
                for ln in read(base, "--kind", "state", "--role", "odometer")]
    assert odometer == [(OLD, "1007"), (OLD, "1015"), (NEW, "1022")]
    vledger(base, "l0", "validate", "--vehicle", VEHICLE)  # exit code 0: no error
