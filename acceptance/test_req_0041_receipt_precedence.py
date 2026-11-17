# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0041, as a user meets it: values from receipts (quantity, energy,
price) take precedence over sensor values.

Derived from the requirement and the user documentation: README's "Receipt
values beat sensor values"; the user guide's *What the vehicle shows* (a
refuelling shows "the receipt's litres, or the sensor's before a receipt"),
its card ("The quantity is a refuelling's litres from its receipt once one is
entered and the sensor's until then … The price is the receipt's, or a
session's cost from its charge point's tariff", and *implausible* "where a
receipt and the sensor disagree by more than the plausibility threshold"),
its *The derivations* (a session's ``grid_kwh`` comes from the charge point's
meter, else its charging power, else the loss factor, and "A charging receipt
takes precedence over all of them"; a refuelling's ``sensor_delta_l`` is
"always ``estimated``, the receipt says what was bought", and with a fuel
price role its ``price_suggestion``), its *Periods* ("the litres and the fuel
cost bought, from the receipts"; "the grid-side kWh and electricity cost of
the charging sessions"), and its *Receipts* (a correction is the whole receipt
again, a cancellation takes it back).

Black-box: every step is the ``vledger`` CLI run as a separate process, the
way the user guide shows it — streams written with ``vledger l0``, receipts
entered, corrected and cancelled with ``vledger receipt``, L1 built with
``vledger derive all --write`` and read with ``vledger l1 read``, and the
export with ``vledger export``. Nothing here imports the library.

The user documentation gives neither the shape of the configuration ``vledger
l0 config`` takes nor the keys of an event's line beyond those it names; the
tests write the configuration as the integration writes it, and read the keys
from the commands' output (ISSUE-0140, ISSUE-0150)."""

import csv
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0041")]

SRC = Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
CHARGEPOINT = "cp01"
DAY = datetime(2026, 10, 5, tzinfo=UTC)

HOME = (51.0000, 7.0000)        # the charge point, with a charging power and a tariff
STATION = (51.1000, 7.0800)     # a fuel station
AWAY = (51.3000, 7.4000)        # a charge somewhere else: foreign

POWER_KW = 3.7
TARIFF = 0.30
NET_KWH = 10.0

VEHICLE_CONFIG = {
    "name": "Test car",
    "roles": {
        "odometer": {"entity": "sensor.odometer"},
        "position": {"entity": "device_tracker.car"},
        "fuel_level": {"entity": "sensor.fuel_level"},
        "fuel_price": {"entity": "sensor.fuel_price"},
        "soc": {"entity": "sensor.soc"},
        "charging_state": {"entity": "sensor.charging_state", "map": {"charging": ["charging"]}},
    },
    "parameters": {"fuel": "petrol", "battery_net_kwh": NET_KWH},
    "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
}

CHARGEPOINT_CONFIG = {
    "name": "Home",
    "latitude": HOME[0],
    "longitude": HOME[1],
    "radius_m": 100,
    "meter": None,
    "power_kw": POWER_KW,
    "tariffs": [{"from": "2026-01-01", "eur_per_kwh": TARIFF}],
}

# What the sensors see, and what the receipts say.
LEVEL_BEFORE, LEVEL_AFTER = 15.0, 45.0           # the sensor sees 30 L go in
PUMP_PRICE = "1.899"                             # the fuel price role's value at the pump
RECEIPT_L, RECEIPT_TOTAL = 41.37, 72.36          # the pump says 41.37 L for 72.36
HOME_HOURS = 2                                   # charging at home: 2 h × 3.7 kW = 7.4 kWh, 2.22 at the tariff
HOME_RECEIPT_KWH, HOME_RECEIPT_TOTAL = 8.1, 2.83
AWAY_RECEIPT_KWH, AWAY_RECEIPT_TOTAL = 11.8, 6.49


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


def scenario() -> list[tuple]:
    """A day of the vehicle, as (hour, role, state, unit, attrs) in time
    order, ``role`` None for a heartbeat of both streams every half hour:

    - 01:00 at home, 15 L in the tank, 40 % charged;
    - 03:00–04:00 a drive to a fuel station, the fill at 05:00 from 15 to 45 L
      while the pump's price stands at 1.899;
    - 07:00–08:00 home again, charging there from 09:00 to 11:00, 40 → 70 %;
    - 13:00–14:00 a drive elsewhere, charging there from 15:00 to 16:00,
      50 → 90 %, at no configured charge point;
    - heartbeats until 20:00."""
    def fix(where):
        return {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}

    lines = [
        (1, "odometer", "1000", "km", {}), (1, "position", "home", None, fix(HOME)),
        (1, "fuel_level", str(LEVEL_BEFORE + 2), "L", {}), (1, "fuel_price", "1.859", "EUR/L", {}),
        (1, "soc", "50", "%", {}), (1, "charging_state", "idle", None, {}),
        # To the fuel station.
        (3, "odometer", "1010", "km", {}), (3, "position", "not_home", None, fix((51.05, 7.04))),
        (4, "odometer", "1020", "km", {}), (4, "position", "not_home", None, fix(STATION)),
        (4.1, "fuel_level", str(LEVEL_BEFORE), "L", {}), (4.1, "soc", "40", "%", {}),
        (4.5, "fuel_price", PUMP_PRICE, "EUR/L", {}),
        (5, "fuel_level", str(LEVEL_AFTER), "L", {}),
        (5.5, "fuel_level", str(LEVEL_AFTER), "L", {}),
        # Home.
        (7, "odometer", "1030", "km", {}), (7, "position", "not_home", None, fix((51.05, 7.04))),
        (8, "odometer", "1040", "km", {}), (8, "position", "home", None, fix(HOME)),
        (8.1, "fuel_level", str(LEVEL_AFTER - 1), "L", {}),
        (9, "charging_state", "charging", None, {}), (9.1, "position", "home", None, fix(HOME)),
        (10, "soc", "55", "%", {}),
        (9 + HOME_HOURS, "soc", "70", "%", {}), (9 + HOME_HOURS, "charging_state", "idle", None, {}),
        # Elsewhere.
        (13, "odometer", "1060", "km", {}), (13, "position", "not_home", None, fix((51.2, 7.2))),
        (14, "odometer", "1085", "km", {}), (14, "position", "not_home", None, fix(AWAY)),
        (14.1, "soc", "50", "%", {}), (14.1, "fuel_level", str(LEVEL_AFTER - 3), "L", {}),
        (15, "charging_state", "charging", None, {}), (15.1, "position", "not_home", None, fix(AWAY)),
        (15.5, "soc", "70", "%", {}),
        (16, "soc", "90", "%", {}), (16, "charging_state", "idle", None, {}),
    ]
    lines += [(h / 2, None, None, None, None) for h in range(1, 41)]
    return sorted(lines, key=lambda x: (x[0], x[1] is None))


def write(base: Path) -> None:
    """Log the day as the integration would: a start and the configuration
    first, then every change, both streams beating every half hour."""
    for flag, subject, config in (("--chargepoint", CHARGEPOINT, CHARGEPOINT_CONFIG),
                                  ("--vehicle", VEHICLE, VEHICLE_CONFIG)):
        vledger(base, "l0", "start", flag, subject, "--t", at(0), "--homeassistant", "2026.9.4",
                "--snapshot", "[]")
        vledger(base, "l0", "config", flag, subject, "--t", at(0), "--config", json.dumps(config))
    for hour, role, value, unit, attrs in scenario():
        if role is None:
            vledger(base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", at(hour), "--lines", "0")
            vledger(base, "l0", "heartbeat", "--chargepoint", CHARGEPOINT, "--t", at(hour),
                    "--lines", "0")
            continue
        argv = ["l0", "state", "--vehicle", VEHICLE, "--t", at(hour), "--role", role,
                "--entity", VEHICLE_CONFIG["roles"][role]["entity"], "--state", value]
        if unit:
            argv += ["--unit", unit]
        for k, v in attrs.items():
            argv += ["--attr", f"{k}={v}"]
        vledger(base, *argv)


def refuelling_receipt(base: Path, quantity=RECEIPT_L, total=RECEIPT_TOTAL, unit_price=None,
                       replaces=None) -> str:
    argv = ["receipt", "add", "refuelling", "--vehicle", VEHICLE, "--anchor", at(5.2),
            "--quantity-l", str(quantity), "--full"]
    if total is not None:
        argv += ["--total-price", str(total)]
    if unit_price is not None:
        argv += ["--unit-price", str(unit_price)]
    if replaces:
        argv += ["--replaces", replaces]
    return jsonl(vledger(base, *argv).stdout)[0]["id"]


def charging_receipt(base: Path, hour: float, kwh: float, total: float, replaces=None) -> str:
    argv = ["receipt", "add", "charging", "--vehicle", VEHICLE, "--anchor", at(hour),
            "--energy-kwh", str(kwh), "--total-price", str(total)]
    if replaces:
        argv += ["--replaces", replaces]
    return jsonl(vledger(base, *argv).stdout)[0]["id"]


def l1(base: Path, kind: str) -> list[dict]:
    """The events of ``kind`` in L1, rebuilt from scratch first."""
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    return jsonl(vledger(base, "l1", "read", "--vehicle", VEHICLE, "--kind", kind).stdout)


def fill(base: Path) -> dict:
    [event] = l1(base, "refuelling")
    assert event["start"] == at(5), event
    return event


def sessions(base: Path) -> tuple[dict, dict]:
    """The session at home and the one elsewhere."""
    home, away = l1(base, "charging")
    assert home["chargepoint"] == CHARGEPOINT and away["chargepoint"] == "foreign", (home, away)
    return home, away


def month(base: Path) -> dict:
    [line] = [p for p in l1(base, "period") if p["period"] == "month"]
    return line


@pytest.fixture
def base(tmp_path: Path) -> Path:
    write(tmp_path)
    return tmp_path


# Refuellings: quantity and price.

def test_a_refuelling_without_a_receipt_shows_what_the_sensor_saw(base):
    """Before any receipt the refuelling's litres are the sensor's — the
    rise of the level, 30 L — estimated, and it waits for a receipt."""
    event = fill(base)
    assert event["sensor_delta_l"] == pytest.approx(LEVEL_AFTER - LEVEL_BEFORE)
    assert event["sensor_delta_quality"] == "estimated"
    assert event.get("quantity_l") in (None, pytest.approx(LEVEL_AFTER - LEVEL_BEFORE))
    assert event["confirmation"] == "unconfirmed"
    assert event["price_suggestion"] == pytest.approx(float(PUMP_PRICE))


def test_a_refuelling_receipt_s_litres_take_precedence_over_the_sensor_s(base):
    """With the pump's receipt for 41.37 L, the refuelling's quantity is the
    receipt's, of quality ``receipt``; the sensor's 30 L stay beside it as
    the sensor's estimate, not as what was bought."""
    rid = refuelling_receipt(base)
    event = fill(base)
    assert event["receipt"] == rid and event["confirmation"] == "receipt"
    assert event["quantity_l"] == pytest.approx(RECEIPT_L)
    assert event["quantity_quality"] == "receipt"
    assert event["sensor_delta_l"] == pytest.approx(LEVEL_AFTER - LEVEL_BEFORE)
    assert event["sensor_delta_quality"] == "estimated"


def test_a_refuelling_receipt_s_price_takes_precedence_over_the_price_role(base):
    """The fuel price role suggested 1.899 a litre at the pump; the receipt
    says 72.36 for 41.37 L. The refuelling's price is the receipt's total,
    and its price per litre the receipt's, 1.749 — not the suggestion."""
    refuelling_receipt(base)
    event = fill(base)
    assert event["price"] == pytest.approx(RECEIPT_TOTAL)
    assert event["unit_price"] == pytest.approx(RECEIPT_TOTAL / RECEIPT_L, abs=0.001)
    assert event["price_quality"] == "receipt"
    assert event["price_suggestion"] == pytest.approx(float(PUMP_PRICE))


def test_a_receipt_with_only_a_price_per_litre_takes_precedence_too(base):
    """A receipt giving the price per litre instead of the total: the
    refuelling is priced by the receipt's 1.749 a litre on the receipt's
    litres, not by the fuel price role's 1.899 nor the sensor's 30 L."""
    refuelling_receipt(base, total=None, unit_price=1.749)
    event = fill(base)
    assert event["unit_price"] == pytest.approx(1.749)
    assert event["price"] == pytest.approx(1.749 * RECEIPT_L, abs=0.01)
    assert event["price_quality"] == "receipt"


def test_the_receipt_wins_even_where_it_and_the_sensor_disagree_beyond_plausibility(base):
    """A receipt for 60 L where the sensor saw 30 L: the ledger flags the
    disagreement as implausible, and the quantity is still the receipt's."""
    refuelling_receipt(base, quantity=60.0, total=105.0)
    event = fill(base)
    assert event["implausible"] is True
    assert event["quantity_l"] == pytest.approx(60.0)
    assert event["quantity_quality"] == "receipt"
    assert event["price"] == pytest.approx(105.0)


def test_a_corrected_receipt_s_values_take_over_and_a_cancelled_one_gives_way_to_the_sensor(base):
    """A correction is the whole receipt again: its litres and price replace
    the first receipt's. Cancelled, no receipt counts, and the refuelling
    is back to the sensor's 30 L, waiting for a receipt."""
    first = refuelling_receipt(base)
    second = refuelling_receipt(base, quantity=41.73, total=72.99, replaces=first)
    event = fill(base)
    assert event["receipt"] == second
    assert (event["quantity_l"], event["price"]) == (pytest.approx(41.73), pytest.approx(72.99))
    vledger(base, "receipt", "cancel", second, "--vehicle", VEHICLE, "--note", "test")
    event = fill(base)
    assert not event.get("receipt") and event["confirmation"] == "unconfirmed"
    assert event.get("quantity_l") in (None, pytest.approx(LEVEL_AFTER - LEVEL_BEFORE))
    assert event.get("quantity_quality") != "receipt"


# Charging sessions: energy and price.

def test_a_session_without_a_receipt_has_the_charge_point_s_energy_and_tariff(base):
    """Before any receipt: at home, two hours at the charge point's 3.7 kW,
    7.4 kWh, costed at its tariff, both estimated; elsewhere, the battery's
    4 kWh through the loss factor, estimated, and no cost."""
    home, away = sessions(base)
    assert home["grid_kwh"] == pytest.approx(HOME_HOURS * POWER_KW)
    assert (home["grid_kwh_source"], home["grid_kwh_quality"]) == ("charge_point_power", "estimated")
    assert home["cost_eur"] == pytest.approx(HOME_HOURS * POWER_KW * TARIFF)
    assert away["grid_kwh_source"] == "loss_factor" and away["grid_kwh_quality"] == "estimated"
    assert away["grid_kwh"] >= away["battery_kwh"] == pytest.approx(0.4 * NET_KWH)
    assert away["cost_eur"] is None


def test_a_charging_receipt_takes_precedence_over_the_charge_point_s_power_and_tariff(base):
    """A receipt for the charge at home, 8.1 kWh for 2.83: the session's
    grid energy and its cost are the receipt's, of quality ``receipt``, not
    the 7.4 kWh of the charging power nor the 2.22 of the tariff."""
    rid = charging_receipt(base, 9, HOME_RECEIPT_KWH, HOME_RECEIPT_TOTAL)
    home, _ = sessions(base)
    assert home["receipt"] == rid
    assert home["grid_kwh"] == pytest.approx(HOME_RECEIPT_KWH)
    assert (home["grid_kwh_source"], home["grid_kwh_quality"]) == ("receipt", "receipt")
    assert home["cost_eur"] == pytest.approx(HOME_RECEIPT_TOTAL)
    assert home["cost_quality"] == "receipt"


def test_a_charging_receipt_takes_precedence_over_the_loss_factor(base):
    """A receipt for the charge elsewhere, 11.8 kWh for 6.49: the session's
    grid energy and its cost are the receipt's, not the loss factor's
    estimate and not missing."""
    rid = charging_receipt(base, 15, AWAY_RECEIPT_KWH, AWAY_RECEIPT_TOTAL)
    _, away = sessions(base)
    assert away["receipt"] == rid and away["confirmation"] == "receipt"
    assert away["grid_kwh"] == pytest.approx(AWAY_RECEIPT_KWH)
    assert (away["grid_kwh_source"], away["grid_kwh_quality"]) == ("receipt", "receipt")
    assert away["cost_eur"] == pytest.approx(AWAY_RECEIPT_TOTAL)
    assert away["cost_quality"] == "receipt"
    # The charging loss is the receipt's grid side over the battery side.
    assert away["charging_loss_kwh"] == pytest.approx(AWAY_RECEIPT_KWH - away["battery_kwh"])


def test_a_cancelled_charging_receipt_gives_way_to_the_sensor_values_again(base):
    """Cancelled, the receipt no longer counts: the session at home is back
    to the charging power's energy and the tariff's cost."""
    rid = charging_receipt(base, 9, HOME_RECEIPT_KWH, HOME_RECEIPT_TOTAL)
    vledger(base, "receipt", "cancel", rid, "--vehicle", VEHICLE, "--note", "test")
    home, _ = sessions(base)
    assert not home.get("receipt")
    assert home["grid_kwh"] == pytest.approx(HOME_HOURS * POWER_KW)
    assert home["grid_kwh_source"] == "charge_point_power"
    assert home["cost_eur"] == pytest.approx(HOME_HOURS * POWER_KW * TARIFF)


# What is built on them.

def test_the_month_s_figures_take_the_receipts_values(base):
    """The month's litres and fuel cost bought are the refuelling receipt's;
    its grid energy and electricity cost are the charging receipts', of
    quality ``receipt`` — none of the sensors' figures."""
    refuelling_receipt(base)
    charging_receipt(base, 9, HOME_RECEIPT_KWH, HOME_RECEIPT_TOTAL)
    charging_receipt(base, 15, AWAY_RECEIPT_KWH, AWAY_RECEIPT_TOTAL)
    line = month(base)
    assert line["fuel_purchased_l"] == pytest.approx(RECEIPT_L)
    assert line["fuel_cost_eur"] == pytest.approx(RECEIPT_TOTAL)
    assert line["grid_kwh"] == pytest.approx(HOME_RECEIPT_KWH + AWAY_RECEIPT_KWH)
    assert line["electricity_cost_eur"] == pytest.approx(HOME_RECEIPT_TOTAL + AWAY_RECEIPT_TOTAL)
    for key in ("fuel_purchased_l", "fuel_cost_eur", "grid_kwh", "electricity_cost_eur"):
        assert line[f"{key}_quality"] == "receipt", (key, line)


def test_the_export_carries_the_receipts_values(base):
    """Exported as CSV for a spreadsheet, the refuelling and the sessions
    carry the receipts' quantities and prices, as L1 holds them."""
    refuelling_receipt(base)
    charging_receipt(base, 15, AWAY_RECEIPT_KWH, AWAY_RECEIPT_TOTAL)
    vledger(base, "derive", "all", "--vehicle", VEHICLE, "--write")
    [row] = list(csv.DictReader(io.StringIO(
        vledger(base, "export", "csv", "--vehicle", VEHICLE, "--kind", "refuelling").stdout)))
    assert float(row["quantity_l"]) == pytest.approx(RECEIPT_L)
    assert float(row["price"]) == pytest.approx(RECEIPT_TOTAL)
    rows = list(csv.DictReader(io.StringIO(
        vledger(base, "export", "csv", "--vehicle", VEHICLE, "--kind", "charging").stdout)))
    away = [r for r in rows if r["chargepoint"] == "foreign"]
    assert len(away) == 1
    assert float(away[0]["grid_kwh"]) == pytest.approx(AWAY_RECEIPT_KWH)
    assert float(away[0]["cost_eur"]) == pytest.approx(AWAY_RECEIPT_TOTAL)
