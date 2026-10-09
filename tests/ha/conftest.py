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

from vledger import config as vconfig


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
