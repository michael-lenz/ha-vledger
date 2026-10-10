# SPDX-License-Identifier: BSD-3-Clause
"""The integration's tests run against a real Home Assistant core, through
pytest-homeassistant-custom-component (the ``ha`` extra); tests/conftest.py
leaves this directory out when it is not installed."""

import pytest
from custom_components.vledger.const import (
    DATA_KIND,
    DATA_SUBJECT,
    DOMAIN,
    OPT_BASE_PATH,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

import vledger.derivations  # noqa: F401 — the real derivations register first, so a stand-in replaces one
from vledger import config as vconfig
from vledger import l1


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/vledger."""


@pytest.fixture
def vehicle_entry(tmp_path):
    options = vconfig.vehicle(
        "Volvo",
        {"odometer": {"entity": "sensor.volvo_odometer"},
         "position": {"entity": "device_tracker.volvo"},
         "charging_state": {"entity": "sensor.volvo_charging", "map": {"charging": ["Charging"]}}},
        {"fuel": "petrol", "tank_capacity_l": 71},
        {"heartbeat_s": 600},
    )
    options[OPT_BASE_PATH] = str(tmp_path)
    return MockConfigEntry(
        domain=DOMAIN, title="Volvo", unique_id="a7c1",
        data={DATA_KIND: "vehicle", DATA_SUBJECT: "a7c1"}, options=options,
    )


@pytest.fixture
def phev_entry(tmp_path):
    options = vconfig.vehicle(
        "Golf", {"odometer": {"entity": "sensor.golf_odometer"}},
        {"fuel": "petrol", "tank_capacity_l": 40, "battery_net_kwh": 10.4})
    options[OPT_BASE_PATH] = str(tmp_path)
    return MockConfigEntry(domain=DOMAIN, title="Golf", unique_id="b8d2",
                           data={DATA_KIND: "vehicle", DATA_SUBJECT: "b8d2"}, options=options)


@pytest.fixture
def stand_in(monkeypatch):
    """Stand-in derivations: per kind, the events they are given."""
    events: dict[str, list[dict]] = {"trip": [], "refuelling": [], "charging": []}
    for kind, given in events.items():
        monkeypatch.setitem(l1.DERIVATIONS, kind, lambda base, subject, since, given=given: list(given))
    return events


@pytest.fixture
def chargepoint_entry(tmp_path):
    options = vconfig.chargepoint("Home", 48.1, 11.5, 50, [{"from": "2026-01-01", "eur_per_kwh": 0.3}])
    options[OPT_BASE_PATH] = str(tmp_path)
    return MockConfigEntry(domain=DOMAIN, title="Home", unique_id="c9e3",
                           data={DATA_KIND: "chargepoint", DATA_SUBJECT: "c9e3"}, options=options)


@pytest.fixture
def period_lines(monkeypatch):
    """A stand-in for the periods: the lines it is given, whatever L1 holds."""
    lines: list[dict] = []
    monkeypatch.setattr(l1, "PERIODS", lambda base, subject, events: list(lines))
    return lines
