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


def test_a_parameter_given_as_none_is_not_set_and_takes_its_default():
    # What the options flow hands over for a field left empty (ISSUE-0022).
    v = config.vehicle("V", {"odometer": {"entity": "sensor.o"}},
                       {k: None for k in config.DEFAULT_PARAMETERS} | {"fuel": "diesel"})
    assert v["parameters"]["charging_loss_factor"] == 1.12
    assert v["parameters"]["eta_ice"] == 0.33
    assert v["parameters"]["charge_cycles_start"] == 0
    assert v["parameters"]["tank_capacity_l"] is None


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


def test_domain_state_holds_only_on_unavailable_and_unknown():
    m = {"charging": ["Charging"]}
    assert config.domain_state("charging_state", "Charging", m) == "charging"
    assert config.domain_state("charging_state", "Idle", m) == "not_charging"   # ADR-0008
    assert config.domain_state("charging_state", "unavailable", m) is None
    assert config.domain_state("charging_state", "unknown", m) is None
    assert config.domain_state("ignition", "off", {"on": ["on"]}) == "off"
    assert config.domain_state("plug_state", "off", {"plugged": ["on"]}) == "unplugged"


def test_a_chargepoint_is_checked():
    cp = config.chargepoint("Home", 48.1, 11.5, 50, [{"from": "2026-01-01", "eur_per_kwh": 0.3}])
    assert cp["meter"] is None and cp["power_kw"] is None and config.is_chargepoint(cp)
    assert config.chargepoint("Home", 48.1, 11.5, 50, [], power_kw=6.35)["power_kw"] == 6.35
    with pytest.raises(ValueError, match="power_kw"):
        config.chargepoint("Home", 48.1, 11.5, 50, [], power_kw=0)
    with pytest.raises(ValueError):
        config.chargepoint("Home", 48.1, 11.5, 0, [])
    with pytest.raises(ValueError):
        config.chargepoint("Home", 48.1, 11.5, 50, [{"from": "yesterday", "eur_per_kwh": 0.3}])


def test_movement_reporting_is_sampled_or_per_cycle():
    roles = {"odometer": {"entity": "sensor.o"}}
    assert config.vehicle("V", roles)["parameters"]["movement_reporting"] == "sampled"
    assert config.vehicle("V", roles, {"movement_reporting": "per_cycle"})["parameters"][
        "movement_reporting"] == "per_cycle"
    assert config.vehicle("V", roles)["thresholds"]["exit_window_s"] == 300
    with pytest.raises(ValueError, match="movement_reporting"):
        config.vehicle("V", roles, {"movement_reporting": "sometimes"})


def test_the_trip_computer_needs_the_trip_counter_and_the_level_its_resolution():
    v = config.vehicle("V", {"odometer": {"entity": "sensor.o"}, "trip_consumption": {"entity": "sensor.a"},
                             "fuel_level": {"entity": "sensor.f"}}, {"fuel": "petrol", "tank_capacity_l": 40})
    assert v["parameters"]["soc_resolution_pct"] == 1
    assert v["thresholds"]["trip_consumption_step_l_per_100km"] == 0.1
    said = config.missing(v)
    assert any(s.startswith("trip_distance:") for s in said)
    assert any(s.startswith("fuel_level_resolution_l:") for s in said)
    v = config.vehicle("V", {"trip_distance": {"entity": "sensor.t"}, "trip_consumption": {"entity": "sensor.a"}})
    assert not any(s.startswith("trip_distance:") for s in config.missing(v))


# A config line written unchecked (`vledger l0 config`) may name a fuel and
# leave out the keys whose default depends on it (ISSUE-0170).
DIESEL_LINE = {"name": "D", "roles": {"odometer": {"entity": "sensor.o"}},
               "parameters": {"fuel": "diesel"}}


def test_a_partial_line_reads_with_the_defaults_of_its_fuel():
    p, thr = config.parameters_of(DIESEL_LINE), config.thresholds_of(DIESEL_LINE)
    assert p["eta_ice"] == 0.33
    assert thr["heating_value_kwh_per_l"] == 9.8
    assert thr["beta_per_k"] == 8.0e-4
    assert thr["t_still_s"] == config.DEFAULT_THRESHOLDS["t_still_s"]
    full = config.vehicle("D", DIESEL_LINE["roles"], DIESEL_LINE["parameters"])
    assert p == full["parameters"] and thr == full["thresholds"]


def test_a_partial_line_keeps_what_it_names_over_its_fuels_defaults():
    line = {"parameters": {"fuel": "diesel", "eta_ice": 0.3},
            "thresholds": {"beta_per_k": 9e-4}}
    assert config.parameters_of(line)["eta_ice"] == 0.3
    assert config.thresholds_of(line)["beta_per_k"] == 9e-4
    assert config.thresholds_of(line)["heating_value_kwh_per_l"] == 9.8


def test_a_line_without_a_fuel_or_without_a_line_reads_petrols_defaults():
    for line in (None, {}, {"parameters": {"fuel": None}}):
        assert config.parameters_of(line) == config.DEFAULT_PARAMETERS
        assert config.thresholds_of(line) == config.DEFAULT_THRESHOLDS


def test_a_chargepoint_configuration_names_what_the_derivation_cannot_read():
    """ISSUE-0173: the shape l0-format.md gives, checked as validation and
    the derivation read it."""
    good = config.chargepoint("Home", 48.1, 11.5, 50, [{"from": "2026-01-01", "eur_per_kwh": 0.3}],
                              meter={"entity": "sensor.m"}, power_kw=11)
    assert config.chargepoint_problems(good) == []
    assert config.chargepoint_problems({k: v for k, v in good.items() if k != "power_kw"}) == []
    bad = dict(good, tariffs=[{"valid_from": "2026-01-01", "price": 0.3},
                              {"from": "2026-13-01", "eur_per_kwh": 0.3},
                              {"from": "2026-07-01", "eur_per_kwh": -1},
                              {"from": "2026-08-01", "eur_per_kwh": "0.3"}, 7])
    assert config.chargepoint_problems(bad) == [
        "tariffs[0] lacks 'from'", "tariffs[0] lacks 'eur_per_kwh'",
        "tariffs[1]: from '2026-13-01' is not a date YYYY-MM-DD",
        "tariffs[2]: eur_per_kwh is negative", "tariffs[3]: eur_per_kwh is not a number",
        "tariffs[4] is not an object"]
    assert config.chargepoint_problems(dict(good, tariffs={"from": "2026-01-01"})) == [
        "tariffs is not a list"]
    assert config.chargepoint_problems({"tariffs": []}) == [
        "latitude is not a number", "longitude is not a number", "radius_m is not a number"]
    assert config.chargepoint_problems(dict(good, radius_m=0, power_kw=0, meter={})) == [
        "radius_m must be positive", "power_kw must be positive", "meter names no entity"]


def test_the_chargepoint_constructor_refuses_what_the_check_names():
    with pytest.raises(ValueError, match="tariffs\\[0\\] lacks 'from'"):
        config.chargepoint("Home", 48.1, 11.5, 50, [{"eur_per_kwh": 0.3}])
    with pytest.raises(ValueError, match="eur_per_kwh is negative"):
        config.chargepoint("Home", 48.1, 11.5, 50, [{"from": "2026-01-01", "eur_per_kwh": -0.1}])
