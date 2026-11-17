# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0003: the library has no dependency on Home Assistant and can be
installed and run on its own.

Shown as a user meets the product: the installed ``vledger`` distribution
asks pip for nothing beyond itself, and the ``vledger`` command runs the
whole loop the user guide walks — a raw log written verb by verb, a receipt,
the derivation written to disk and read back, an export and a report — in an
interpreter that sees nothing but the standard library and the library
itself. Nothing in that run imports Home Assistant, nor any other module
from outside the standard library."""

import importlib.metadata
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0003")]

# Where the installed library lies, found without importing it: the test
# imports nothing of the library, and runs it only as the vledger command.
PACKAGE = Path(importlib.util.find_spec("vledger").origin).resolve().parent
VERSION = importlib.metadata.version("vledger")

# Runs the vledger command in an interpreter started with -I -S: no
# site-packages, no user site, no PYTHONPATH — only the standard library and
# the directory that holds the library. After the verb it reports every
# module the run loaded from a file outside the standard library's
# directories, so the test sees what the library needed beyond them.
RUNNER = """
import json, os, sys, sysconfig
sys.path.insert(0, sys.argv[1])
from vledger.cli import main
try:
    code = main(sys.argv[3:])
except SystemExit as e:
    code = e.code
homes = {os.path.realpath(sysconfig.get_path(k)) for k in ("stdlib", "platstdlib")}
homes.add(os.path.realpath(os.path.join(sys.argv[1], "vledger")))
foreign = sorted(
    name for name, module in list(sys.modules.items())
    if name.split(".")[0] == "homeassistant"
    or (getattr(module, "__file__", None)
        and not any(os.path.realpath(module.__file__).startswith(h + os.sep) for h in homes))
)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(foreign, f)
sys.exit(code or 0)
"""


def vledger_alone(tmp_path: Path, *argv: str) -> str:
    """Runs ``vledger <argv>`` with nothing but the standard library on the
    path; asserts success and that only the standard library and vledger
    were imported; returns what it printed."""
    modules = tmp_path / "modules.json"
    run = subprocess.run(
        [sys.executable, "-I", "-S", "-c", RUNNER, str(PACKAGE.parent), str(modules), *argv],
        check=False, capture_output=True, text=True, cwd=tmp_path, timeout=120,
    )
    assert run.returncode == 0, (argv, run.stdout, run.stderr)
    assert json.loads(modules.read_text()) == [], argv
    return run.stdout


def test_the_installed_library_asks_for_nothing_but_itself():
    # Every requirement the distribution declares belongs to an extra (test,
    # dev, ha): `pip install vledger` installs the library alone.
    requires = importlib.metadata.requires("vledger") or []
    unconditional = [r for r in requires if "extra ==" not in r]
    assert unconditional == []


def test_the_installed_command_is_the_installed_distribution():
    run = subprocess.run([sys.executable, "-m", "vledger.cli", "--version"],
                         check=True, capture_output=True, text=True, timeout=60)
    assert run.stdout.strip().endswith(VERSION)


def test_the_command_runs_the_whole_loop_with_nothing_but_the_standard_library(tmp_path):
    base = ["--base", str(tmp_path / "data")]
    v = [*base, "--vehicle", "a7c1"]
    assert vledger_alone(tmp_path, "--version").strip().endswith(VERSION)

    snapshot = [
        {"role": "odometer", "entity": "sensor.o", "state": "1000", "unit": "km", "since": "2026-10-08T06:00:00Z"},
        {"role": "position", "entity": "device_tracker.v", "state": "home", "since": "2026-10-08T06:00:00Z",
         "attrs": {"latitude": 51.0, "longitude": 7.0, "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.f", "state": "40", "unit": "L", "since": "2026-10-08T06:00:00Z"},
    ]
    config = {"name": "V",
              "roles": {"odometer": {"entity": "sensor.o"}, "position": {"entity": "device_tracker.v"},
                        "fuel_level": {"entity": "sensor.f"},
                        "ignition": {"entity": "binary_sensor.i", "map": {"on": ["on"]}}},
              "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360}}
    vledger_alone(tmp_path, "l0", "start", *v, "--t", "2026-10-08T07:00:00Z",
                  "--homeassistant", "2026.10.1", "--snapshot", json.dumps(snapshot))
    vledger_alone(tmp_path, "l0", "config", *v, "--t", "2026-10-08T07:00:00Z", "--config", json.dumps(config))

    def state(t, role, value, unit=None, **attrs):
        args = ["l0", "state", *v, "--t", t, "--role", role, "--entity", f"x.{role}", "--state", value]
        if unit:
            args += ["--unit", unit]
        for k, a in attrs.items():
            args += ["--attr", f"{k}={a}"]
        vledger_alone(tmp_path, *args)

    vledger_alone(tmp_path, "l0", "heartbeat", *v, "--t", "2026-10-08T07:30:00Z", "--lines", "0")
    state("2026-10-08T08:00:00Z", "ignition", "on")
    state("2026-10-08T08:15:00Z", "odometer", "1007", "km")
    state("2026-10-08T08:15:00Z", "position", "not_home", latitude=51.03, longitude=7.02, gps_accuracy=0)
    state("2026-10-08T08:30:00Z", "odometer", "1015", "km")
    state("2026-10-08T08:30:00Z", "position", "not_home", latitude=51.06, longitude=7.05, gps_accuracy=0)
    state("2026-10-08T08:45:00Z", "odometer", "1022", "km")
    state("2026-10-08T08:45:00Z", "position", "not_home", latitude=51.09, longitude=7.07, gps_accuracy=0)
    state("2026-10-08T08:47:00Z", "ignition", "off")
    state("2026-10-08T08:48:00Z", "fuel_level", "38.4", "L")
    vledger_alone(tmp_path, "l0", "heartbeat", *v, "--t", "2026-10-08T10:00:00Z", "--lines", "0")

    assert json.loads(vledger_alone(tmp_path, "l0", "validate", *v, "--json"))["problems"] == []

    vledger_alone(tmp_path, "receipt", "add", "refuelling", *v, "--anchor", "2026-10-08T09:30:00Z",
                  "--quantity-l", "40", "--unit-price", "1.80", "--full")
    vledger_alone(tmp_path, "derive", "all", *v, "--write")

    trips = [json.loads(line) for line in vledger_alone(tmp_path, "l1", "read", *v, "--kind", "trip").splitlines()]
    assert len(trips) == 1
    assert trips[0]["distance_km"] == 22
    # compared by the instant, not the spelling, which is REQ-0024's
    assert datetime.fromisoformat(trips[0]["start"]) == datetime(2026, 10, 8, 8, 0, tzinfo=UTC)

    csv = vledger_alone(tmp_path, "export", "csv", *v, "--kind", "trip").splitlines()
    assert len(csv) == 2                                   # a header and the one trip
    assert "22" in csv[1]
    report = json.loads(vledger_alone(tmp_path, "report", "metrics", *v, "--json"))
    assert report
    assert vledger_alone(tmp_path, "calc", "convert", "1", "km", "--quantity", "distance").strip() == "1 km"
