# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0045, as a user meets it: every derived value carries a quality
flag — ``measured``, ``receipt``, ``estimated`` or ``incomplete``.

Derived from the requirement and the user documentation: README.md ("Every
derived value carries a quality flag: `measured`, `receipt`, `estimated` or
`incomplete`"), the user guide's *The derivations: vledger derive* (a
trip's distance and consumption with their quality; a trip or session
across a capture gap is ``incomplete``; a fill whose settle time spans a
gap is ``incomplete``; ``charge_point_power`` and ``loss_factor`` energy
estimated; a session's cost "with the energy's quality"; a foreign charge
"has no cost until a receipt gives one"), *Periods* ("Every value has its
quality beside it"), *Receipts* (a receipt that met nothing "stands in L1
as an event of its own … whose `quality` is `receipt`"), and the format
document it hands L1 over to, l1-format.md, for the key names: every
event's envelope ``quality`` of the four; the receipt's values "flagged
`receipt`" (``quantity_quality``, ``price_quality``, ``grid_kwh_quality``,
``grid_kwh_source``, ``cost_quality``); every period metric "comes with
`<key>_quality` … `null` beside a `null` value", the envelope's ``quality``
"the weakest of the line's" in the order ``receipt``, ``measured``,
``estimated``, ``incomplete``, and ``electric_distance_share`` "always
`estimated`".

The inputs are a day of a plug-in hybrid with a charge point at home — two
drives and a refuelling, a charge at home and one elsewhere — written with
``vledger l0`` as the user guide shows it, derived once without and once
with receipts; and a second day whose drive, fill and charge each cross a
capture gap, an orderly ``l0 stop`` and the next ``l0 start``
(l0-format.md, *Gaps*).

Every step is the ``vledger`` command run as a program; nothing of
``vledger`` is imported."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0045")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
CHARGEPOINT = "cp01"
DAY = datetime(2026, 10, 5, tzinfo=UTC)

HOME = (51.0000, 7.0000)        # the charge point
STATION = (51.1000, 7.0800)     # a fuel station
AWAY = (51.3000, 7.4000)        # a charge at no configured charge point

FLAGS = ("receipt", "measured", "estimated", "incomplete")   # strongest first

VEHICLE_CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "position": {"entity": "device_tracker.car"},
        "fuel_level": {"entity": "sensor.fuel_level"},
        "fuel_price": {"entity": "sensor.fuel_price"},
        "outside_temperature": {"entity": "sensor.outside_temperature"},
        "soc": {"entity": "sensor.soc"},
        "charging_state": {"entity": "sensor.charging_state", "map": {"charging": ["charging"]}},
    },
    "parameters": {"fuel": "petrol", "battery_net_kwh": 10.0, "tank_capacity_l": 50,
                   "fuel_level_resolution_l": 1.0},
    "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
    "time_zone": "Europe/Berlin",
}

CHARGEPOINT_CONFIG = {
    "name": "Home",
    "latitude": HOME[0],
    "longitude": HOME[1],
    "radius_m": 100,
    "meter": None,
    "power_kw": 3.7,
    "tariffs": [{"from": "2026-01-01", "eur_per_kwh": 0.30}],
}

KINDS = ("trip", "refuelling", "charging", "period")
DERIVE = {"trip": "trips", "refuelling": "refuellings", "charging": "charging", "period": "periods"}

# The metrics of a period line (l1-format.md, *Periods*, "What a period
# holds"), each of which "comes with `<key>_quality`".
METRICS = ("distance_km", "fuel_purchased_l", "fuel_cost_eur", "fuel_consumed_l", "grid_kwh",
           "electricity_cost_eur", "battery_kwh", "grid_kwh_per_100km", "battery_kwh_per_100km",
           "fuel_eur_per_100km", "electricity_eur_per_100km", "eur_per_100km",
           "electric_energy_share", "electric_distance_share", "charge_cycles", "tank_fills")


def at(hours: float) -> str:
    """A time of the scenario: hours after midnight UTC on 5 October 2026."""
    return (DAY + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def vledger(base: Path, *argv: str, expect: int = 0) -> subprocess.CompletedProcess:
    """``vledger <argv>`` in its own process, on the data directory ``base``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=environ, cwd=base)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p


def jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def fix(where) -> dict:
    return {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}


def scenario() -> list[tuple]:
    """The day, as (hour, role, state, unit, attrs) in time order, ``role``
    None for a heartbeat of both streams every half hour:

    - 01:00 at home, 17 L in the tank, 50 % charged, 9 °C outside;
    - 03:00–04:00 a drive to a fuel station, the fill at 05:00 from 15 to 45 L;
    - 07:00–08:00 home again, charging there from 09:00 to 11:00, 40 → 70 %;
    - 13:00–14:00 a drive elsewhere, charging there from 15:00 to 16:00,
      50 → 90 %, at no configured charge point;
    - heartbeats until 20:00."""
    lines = [
        (1, "odometer", "1000", "km", {}), (1, "position", "home", None, fix(HOME)),
        (1, "fuel_level", "17.0", "L", {}), (1, "fuel_price", "1.859", "EUR/L", {}),
        (1, "outside_temperature", "9.0", "°C", {}),
        (1, "soc", "50", "%", {}), (1, "charging_state", "idle", None, {}),
        # To the fuel station.
        (3, "odometer", "1010", "km", {}), (3, "position", "not_home", None, fix((51.05, 7.04))),
        (3.5, "outside_temperature", "11.5", "°C", {}),
        (4, "odometer", "1020", "km", {}), (4, "position", "not_home", None, fix(STATION)),
        (4.1, "fuel_level", "15.0", "L", {}), (4.1, "soc", "40", "%", {}),
        (4.5, "fuel_price", "1.899", "EUR/L", {}),
        (5, "fuel_level", "45.0", "L", {}),
        (5.5, "fuel_level", "45.0", "L", {}),
        # Home.
        (7, "odometer", "1030", "km", {}), (7, "position", "not_home", None, fix((51.05, 7.04))),
        (8, "odometer", "1040", "km", {}), (8, "position", "home", None, fix(HOME)),
        (8.1, "fuel_level", "44.0", "L", {}),
        (9, "charging_state", "charging", None, {}), (9.1, "position", "home", None, fix(HOME)),
        (10, "soc", "55", "%", {}),
        (11, "soc", "70", "%", {}), (11, "charging_state", "idle", None, {}),
        # Elsewhere.
        (12, "outside_temperature", "14.0", "°C", {}),
        (13, "odometer", "1060", "km", {}), (13, "position", "not_home", None, fix((51.2, 7.2))),
        (14, "odometer", "1085", "km", {}), (14, "position", "not_home", None, fix(AWAY)),
        (14.1, "soc", "50", "%", {}), (14.1, "fuel_level", "42.0", "L", {}),
        (15, "charging_state", "charging", None, {}), (15.1, "position", "not_home", None, fix(AWAY)),
        (15.5, "soc", "70", "%", {}),
        (16, "soc", "90", "%", {}), (16, "charging_state", "idle", None, {}),
    ]
    lines += [(h / 2, None, None, None, None) for h in range(1, 41)]
    return sorted(lines, key=lambda x: (x[0], x[1] is None))


def gapped() -> list[tuple]:
    """A day at home whose drive, fill and charge each cross a capture gap,
    ``role`` "stop" or "start" for the vehicle's markers:

    - a drive from 03:00 to 04:00, capture off from 03:20 to 03:40;
    - a fill at 06:00 from 15 to 45 L, capture off from 06:02 to 06:30,
      inside its settle time of 6 minutes;
    - charging at home from 09:00 to 11:00, 40 → 70 %, capture off from
      09:30 to 10:30, longer than T_still;
    - heartbeats until 16:00."""
    lines = [
        (1, "odometer", "1000", "km", {}), (1, "position", "home", None, fix(HOME)),
        (1, "fuel_level", "17.0", "L", {}), (1, "fuel_price", "1.859", "EUR/L", {}),
        (1, "soc", "50", "%", {}), (1, "charging_state", "idle", None, {}),
        # A drive with a gap in it.
        (3, "odometer", "1010", "km", {}), (3, "position", "not_home", None, fix((51.05, 7.04))),
        (3 + 20 / 60, "stop", None, None, None), (3 + 40 / 60, "start", None, None, None),
        (3.75, "odometer", "1025", "km", {}),
        (4, "odometer", "1030", "km", {}), (4, "position", "not_home", None, fix(STATION)),
        (4.5, "fuel_level", "15.0", "L", {}), (4.5, "soc", "40", "%", {}),
        # A fill whose settle time spans a gap.
        (6, "fuel_level", "45.0", "L", {}),
        (6 + 2 / 60, "stop", None, None, None), (6.5, "start", None, None, None),
        (6.6, "fuel_level", "45.0", "L", {}),
        # Home, and a charge with a gap in it.
        (7, "odometer", "1040", "km", {}), (7, "position", "not_home", None, fix((51.05, 7.04))),
        (8, "odometer", "1050", "km", {}), (8, "position", "home", None, fix(HOME)),
        (9, "charging_state", "charging", None, {}), (9.1, "position", "home", None, fix(HOME)),
        (9.25, "soc", "45", "%", {}),
        (9.5, "stop", None, None, None), (10.5, "start", None, None, None),
        (10.6, "soc", "60", "%", {}),
        (11, "soc", "70", "%", {}), (11, "charging_state", "idle", None, {}),
    ]
    lines += [(h / 2, None, None, None, None) for h in range(1, 33)
              if not (3.3 < h / 2 < 3.7 or 6 < h / 2 < 6.5 or 9.5 <= h / 2 < 10.5)]
    return sorted(lines, key=lambda x: (x[0], x[1] is not None and x[1] != "start"))


def begin(base: Path) -> None:
    """Both streams' start and configuration, as the integration writes them."""
    for flag, subject, config in (("--chargepoint", CHARGEPOINT, CHARGEPOINT_CONFIG),
                                  ("--vehicle", VEHICLE, VEHICLE_CONFIG)):
        vledger(base, "l0", "start", flag, subject, "--t", at(0), "--homeassistant", "2026.9.4",
                "--snapshot", "[]")
        vledger(base, "l0", "config", flag, subject, "--t", at(0), "--config", json.dumps(config))


def log(base: Path, lines: list[tuple]) -> None:
    """Log ``lines`` as the integration would: a heartbeat of both streams,
    the vehicle's stop and start markers, its state lines."""
    for hour, role, value, unit, attrs in lines:
        if role is None:
            vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(hour), "--lines", "0")
            vledger(base, "l0", "heartbeat", "--chargepoint", CHARGEPOINT, "--t", at(hour),
                    "--lines", "0")
            continue
        if role == "stop":
            vledger(base, "l0", "stop", "--vehicle", VEHICLE, "--t", at(hour), "--reason", "shutdown")
            continue
        if role == "start":
            vledger(base, "l0", "start", "--vehicle", VEHICLE, "--t", at(hour),
                    "--homeassistant", "2026.9.4", "--snapshot", "[]")
            continue
        argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(hour), "--role", role,
                "--entity", VEHICLE_CONFIG["roles"][role]["entity"], "--state", value]
        if unit:
            argv += ["--unit", unit]
        for k, v in attrs.items():
            argv += ["--attr", f"{k}={v}"]
        vledger(base, *argv)


def receipts(base: Path) -> None:
    """The pump's receipt for the fill, and a bill for each charge."""
    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(5.2),
            "--quantity-l", "41.37", "--total-price", "72.36", "--full", "--place", "motorway services")
    vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(10),
            "--energy-kwh", "8.1", "--total-price", "2.83")
    vledger(base, "receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(15.5),
            "--energy-kwh", "11.8", "--total-price", "6.49", "--provider", "roaming card")


def derived(base: Path) -> dict[str, list[dict]]:
    """What each derivation prints."""
    return {kind: jsonl(vledger(base, "derive", DERIVE[kind], "--vehicle", VEHICLE).stdout)
            for kind in KINDS}


def l1(base: Path) -> dict[str, list[dict]]:
    """Every kind of event in L1, written from scratch and read back as
    ``l1 read`` gives it."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    return {kind: jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind).stdout)
            for kind in KINDS}


def every_line(events: dict[str, list[dict]]) -> list[dict]:
    return [e for kind in KINDS for e in events[kind]]


@pytest.fixture(scope="module")
def unreceipted(tmp_path_factory) -> Path:
    """The day's streams, without a receipt."""
    base = tmp_path_factory.mktemp("unreceipted")
    begin(base)
    log(base, scenario())
    return base


@pytest.fixture(scope="module")
def receipted(unreceipted: Path, tmp_path_factory) -> Path:
    """The same day, with the receipts entered."""
    base = tmp_path_factory.mktemp("receipted") / "vledger"
    shutil.copytree(unreceipted, base)
    receipts(base)
    return base


@pytest.fixture(scope="module")
def gaps(tmp_path_factory) -> Path:
    """The day whose drive, fill and charge cross capture gaps."""
    base = tmp_path_factory.mktemp("gaps")
    begin(base)
    log(base, gapped())
    return base


@pytest.fixture(scope="module", params=["unreceipted", "receipted", "gaps"])
def outputs(request) -> list[tuple[str, dict[str, list[dict]]]]:
    """For each data directory, what ``derive`` prints and what L1 holds."""
    base = request.getfixturevalue(request.param)
    return [("derive", derived(base)), ("l1", l1(base))]


def test_the_day_derives_every_kind_of_event(receipted):
    """The inputs are not trivially empty: trips, the fill, both charges and
    the periods are derived."""
    out = derived(receipted)
    assert out["trip"], out
    assert len(out["refuelling"]) == 1, out["refuelling"]
    assert [c["chargepoint"] for c in out["charging"]] == [CHARGEPOINT, "foreign"], out["charging"]
    assert {p["period"] for p in out["period"]} >= {"month", "year", "lifetime"}, out["period"]


def test_every_event_and_period_carries_one_of_the_four_flags(outputs):
    """Every trip, refuelling, charging session and period line, as derived
    and as L1 holds it, carries ``quality``, one of the four flags."""
    for where, events in outputs:
        lines = every_line(events)
        assert lines, where
        for e in lines:
            assert e.get("quality") in FLAGS, (where, e)


def test_every_flag_a_line_carries_is_one_of_the_four_or_null(outputs):
    """Every ``…_quality`` key of every line says one of the four flags —
    or, beside a value that is not there, ``null`` — and nothing else."""
    for where, events in outputs:
        for e in every_line(events):
            flags = {k: v for k, v in e.items() if k.endswith("_quality")}
            for k, v in flags.items():
                assert v is None or v in FLAGS, (where, k, v, e)


def test_each_period_metric_comes_with_its_flag(outputs):
    """Every metric of a period line comes with ``<key>_quality``: one of the
    four flags beside a value, ``null`` beside a ``null``."""
    for where, events in outputs:
        for line in events["period"]:
            for key in METRICS:
                assert key in line, (where, key, line)
                assert f"{key}_quality" in line, (where, key, line)
                if line[key] is None:
                    assert line[f"{key}_quality"] is None, (where, key, line)
                else:
                    assert line[f"{key}_quality"] in FLAGS, (where, key, line)


def test_a_periods_flag_is_the_weakest_of_its_values(outputs):
    """A period line's own ``quality`` is the weakest of its values' flags,
    in the order receipt, measured, estimated, incomplete."""
    for where, events in outputs:
        for line in events["period"]:
            flags = [v for k, v in line.items() if k.endswith("_quality") and v is not None]
            assert flags, (where, line)
            assert line["quality"] == max(flags, key=FLAGS.index), (where, line)


def test_the_electric_distance_share_is_always_estimated(outputs):
    """``electric_distance_share`` is weighted by assumed efficiencies, and
    always ``estimated`` where it has a value."""
    for where, events in outputs:
        shares = [p for p in events["period"] if p["electric_distance_share"] is not None]
        assert shares, (where, events["period"])
        for line in shares:
            assert line["electric_distance_share_quality"] == "estimated", (where, line)


def test_a_receipts_values_are_flagged_receipt(receipted):
    """With a receipt, its values come first in L1, flagged ``receipt``: the
    fill's litres and price, each charge's grid-side energy, its source and
    its cost. (Receipts meet events in L1; ``derive`` per kind prints the
    detection, and ``derive match`` the meeting — the user guide's
    *Receipts*.)"""
    for where, events in (("l1", l1(receipted)),):
        [fill] = events["refuelling"]
        assert fill["quantity_l"] == 41.37, (where, fill)
        assert fill["quantity_quality"] == "receipt", (where, fill)
        assert fill["price"] == 72.36, (where, fill)
        assert fill["price_quality"] == "receipt", (where, fill)
        assert len(events["charging"]) == 2, (where, events["charging"])
        for session in events["charging"]:
            assert session["grid_kwh_quality"] == "receipt", (where, session)
            assert session["grid_kwh_source"] == "receipt", (where, session)
            assert session["cost_quality"] == "receipt", (where, session)


def test_energy_from_the_charging_power_is_estimated_and_so_is_its_cost(unreceipted):
    """Without a receipt, the charge at home takes its grid-side energy from
    the charge point's power, ``estimated``, and its cost from the tariff
    with the energy's quality; the foreign charge has neither cost nor a
    flag for it until a receipt gives one."""
    for where, events in (("derive", derived(unreceipted)), ("l1", l1(unreceipted))):
        home, foreign = events["charging"]
        assert home["chargepoint"] == CHARGEPOINT, (where, home)
        assert home["grid_kwh_source"] == "charge_point_power", (where, home)
        assert home["grid_kwh"] is not None, (where, home)
        assert home["grid_kwh_quality"] == "estimated", (where, home)
        assert home["cost_eur"] is not None, (where, home)
        assert home["cost_quality"] == "estimated", (where, home)
        assert foreign["chargepoint"] == "foreign", (where, foreign)
        assert foreign.get("cost_eur") is None, (where, foreign)
        assert foreign.get("cost_quality") is None, (where, foreign)


def test_a_receipt_that_met_nothing_is_an_event_flagged_receipt(receipted, tmp_path):
    """A refuelling receipt anchored where no fill was detected stands in L1
    as a refuelling of its own, ``start`` and ``end`` its anchor, flagged
    ``receipt``."""
    base = tmp_path / "vledger"
    shutil.copytree(receipted, base)
    vledger(base, "receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(19.5),
            "--quantity-l", "20.0", "--total-price", "37.98", "--partial")
    matches = jsonl(vledger(base, "derive", "match", "--vehicle", VEHICLE).stdout)
    assert [m["match"] for m in matches if m["anchor"] == at(19.5)] == ["own"], matches
    own = [e for e in l1(base)["refuelling"] if e["start"] == at(19.5)]
    assert len(own) == 1, own
    assert own[0]["end"] == at(19.5), own
    assert own[0]["quality"] == "receipt", own


def test_a_trip_across_a_capture_gap_is_incomplete(gaps):
    """The drive that capture was off for part of is ``incomplete``: nothing
    is read across the gap, so whatever trips the drive from 03:00 to 04:00
    yields, each of them is."""
    for where, events in (("derive", derived(gaps)), ("l1", l1(gaps))):
        drive = [t for t in events["trip"] if at(3) <= t["start"] <= at(4)]
        assert drive, (where, events["trip"])
        for trip in drive:
            assert trip["quality"] == "incomplete", (where, trip)


def test_a_fill_whose_settle_time_spans_a_gap_is_incomplete(gaps):
    """The fill whose settle time capture was off for is ``incomplete``, with
    no level after."""
    for where, events in (("derive", derived(gaps)), ("l1", l1(gaps))):
        fills = [r for r in events["refuelling"] if at(5.9) <= r["start"] <= at(6.1)]
        assert len(fills) == 1, (where, events["refuelling"])
        assert fills[0]["quality"] == "incomplete", (where, fills[0])
        assert fills[0]["level_after_l"] is None, (where, fills[0])


def test_a_charge_across_a_gap_of_t_still_or_longer_is_incomplete(gaps):
    """The charge at home that capture was off for an hour of — longer than
    T_still — is ``incomplete``."""
    for where, events in (("derive", derived(gaps)), ("l1", l1(gaps))):
        across = [c for c in events["charging"] if c["start"] < at(9.5) and c["end"] > at(10.5)]
        assert len(across) == 1, (where, events["charging"])
        assert across[0]["quality"] == "incomplete", (where, across[0])
