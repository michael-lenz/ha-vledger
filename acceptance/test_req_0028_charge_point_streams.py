# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0028: the energy meter of a charge point is logged in a stream of its
own per charge point, not in a vehicle's stream.

Derived from the requirement, README.md ("one stream per vehicle and per
charge point") and the user guide's *Where the data is* — a stream is named by
what it belongs to, ``--vehicle ID`` or ``--chargepoint ID``; that id is the
name of the subject's directory under the data directory without its prefix,
``vehicle-<id>`` or ``chargepoint-<id>`` — and *The raw log: vledger l0*: the
verbs the integration writes a stream with, "usable by hand to build one",
``l0 state`` printing the line it wrote and ``l0 read`` the stream back.

The meter is written as the integration would write a wallbox's energy
meter, and read back by the user; the stream as Home Assistant's capture
writes it is the other module, test_req_0028_charge_point_streams_ha.py.
Every test drives the ``vledger`` command as a program; nothing of
``vledger`` is imported."""

import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0028")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

HOME, WORK, CAR = ("--chargepoint", "c3d9"), ("--chargepoint", "e81f"), ("--vehicle", "a7c1")


def vledger(base: Path, *argv: str) -> str:
    """``vledger <argv> --base <base>`` in a process of its own."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def meter_reading(base, subject, entity, kwh, t):
    vledger(base, "l0", "state", *subject, "--role", "energy_meter", "--entity", entity,
            "--state", kwh, "--unit", "kWh", "--t", t)


def odometer(base, km, t):
    vledger(base, "l0", "state", *CAR, "--role", "odometer", "--entity", "sensor.car_odometer",
            "--state", km, "--unit", "km", "--t", t)


def read(base, subject):
    return [json.loads(x) for x in vledger(base, "l0", "read", *subject).splitlines()
            if x.strip()]


def entities(lines):
    return {ln.get("entity") for ln in lines if ln["kind"] == "state"}


@pytest.fixture
def base(tmp_path):
    d = tmp_path / "vledger"
    d.mkdir()
    return d


def test_a_meter_reading_goes_to_the_charge_point_s_directory_and_no_vehicle_s(base):
    """A charge point's meter readings, written beside a vehicle's odometer,
    land under ``chargepoint-<id>`` and read back with ``--chargepoint``;
    the vehicle's directory and stream hold none of them."""
    odometer(base, "12345", "2026-10-09T17:58:00.000Z")
    meter_reading(base, HOME, "sensor.home_wallbox_energy", "1520.4", "2026-10-09T18:00:00.000Z")
    odometer(base, "12346", "2026-10-09T18:01:00.000Z")
    meter_reading(base, HOME, "sensor.home_wallbox_energy", "1523.1", "2026-10-09T18:30:00.000Z")

    assert sorted(p.name for p in base.iterdir()) == ["chargepoint-c3d9", "vehicle-a7c1"]
    home = read(base, HOME)
    assert [ln["state"] for ln in home if ln["kind"] == "state"] == ["1520.4", "1523.1"]
    assert entities(home) == {"sensor.home_wallbox_energy"}
    assert entities(read(base, CAR)) == {"sensor.car_odometer"}
    assert not any("wallbox" in str(p) for p in (base / "vehicle-a7c1").rglob("*"))
    assert not any("wallbox" in p.read_text() for p in (base / "vehicle-a7c1").rglob("*")
                   if p.is_file())


def test_each_charge_point_s_meter_is_a_stream_of_its_own(base):
    """Two charge points' meters, written in turn: two directories, and each
    stream holds its own meter's readings, in order, and not the other's."""
    for i, (home_kwh, work_kwh) in enumerate([("1520.4", "88.0"), ("1525.0", "96.5"),
                                               ("1531.5", "101.25")]):
        meter_reading(base, HOME, "sensor.home_wallbox_energy", home_kwh,
                      f"2026-10-09T18:{i:02d}:00.000Z")
        meter_reading(base, WORK, "sensor.work_wallbox_energy", work_kwh,
                      f"2026-10-09T18:{i:02d}:30.000Z")

    assert sorted(p.name for p in base.iterdir()) == ["chargepoint-c3d9", "chargepoint-e81f"]
    home, work = read(base, HOME), read(base, WORK)
    assert [ln["state"] for ln in home if ln["kind"] == "state"] == ["1520.4", "1525.0", "1531.5"]
    assert [ln["state"] for ln in work if ln["kind"] == "state"] == ["88.0", "96.5", "101.25"]
    assert entities(home) == {"sensor.home_wallbox_energy"}
    assert entities(work) == {"sensor.work_wallbox_energy"}
