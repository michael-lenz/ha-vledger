# SPDX-License-Identifier: BSD-3-Clause
"""Charging sessions, derived from streams built through the l0 verbs
(ADR-0005): a vehicle's stream, and the charge points' own streams with
their meters and tariffs (ERF-06)."""

import json
from datetime import timedelta

from vledger import charging, clock, l1
from vledger.cli import main
from vledger.layout import Subject

T0 = clock.parse("2026-10-09T16:00:00Z")
HOME = (51.0000, 7.0000)
WORK = (51.2000, 7.3000)
AWAY = (52.5000, 13.4000)


def at(minutes: float) -> str:
    return clock.to_text(T0 + timedelta(minutes=minutes))


class Stream:
    """One subject's stream, written verb by verb as the integration would,
    with a heartbeat every half hour between the lines asked for."""

    def __init__(self, base, capsys, kind, sid, config, snapshot):
        self.b = ["--base", str(base), f"--{kind}", sid]
        self.capsys = capsys
        self.beat = -30
        self.run("start", *self.b, "--t", at(-60), "--homeassistant", "2026.9.4",
                 "--snapshot", json.dumps(snapshot))
        self.run("config", *self.b, "--t", at(-60), "--config", json.dumps(config))

    def run(self, *argv):
        assert main(["l0", *argv]) == 0, argv
        self.capsys.readouterr()
        return self

    def alive(self, minutes):
        while self.beat < minutes:
            self.run("heartbeat", *self.b, "--t", at(self.beat), "--lines", "0")
            self.beat += 30

    def state(self, minutes, role, value, unit=None, **attrs):
        self.alive(minutes)
        args = ["state", *self.b, "--t", at(minutes), "--role", role, "--entity", f"x.{role}",
                "--state", str(value)]
        if unit:
            args += ["--unit", unit]
        for k, v in attrs.items():
            args += ["--attr", f"{k}={v}"]
        return self.run(*args)

    def heartbeat(self, minutes):
        self.alive(minutes)
        self.beat = minutes + 30
        return self.run("heartbeat", *self.b, "--t", at(minutes), "--lines", "0")


def vehicle(base, capsys, sid="a7c1", where=HOME, soc=40, charging_state=True, parameters=None):
    roles = {"odometer": {"entity": "sensor.o"}, "position": {"entity": "device_tracker.v"},
             "soc": {"entity": "sensor.s"}}
    if charging_state:
        roles["charging_state"] = {"entity": "sensor.c", "map": {"charging": ["Charging"]}}
    cfg = {"name": sid, "roles": roles,
           "parameters": dict({"battery_net_kwh": 14.7, "charging_loss_factor": 1.12}, **(parameters or {})),
           "thresholds": {"t_still_s": 1800, "heartbeat_s": 3600, "charging_threshold_pct": 2}}
    snapshot = [
        {"role": "odometer", "entity": "sensor.o", "state": "1000", "unit": "km", "since": at(-120)},
        {"role": "position", "entity": "device_tracker.v", "state": "home", "since": at(-120),
         "attrs": {"latitude": where[0], "longitude": where[1], "gps_accuracy": 0}},
        {"role": "soc", "entity": "sensor.s", "state": str(soc), "unit": "%", "since": at(-120)},
    ]
    if charging_state:
        snapshot.append({"role": "charging_state", "entity": "sensor.c", "state": "Idle", "since": at(-120)})
    return Stream(base, capsys, "vehicle", sid, cfg, snapshot)


def chargepoint(base, capsys, sid, where, tariffs, meter=True, reading=5000.0):
    cfg = {"name": sid.title(), "latitude": where[0], "longitude": where[1], "radius_m": 50,
           "meter": {"entity": "sensor.meter"} if meter else None, "tariffs": tariffs}
    snapshot = ([{"role": "energy_meter", "entity": "sensor.meter", "state": str(reading),
                  "unit": "kWh", "since": at(-120)}] if meter else [])
    return Stream(base, capsys, "chargepoint", sid, cfg, snapshot)


def charge(v, start, soc_from, soc_to, minutes=60, meter=None, reading=None, kwh=None):
    """A charge: the state turns to Charging, SoC climbs every 10 minutes,
    the meter with it, then Done."""
    v.state(start, "charging_state", "Charging")
    steps = minutes // 10
    for i in range(1, steps + 1):
        v.state(start + 10 * i, "soc", round(soc_from + (soc_to - soc_from) * i / steps, 1), "%")
        if meter is not None:
            meter.state(start + 10 * i, "energy_meter", round(reading + kwh * i / steps, 3), "kWh")
    v.state(start + minutes + 1, "charging_state", "Done")
    return start + minutes + 1


V = Subject("vehicle", "a7c1")
HOME_TARIFFS = [{"from": "2026-01-01", "eur_per_kwh": 0.30}]


def test_a_home_charge_with_a_meter(tmp_path, capsys):
    home = chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(tmp_path, capsys)
    end = charge(v, 0, 40, 80, meter=home, reading=5000.0, kwh=6.5)
    v.heartbeat(end + 30)
    s, = charging.derive_from(tmp_path, V)
    assert (s.start, s.end, s.source, s.quality) == (at(0), at(61), "charging_state", "measured")
    assert (s.soc_start_pct, s.soc_end_pct, s.delta_soc_pct) == (40, 80, 40)
    assert s.chargepoint == "home" and s.chargepoint_name == "Home"
    assert s.battery_kwh == 5.88                                  # 40 % of 14.7 kWh, estimated
    assert (s.grid_kwh, s.grid_kwh_quality, s.grid_kwh_source) == (6.5, "measured", "meter")
    assert s.meter_attributable is True
    assert (s.tariff_eur_per_kwh, s.cost_eur, s.cost_quality) == (0.30, 1.95, "measured")
    assert s.kwh_per_pct == 0.1625                                # LAD-09
    assert s.charging_loss_kwh == 0.62
    assert s.movements_while_charging == 0


def test_a_work_charge_at_tariff_0_costs_nothing_even_without_energy(tmp_path, capsys):
    chargepoint(tmp_path, capsys, "work", WORK, [{"from": "2026-01-01", "eur_per_kwh": 0}], meter=False)
    v = vehicle(tmp_path, capsys, where=WORK, parameters={"battery_net_kwh": None})
    end = charge(v, 0, 30, 70)
    v.heartbeat(end + 30)
    s, = charging.derive_from(tmp_path, V)
    assert s.chargepoint == "work" and s.meter_attributable is None
    assert s.battery_kwh is None and s.grid_kwh is None            # no capacity: no energy at all
    assert (s.cost_eur, s.cost_quality) == (0.0, "measured")
    assert s.kwh_per_pct is None and s.charging_loss_kwh is None


def test_an_unmetered_wallbox_costs_through_the_loss_factor(tmp_path, capsys):
    chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS, meter=False)
    v = vehicle(tmp_path, capsys)
    end = charge(v, 0, 40, 80)
    v.heartbeat(end + 30)
    s, = charging.derive_from(tmp_path, V)
    assert (s.grid_kwh, s.grid_kwh_quality, s.grid_kwh_source) == (6.586, "estimated", "loss_factor")
    assert (s.cost_eur, s.cost_quality) == (1.98, "estimated")
    assert s.kwh_per_pct is None and s.charging_loss_kwh is None  # never from the factor itself


def test_a_foreign_charge(tmp_path, capsys):
    chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(tmp_path, capsys, where=AWAY, soc=20)
    end = charge(v, 0, 20, 70)
    v.heartbeat(end + 30)
    s, = charging.derive_from(tmp_path, V)
    assert s.chargepoint == "foreign" and s.chargepoint_name is None
    assert s.position["latitude"] == AWAY[0]
    assert (s.grid_kwh, s.grid_kwh_source) == (8.232, "loss_factor")
    assert s.tariff_eur_per_kwh is None and s.cost_eur is None and s.cost_quality is None


def test_two_vehicles_at_one_meter_make_it_unattributable(tmp_path, capsys):
    home = chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(tmp_path, capsys)
    w = vehicle(tmp_path, capsys, sid="b2d4")
    charge(w, 30, 50, 60, minutes=20)                 # the other one, overlapping
    end = charge(v, 0, 40, 80, meter=home, reading=5000.0, kwh=9.0)
    v.heartbeat(end + 30)
    w.heartbeat(end + 30)
    s, = charging.derive_from(tmp_path, V)
    assert s.meter_attributable is False
    assert (s.grid_kwh, s.grid_kwh_source, s.grid_kwh_quality) == (6.586, "loss_factor", "estimated")
    assert s.kwh_per_pct is None


def test_another_vehicle_still_charging_counts_too(tmp_path, capsys):
    home = chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(tmp_path, capsys)
    w = vehicle(tmp_path, capsys, sid="b2d4")
    w.state(30, "charging_state", "Charging")         # and never done
    end = charge(v, 0, 40, 80, meter=home, reading=5000.0, kwh=9.0)
    v.heartbeat(end + 30)
    s, = charging.derive_from(tmp_path, V)
    assert s.meter_attributable is False


def test_a_sensor_dropout_mid_charge_holds(tmp_path, capsys):
    home = chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(tmp_path, capsys)
    v.state(0, "charging_state", "Charging")
    v.state(10, "soc", 50, "%")
    home.state(10, "energy_meter", 5001.5, "kWh")
    v.state(15, "charging_state", "unavailable")      # says nothing: the session holds
    v.state(20, "soc", 60, "%")
    v.state(25, "charging_state", "Charging")         # back, no new session
    v.state(30, "soc", 70, "%")
    home.state(30, "energy_meter", 5004.4, "kWh")
    v.state(31, "charging_state", "Done")
    v.heartbeat(60)
    s, = charging.derive_from(tmp_path, V)
    assert (s.start, s.end, s.delta_soc_pct, s.grid_kwh) == (at(0), at(31), 30, 4.4)


def test_the_soc_fallback_without_a_charging_state(tmp_path, capsys):
    v = vehicle(tmp_path, capsys, charging_state=False, soc=40)
    for i, value in enumerate((41, 45, 50, 55), start=1):  # the first rise comes hours after 40 was reported
        v.state(10 * i, "soc", value, "%")
    v.state(60, "soc", 55.5, "%")                      # too late to join: 20 min is fine, but…
    v.state(200, "soc", 56, "%")                       # …two hours later, a second run of 0.5 %: no session
    v.heartbeat(260)
    found = charging.derive_from(tmp_path, V)
    assert [(s.start, s.end, s.source, s.soc_start_pct, s.soc_end_pct) for s in found] == [
        (at(10), at(60), "soc", 40, 55.5)]
    assert found[0].chargepoint == "foreign"


def test_the_soc_fallback_waits_for_its_run_to_be_broken(tmp_path, capsys):
    v = vehicle(tmp_path, capsys, charging_state=False, soc=40)
    v.state(10, "soc", 45, "%").state(20, "soc", 50, "%")
    assert charging.derive_from(tmp_path, V) == []       # still rising, as far as anyone knows
    v.heartbeat(49)
    assert charging.derive_from(tmp_path, V) == []       # 29 minutes: not yet T_still
    v.heartbeat(50)
    assert [s.end for s in charging.derive_from(tmp_path, V)] == [at(20)]


def test_a_soc_rise_while_driving_is_no_session(tmp_path, capsys):
    v = vehicle(tmp_path, capsys, charging_state=False, soc=40)
    v.state(10, "odometer", 1005, "km").state(10, "soc", 43, "%")
    v.state(20, "odometer", 1010, "km").state(20, "soc", 46, "%")   # regeneration downhill
    v.heartbeat(90)
    assert charging.derive_from(tmp_path, V) == []


def test_a_session_across_a_capture_gap_is_incomplete(tmp_path, capsys):
    v = vehicle(tmp_path, capsys)
    v.state(0, "charging_state", "Charging").state(10, "soc", 50, "%")
    v.run("stop", *v.b, "--t", at(15), "--reason", "shutdown")
    v.run("start", *v.b, "--t", at(50), "--homeassistant", "2026.9.4", "--snapshot", json.dumps([
        {"role": "charging_state", "entity": "sensor.c", "state": "Done", "since": at(40)},
        {"role": "soc", "entity": "sensor.s", "state": "80", "unit": "%", "since": at(40)}]))
    v.heartbeat(90)
    s, = charging.derive_from(tmp_path, V)
    assert (s.start, s.end, s.quality, s.soc_end_pct) == (at(0), at(40), "incomplete", 80)


def test_movements_while_charging_are_reported(tmp_path, capsys):
    v = vehicle(tmp_path, capsys)
    v.state(0, "charging_state", "Charging")
    v.state(5, "odometer", 1001, "km")                 # a value measured before the plug-in, late
    v.state(30, "soc", 60, "%").state(31, "charging_state", "Done").heartbeat(60)
    s, = charging.derive_from(tmp_path, V)
    assert s.movements_while_charging == 1


def test_the_tariff_of_the_sessions_time(tmp_path, capsys):
    tariffs = [{"from": "2026-01-01", "eur_per_kwh": 0.30}, {"from": "2026-10-10", "eur_per_kwh": 0.20}]
    home = chargepoint(tmp_path, capsys, "home", HOME, tariffs)
    v = vehicle(tmp_path, capsys)
    charge(v, 0, 40, 50, minutes=10, meter=home, reading=5000.0, kwh=2.0)            # 9 October
    end = charge(v, 24 * 60, 50, 60, minutes=10, meter=home, reading=5002.0, kwh=2.0)  # 10 October
    v.heartbeat(end + 30)
    assert [s.cost_eur for s in charging.derive_from(tmp_path, V)] == [0.6, 0.4]


def test_incremental_line_by_line_equals_one_rebuild(tmp_path, capsys):
    """ADR-0009, point 6, for charging: the vehicle's stream replayed line
    by line, the charge point's present throughout."""
    live = tmp_path / "live"
    home = chargepoint(live, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(live, capsys)
    end = charge(v, 0, 40, 60, meter=home, reading=5000.0, kwh=3.0)
    end = charge(v, end + 120, 60, 90, meter=home, reading=5003.0, kwh=4.5)
    v.heartbeat(end + 30)

    replay = tmp_path / "replay"
    cp_src = live / "chargepoint-home" / "l0" / "2026-10.jsonl"
    cp_dst = replay / "chargepoint-home" / "l0" / "2026-10.jsonl"
    cp_dst.parent.mkdir(parents=True)
    cp_dst.write_bytes(cp_src.read_bytes())
    src = live / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst = replay / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst.parent.mkdir(parents=True)
    with open(dst, "a") as f:
        for line in src.read_text().splitlines(keepends=True):
            f.write(line)
            f.flush()
            l1.incremental(replay, V)

    l1.rebuild(live, V)
    batch = (l1.l1_dir(live, V) / "charging-sessions.jsonl").read_bytes()
    assert batch == (l1.l1_dir(replay, V) / "charging-sessions.jsonl").read_bytes()
    assert [e["grid_kwh"] for e in l1.read(live, V, "charging")] == [3.0, 4.5]


def test_the_verb(tmp_path, capsys):
    home = chargepoint(tmp_path, capsys, "home", HOME, HOME_TARIFFS)
    v = vehicle(tmp_path, capsys)
    end = charge(v, 0, 40, 80, meter=home, reading=5000.0, kwh=6.5)
    v.heartbeat(end + 30)
    assert main(["derive", "charging", *v.b]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["cost_eur"] == 1.95 and err.strip() == "1 charging session(s)"
    assert main(["derive", "charging", *v.b, "--write"]) == 0
    assert "1 completed charging session(s)" in capsys.readouterr().out
    assert [e["kind"] for e in l1.read(tmp_path, V, "charging")] == ["charging"]
    assert l1.read_manifest(tmp_path, V)["through"] == {"charging": at(61)}
    assert main(["derive", "charging", *home.b]) == 2      # a charge point has no sessions
