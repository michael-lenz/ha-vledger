# SPDX-License-Identifier: BSD-3-Clause
"""The configuration object: complete over defaults, refusing what the
requirements refuse, and answering which tariff holds when."""

import pytest

from vledger import config


def test_a_vehicle_is_complete_over_the_defaults():
    v = config.vehicle("Volvo", {"odometer": {"entity": "sensor.o"}}, {"fuel": "diesel"})
    assert v["parameters"]["fuel"] == "diesel"
    assert v["parameters"]["eta_ice"] == 0.33            # the diesel default
    assert v["parameters"]["tank_capacity_l"] is None    # not set, not refused
    assert v["thresholds"]["heating_value_kwh_per_l"] == 9.8
    assert v["thresholds"]["t_still_s"] == 1800
    assert set(v["thresholds"]) == set(config.DEFAULT_THRESHOLDS)


def test_an_explicit_value_beats_a_fuel_default():
    v = config.vehicle("V", {"odometer": {"entity": "sensor.o"}},
                       {"fuel": "diesel", "eta_ice": 0.3}, {"heating_value_kwh_per_l": 10})
    assert v["parameters"]["eta_ice"] == 0.3
    assert v["thresholds"]["heating_value_kwh_per_l"] == 10


def test_what_is_refused():
    with pytest.raises(ValueError, match="movement role"):
        config.vehicle("V", {"soc": {"entity": "sensor.s"}})
    with pytest.raises(ValueError, match="unknown vehicle role"):
        config.vehicle("V", {"speed": {"entity": "sensor.s"}})
    with pytest.raises(ValueError, match="takes no map"):
        config.vehicle("V", {"odometer": {"entity": "sensor.o", "map": {}}})
    with pytest.raises(ValueError, match="other than 'charging'"):
        config.vehicle("V", {"odometer": {"entity": "sensor.o"},
                             "charging_state": {"entity": "s.c", "map": {"idle": ["x"]}}})
    with pytest.raises(ValueError, match="unknown parameters"):
        config.vehicle("V", {"odometer": {"entity": "sensor.o"}}, {"colour": "red"})
    with pytest.raises(ValueError, match="fuel must be"):
        config.vehicle("V", {"odometer": {"entity": "sensor.o"}}, {"fuel": "lpg"})


def test_missing_names_what_a_derivation_will_lack():
    v = config.vehicle("V", {"odometer": {"entity": "sensor.o"}, "fuel_level": {"entity": "sensor.f"},
                             "soc": {"entity": "sensor.s"}})
    names = [m.split(":")[0] for m in config.missing(v)]
    assert names == ["tank_capacity_l", "fuel", "battery_net_kwh", "fuel_level_resolution_l"]
    full = config.vehicle("V", {"odometer": {"entity": "sensor.o"}})
    assert config.missing(full) == []


def test_tariff_at_takes_the_latest_from_and_the_last_appended_on_a_tie():
    tariffs = [{"from": "2026-01-01", "eur_per_kwh": 0.30},
               {"from": "2026-07-01", "eur_per_kwh": 0.34},
               {"from": "2026-01-01", "eur_per_kwh": 0.31}]   # a correction
    assert config.tariff_at(tariffs, "2025-12-31T23:59:59Z") is None
    assert config.tariff_at(tariffs, "2026-03-01T12:00:00Z")["eur_per_kwh"] == 0.31
    assert config.tariff_at(tariffs, "2026-07-01T00:00:00Z")["eur_per_kwh"] == 0.34
    assert config.tariff_at(tariffs, "2026-06-30T23:00:00-02:00")["eur_per_kwh"] == 0.34  # already July in UTC


def test_domain_state_holds_on_unmapped_and_unavailable():
    m = {"charging": ["Charging"]}
    assert config.domain_state("charging_state", "Charging", m) == "charging"
    assert config.domain_state("charging_state", "Idle", m) is None
    assert config.domain_state("charging_state", "unavailable", m) is None


def test_a_chargepoint_is_checked():
    cp = config.chargepoint("Home", 48.1, 11.5, 50, [{"from": "2026-01-01", "eur_per_kwh": 0.3}])
    assert cp["meter"] is None and config.is_chargepoint(cp)
    with pytest.raises(ValueError):
        config.chargepoint("Home", 48.1, 11.5, 0, [])
    with pytest.raises(ValueError):
        config.chargepoint("Home", 48.1, 11.5, 50, [{"from": "yesterday", "eur_per_kwh": 0.3}])
