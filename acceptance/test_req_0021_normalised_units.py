# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0021, as a user meets it: L1 uses normalised units — km, L, kWh, %,
°C, UTC — and the conversion starts from the source unit stored in L0.

Black-box: every step is the ``vledger`` CLI run as a separate process, the
way the user guide shows it — a stream written with ``vledger l0``, L1 built
with ``vledger derive … --write`` and read with ``vledger l1 read``. Nothing
here imports the library.

What the user can see of it:

- a raw log keeps each value in the unit its entity reported (``--unit``,
  "unit_of_measurement", as Home Assistant spells it), unconverted;
- an odometer in miles, a fuel level in gallons or in per cent and an outside
  temperature in degrees Fahrenheit give the same L1 as the same drive
  reported in km, L and °C — L1 never carries the source's unit;
- the keys L1 names after a unit hold that unit: ``distance_km`` in km,
  ``level_before_l`` in litres, ``battery_kwh`` in kWh, ``delta_soc_pct`` in
  per cent;
- every time in L1 is UTC, whatever the time zone the CLI runs under.
"""

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0021")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "b3d9"
MI = 1.609344          # km per international mile
GAL = 3.785411784      # L per US gallon, Home Assistant's "gal"
HOME = ("51.0000", "7.0000")
ROAD = [("51.0300", "7.0200"), ("51.0600", "7.0500"), ("51.0900", "7.0700")]
STATION = ("51.1000", "7.0800")


def at(minutes: int) -> str:
    """A time of the scenario: minutes after 06:00 UTC on 5 October 2026."""
    t = datetime(2026, 10, 5, 6, 0) + timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def vledger(base: Path, *argv: str, env: dict | None = None, expect: int = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ.update(env or {})
    # The process runs in the data directory: a relative PYTHONPATH such as
    # the checks' ``src`` would no longer find vledger there.
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def num(x: float) -> str:
    """A number as a sensor's state string."""
    return repr(round(x, 6))


class Units:
    """How one vehicle's entities report: the unit of each role, and the
    conversion from the metric value of the scenario into that unit."""

    def __init__(self, distance: str, volume: str, temperature: str, tank_l: float | None = None):
        self.distance, self.volume, self.temperature, self.tank_l = distance, volume, temperature, tank_l

    def odometer(self, km: float) -> str:
        return num(km / MI if self.distance == "mi" else km)

    def fuel(self, litres: float) -> str:
        if self.volume == "%":
            return num(litres / self.tank_l * 100)
        return num(litres / GAL if self.volume == "gal" else litres)

    def temp(self, celsius: float) -> str:
        return num(celsius * 9 / 5 + 32 if self.temperature == "°F" else celsius)


METRIC = Units("km", "L", "°C")
US = Units("mi", "gal", "°F")
PERCENT = Units("km", "%", "°C", tank_l=50.0)


def config(units: Units) -> dict:
    parameters = {"fuel": "petrol", "battery_net_kwh": 10.0}
    if units.tank_l:
        parameters["tank_capacity_l"] = units.tank_l
    return {
        "name": "Test car",
        "roles": {
            "odometer": {"entity": "sensor.odometer"},
            "position": {"entity": "sensor.position"},
            "fuel_level": {"entity": "sensor.fuel_level"},
            "soc": {"entity": "sensor.soc"},
            "outside_temperature": {"entity": "sensor.outside_temperature"},
            "ignition": {"entity": "sensor.ignition", "map": {"on": ["on"]}},
        },
        "parameters": parameters,
        "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
    }


def write_stream(base: Path, units: Units) -> None:
    """A morning as the integration would have logged it, in the units the
    entities report: a drive of 22 km to a fuel station, a fill from 18.9 L
    to 60.4 L, the drive home, and nearly three hours of quiet. Outside it
    warms from 10 °C to 20 °C; the battery runs from 80 % to 60 %."""

    def state(minutes: int, role: str, value: str, unit: str | None = None, **attrs: str) -> None:
        argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(minutes), "--role", role,
                "--entity", f"sensor.{role}", "--state", value]
        if unit:
            argv += ["--unit", unit]
        for k, v in attrs.items():
            argv += ["--attr", f"{k}={v}"]
        vledger(base, *argv)

    def odo(minutes: int, km: float) -> None:
        state(minutes, "odometer", units.odometer(km), units.distance)

    def fuel(minutes: int, litres: float) -> None:
        state(minutes, "fuel_level", units.fuel(litres), units.volume)

    def temp(minutes: int, celsius: float) -> None:
        state(minutes, "outside_temperature", units.temp(celsius), units.temperature)

    def soc(minutes: int, pct: float) -> None:
        state(minutes, "soc", num(pct), "%")

    def fix(minutes: int, where: tuple[str, str], zone: str = "not_home") -> None:
        state(minutes, "position", zone, latitude=where[0], longitude=where[1], gps_accuracy="0")

    snapshot = [
        {"role": "odometer", "entity": "sensor.odometer", "state": units.odometer(1000),
         "unit": units.distance, "since": at(-120)},
        {"role": "position", "entity": "sensor.position", "state": "home", "since": at(-120),
         "attrs": {"latitude": float(HOME[0]), "longitude": float(HOME[1]), "gps_accuracy": 0}},
        {"role": "fuel_level", "entity": "sensor.fuel_level", "state": units.fuel(20.5),
         "unit": units.volume, "since": at(-120)},
        {"role": "soc", "entity": "sensor.soc", "state": "80", "unit": "%", "since": at(-120)},
        {"role": "outside_temperature", "entity": "sensor.outside_temperature",
         "state": units.temp(10), "unit": units.temperature, "since": at(-120)},
    ]
    vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(-60), "--homeassistant", "2026.9.4",
            "--snapshot", json.dumps(snapshot))
    vledger(base, "l0", "config", "--vehicle", VEHICLE, "--t", at(-60), "--config", json.dumps(config(units)))
    # To the station.
    state(0, "ignition", "on")
    odo(15, 1007), fix(15, ROAD[0]), temp(15, 12)
    odo(30, 1015), fix(30, ROAD[1]), soc(30, 70)
    odo(45, 1022), fix(45, STATION, zone="fuel_station"), temp(45, 15)
    fuel(46, 18.9)
    state(47, "ignition", "off")
    # The fill, seen in two steps, settling a little higher.
    fuel(52, 40.0)
    fuel(54, 60.0)
    fuel(58, 60.4)
    # Home, after the stop.
    state(100, "ignition", "on")
    odo(115, 1037), fix(115, ROAD[2]), temp(115, 18)
    odo(130, 1044), fix(130, HOME, zone="home"), soc(130, 60), temp(130, 20)
    fuel(131, 58.1)
    state(132, "ignition", "off")
    vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(300), "--lines", "0")


def read(base: Path, kind: str, env: dict | None = None) -> list[dict]:
    p = vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind, env=env)
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def l1(base: Path, env: dict | None = None) -> dict[str, list[dict]]:
    """L1 rebuilt from the raw log, kind by kind, as `l1 read` prints it."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write", env=env)
    return {kind: read(base, kind, env) for kind in ("trip", "refuelling", "charging", "period")}


def scenario(tmp_path: Path, name: str, units: Units) -> Path:
    base = tmp_path / name
    base.mkdir()
    write_stream(base, units)
    return base


def same(a, b, path: str = "") -> list[str]:
    """Where two JSON values differ, numbers compared to a millionth of their
    size (a conversion there and back may lose a last bit)."""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return [] if a == b else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return [] if math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return [f"{path}: keys {sorted(a.keys() ^ b.keys())}"]
        return [d for k in a for d in same(a[k], b[k], f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items != {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b, strict=True)) for d in same(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def test_the_raw_log_keeps_each_value_in_the_unit_its_entity_reported(tmp_path):
    """L0 stores the source's unit beside the value, unconverted: miles stay
    miles, gallons gallons, °F °F — it is where the conversion starts."""
    base = scenario(tmp_path, "us", US)
    lines = [json.loads(x) for x in vledger(base, "l0", "read", "--vehicle", VEHICLE, "--kind", "state")
             .stdout.splitlines() if x.strip()]
    seen = {(x["role"], x["state"], x.get("unit")) for x in lines}
    assert ("odometer", US.odometer(1022), "mi") in seen, sorted(seen)
    assert ("fuel_level", US.fuel(60.4), "gal") in seen, sorted(seen)
    assert ("outside_temperature", US.temp(20), "°F") in seen, sorted(seen)
    assert ("soc", "60.0", "%") in seen or ("soc", "60", "%") in seen, sorted(seen)


def test_a_drive_reported_in_us_units_gives_the_same_l1_as_in_metric_units(tmp_path):
    """The same morning, once reported in km, L and °C and once in mi, gal
    and °F, gives the same trips, refuelling and periods: L1 holds the
    normalised value, never the source's unit."""
    metric = l1(scenario(tmp_path, "metric", METRIC))
    us = l1(scenario(tmp_path, "us", US))
    assert len(metric["trip"]) == 2 and len(metric["refuelling"]) == 1, metric
    assert same(metric, us) == []


def test_a_fuel_level_in_percent_gives_the_same_l1_as_in_litres(tmp_path):
    """A fuel level in % is converted with the tank capacity: 50 L, and the
    level reported as a share of it, gives the L1 of the level in litres."""
    metric = l1(scenario(tmp_path, "metric", METRIC))
    percent = l1(scenario(tmp_path, "percent", PERCENT))
    # The tank capacity is a parameter of the percent vehicle alone; what it
    # derives is the same.
    assert same(metric["trip"], percent["trip"]) == []
    assert same(metric["refuelling"], percent["refuelling"]) == []


def test_the_keys_named_after_a_unit_hold_that_unit(tmp_path):
    """Read from a vehicle reporting miles, gallons and °F: the trips'
    distance is in km, the fill's levels in litres, the battery's change in %
    and its energy in kWh (ΔSoC × the 10 kWh net capacity), the outside
    temperature in °C."""
    l1_ = l1(scenario(tmp_path, "us", US))
    out, home = l1_["trip"]
    assert out["distance_km"] == pytest.approx(22, abs=0.01)
    assert home["distance_km"] == pytest.approx(22, abs=0.01)
    [fill] = l1_["refuelling"]
    assert fill["level_before_l"] == pytest.approx(18.9, abs=0.01)
    assert fill["level_after_l"] == pytest.approx(60.4, abs=0.01)
    assert fill["sensor_delta_l"] == pytest.approx(41.5, abs=0.01)
    deltas = [t["delta_soc_pct"] for t in (out, home)]
    assert sum(deltas) == pytest.approx(-20, abs=0.01), deltas
    energies = [t["battery_consumed_kwh"] for t in (out, home)]
    assert sum(energies) == pytest.approx(2.0, abs=0.01), energies
    # The mean outside temperature, under the key whose name says °C: the
    # drive out ran between 10 °C and 15 °C, reported as 50 °F to 59 °F.
    celsius = {k: v for k, v in out.items() if k.endswith("_c") and isinstance(v, (int, float))}
    assert celsius and all(10 <= v <= 15 for v in celsius.values()), celsius


def test_every_time_in_l1_is_utc_whatever_the_time_zone(tmp_path):
    """Built and read under a time zone far from UTC, every event's start and
    end, and a refuelling's settled_at, are UTC times: the trip out starts
    no later than its first moving sample at 06:15 UTC, and a time's offset
    is zero."""
    env = {"TZ": "Pacific/Kiritimati"}
    l1_ = l1(scenario(tmp_path, "us", US), env=env)
    times = []
    for kind in ("trip", "refuelling", "charging", "period"):
        for event in l1_[kind]:
            times += [(kind, k, event[k]) for k in ("start", "end", "settled_at") if event.get(k)]
    assert times
    for kind, key, value in times:
        t = datetime.fromisoformat(value)
        assert t.utcoffset() == timedelta(0), (kind, key, value)
    out = l1_["trip"][0]
    start = datetime.fromisoformat(out["start"])
    assert datetime.fromisoformat(at(0)) <= start \
        <= datetime.fromisoformat(at(15)), out["start"]
