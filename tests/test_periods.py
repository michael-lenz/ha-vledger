# SPDX-License-Identifier: BSD-3-Clause
"""Periods and their metrics (ADR-0014), on streams built through the
verbs with answers worked out by hand."""

import json
import shutil

import pytest

from test_charging import HOME, HOME_TARIFFS, Stream, at, charge, chargepoint
from vledger import clock, l1, periods, series
from vledger import config as vconfig
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")
BERLIN = vconfig.zone_of({"time_zone": "Europe/Berlin"})


def vehicle(base, capsys, parameters=None, time_zone=None, charging_state=True):
    """Odometer, position, SoC, fuel level and charging state: a plug-in
    hybrid with a 10 kWh battery and a 40 L tank, standing at home."""
    roles = {"odometer": {"entity": "sensor.o"}, "position": {"entity": "device_tracker.v"},
             "soc": {"entity": "sensor.s"}, "fuel_level": {"entity": "sensor.f"}}
    if charging_state:
        roles["charging_state"] = {"entity": "sensor.c", "map": {"charging": ["Charging"]}}
    cfg = vconfig.vehicle("V", roles, dict({
        "fuel": "petrol", "battery_net_kwh": 10, "tank_capacity_l": 40,
        "charging_loss_factor": 1.0, "charge_cycles_start": 100, "tank_fills_start": 10,
    }, **(parameters or {})), {"t_still_s": 1800, "heartbeat_s": 3600, "t_settle_s": 360},
        time_zone=time_zone)
    snapshot = [
        {"role": "odometer", "entity": "sensor.o", "state": "1000", "unit": "km", "since": at(-120)},
        {"role": "position", "entity": "device_tracker.v", "state": "home", "since": at(-120),
         "attrs": {"latitude": HOME[0], "longitude": HOME[1], "gps_accuracy": 0}},
        {"role": "soc", "entity": "sensor.s", "state": "50", "unit": "%", "since": at(-120)},
        {"role": "fuel_level", "entity": "sensor.f", "state": "20", "unit": "L", "since": at(-120)},
    ]
    if charging_state:
        snapshot.append({"role": "charging_state", "entity": "sensor.c", "state": "Idle", "since": at(-120)})
    return Stream(base, capsys, "vehicle", "a7c1", cfg, snapshot)


def trip(v, start, odo_from, km, fuel=None, soc=None):
    """Three odometer samples 15 minutes apart; fuel and SoC as they stand
    at the end."""
    for i in (1, 2, 3):
        v.state(start + 15 * i, "odometer", odo_from + km * i / 3, "km")
    if fuel is not None:
        v.state(start + 45, "fuel_level", fuel, "L")
    if soc is not None:
        v.state(start + 45, "soc", soc, "%")


def receipt(base, capsys, start, litres, price, full=True):
    argv = ["receipt", "add", "refuelling", "--base", str(base), "--vehicle", "a7c1",
            "--from-candidate", at(start), "--quantity-l", str(litres),
            "--total-price", str(price), "--t", at(start + 1)]
    assert main([*argv, "--full" if full else "--partial"]) == 0
    capsys.readouterr()


def a_day(base, capsys, **kw):
    """Two trips of 30 km, a refuelling after each, a metered charge at home
    between them:

    - trip 1, odometer 1000 → 1030; fuel 20 → 17 L, SoC 50 → 40 %
    - refuelling at +80: 17 → 37 L; receipt 20 L, 35.00, full
    - charge +100 to +161: SoC 40 → 60 %, meter 2.5 kWh at 0.30
    - trip 2, odometer 1030 → 1060; fuel 37 → 34 L, SoC 60 → 50 %
    - refuelling at +280: 34 → 37 L; receipt 3 L, 5.40, full
    - the stream's last line at +400
    """
    v = vehicle(base, capsys, **kw)
    home = chargepoint(base, capsys, "home", HOME, HOME_TARIFFS)
    trip(v, 0, 1000, 30, fuel=17, soc=40)
    v.state(80, "fuel_level", 37, "L")
    charge(v, 100, 40, 60, minutes=60, meter=home, reading=5000.0, kwh=2.5)
    home.heartbeat(400)
    trip(v, 200, 1030, 30, fuel=34, soc=50)
    v.state(280, "fuel_level", 37, "L")
    v.heartbeat(400)
    receipt(base, capsys, 80, 20, 35.00)
    receipt(base, capsys, 280, 3, 5.40)
    return v


def by_period(lines):
    return {x["period"]: x for x in lines}


def test_a_day_by_hand(tmp_path, capsys):
    a_day(tmp_path, capsys)
    lines = periods.derive_from(tmp_path, V)
    assert [x["period"] for x in lines] == ["month", "year", "rolling", "lifetime"]
    m = by_period(lines)["month"]
    assert (m["start"], m["end"], m["open"]) == ("2026-10-01T00:00:00.000Z", "2026-11-01T00:00:00.000Z", True)
    assert m["kind"] == "period" and m["subject"] == "a7c1" and m["gaps"] == 0
    assert m["distance_km"] == 60 and m["distance_km_quality"] == "measured"
    # Purchase: what the receipts say.
    assert m["fuel_purchased_l"] == 23 and m["fuel_purchased_l_quality"] == "receipt"
    assert m["fuel_cost_eur"] == 40.40 and m["fuel_cost_eur_quality"] == "receipt"
    # Consumed: 23 bought + (20 at capture's start − 37 at its last line).
    assert m["fuel_consumed_l"] == 6 and m["fuel_consumed_l_quality"] == "estimated"
    assert m["fuel_level_corrected"] is True
    assert m["grid_kwh"] == 2.5 and m["grid_kwh_quality"] == "measured"
    assert m["electricity_cost_eur"] == 0.75
    # 20 % of 10 kWh charged; SoC 50 % at both ends: nothing to correct.
    assert m["battery_kwh"] == 2 and m["soc_corrected"] is True
    assert m["grid_kwh_per_100km"] == pytest.approx(4.167)
    assert m["battery_kwh_per_100km"] == pytest.approx(3.333)
    # 6 L at the mean receipt price 40.40 / 23, over 60 km.
    assert m["fuel_eur_per_100km"] == pytest.approx(6 * 40.40 / 23 / 60 * 100, abs=1e-3)
    assert m["electricity_eur_per_100km"] == 1.25
    assert m["eur_per_100km"] == pytest.approx(m["fuel_eur_per_100km"] + 1.25, abs=1e-3)
    # VER-05: 2 kWh against 6 L × 8.9 kWh/L; weighted 0.85 and 0.28.
    assert m["electric_energy_share"] == pytest.approx(2 / (2 + 53.4), abs=1e-4)
    assert m["electric_distance_share"] == pytest.approx(1.7 / (1.7 + 0.28 * 53.4), abs=1e-4)
    assert m["electric_distance_share_quality"] == "estimated"
    assert m["charge_cycles"] == 0.2 and m["tank_fills"] == 0.575
    assert m["quality"] == "estimated"
    assert "consumption_l_per_100km" not in m

    life = by_period(lines)["lifetime"]
    assert (life["start"], life["end"], life["open"]) == (at(-60), at(400), False)
    assert life["charge_cycles"] == 100.2 and life["tank_fills"] == 10.575
    # Full to full: 3 L over the 30 km between the two refuellings.
    assert life["consumption_l_per_100km"] == 10 and life["consumption_quality"] == "receipt"
    assert (life["consumption_from"], life["consumption_to"]) == (at(80), at(280))
    assert life["consumption_receipts"] == 2 and life["consumption_error_pct"] == 0

    rolling = by_period(lines)["rolling"]
    assert rolling["end"] == at(400) and rolling["distance_km"] == 60


def test_a_dropout_at_the_last_line_holds_the_stock(tmp_path, capsys):
    """ISSUE-0062, ADR-0046: a sensor that is unavailable at the open
    periods' end holds its last number, so the stock correction and every
    figure on it stay what they were."""
    v = a_day(tmp_path, capsys)
    before = by_period(periods.derive_from(tmp_path, V))["month"]
    assert before["fuel_level_corrected"] and before["soc_corrected"]
    v.state(410, "fuel_level", "unavailable")
    v.state(411, "soc", "unknown")
    during = by_period(periods.derive_from(tmp_path, V))["month"]
    assert during["fuel_level_corrected"] and during["soc_corrected"]
    assert {k: during[k] for k in periods.METRICS} == {k: before[k] for k in periods.METRICS}
    v.state(412, "fuel_level", 37, "L").state(413, "soc", 50, "%")
    after = by_period(periods.derive_from(tmp_path, V))["month"]
    assert {k: after[k] for k in periods.METRICS} == {k: before[k] for k in periods.METRICS}


def test_the_calendar_is_local_to_the_vehicle(tmp_path, capsys):
    first, last = "2026-10-09T10:00:00.000Z", "2027-01-05T10:00:00.000Z"
    spans = periods.spans(first, last, BERLIN, 30)
    months = [(s, e) for p, s, e in spans if p == "month"]
    # Summer time in October, winter time from its last Sunday on.
    assert months[0] == ("2026-09-30T22:00:00.000Z", "2026-10-31T23:00:00.000Z")
    assert months[-1] == ("2026-12-31T23:00:00.000Z", "2027-01-31T23:00:00.000Z")
    assert len(months) == 4
    years = [(s, e) for p, s, e in spans if p == "year"]
    assert years == [("2025-12-31T23:00:00.000Z", "2026-12-31T23:00:00.000Z"),
                     ("2026-12-31T23:00:00.000Z", "2027-12-31T23:00:00.000Z")]
    assert spans[-2] == ("rolling", "2026-12-06T10:00:00.000Z", last)
    assert spans[-1] == ("lifetime", first, last)


def test_the_zone_comes_from_the_configuration(tmp_path, capsys):
    a_day(tmp_path, capsys, time_zone="Europe/Berlin")
    m = by_period(periods.derive_from(tmp_path, V))["month"]
    assert m["start"] == "2026-09-30T22:00:00.000Z"
    with pytest.raises(ValueError, match="unknown time zone"):
        vconfig.vehicle("V", {"odometer": {"entity": "sensor.o"}}, time_zone="Mars/Olympus")
    assert "time_zone" not in vconfig.vehicle("V", {"odometer": {"entity": "sensor.o"}})


def test_a_straddling_trip_moves_the_stock_reading_to_its_end():
    spanning = [{"start": "2026-10-31T22:50:00.000Z", "end": "2026-10-31T23:40:00.000Z"}]
    first, last = "2026-10-01T00:00:00.000Z", "2026-11-30T00:00:00.000Z"
    assert periods.reading_time("2026-10-31T23:00:00.000Z", spanning, first, last) == spanning[0]["end"]
    assert periods.reading_time("2026-10-31T22:00:00.000Z", spanning, first, last) == "2026-10-31T22:00:00.000Z"
    # Kept within the stream at both ends.
    assert periods.reading_time("2026-09-01T00:00:00.000Z", [], first, last) == first
    assert periods.reading_time("2026-12-01T00:00:00.000Z", [], first, last) == last


def test_an_event_not_yet_in_l1_moves_the_stock_reading_back_to_its_start():
    spanning = [{"start": "2026-10-31T22:50:00.000Z", "end": "2026-10-31T23:40:00.000Z"}]
    first, last = "2026-10-01T00:00:00.000Z", "2026-11-30T00:00:00.000Z"
    # Begun before the boundary and not an event: read at its start (ADR-0033).
    assert periods.reading_time("2026-11-30T00:00:00.000Z", [], first, last,
                                ["2026-11-29T23:00:00.000Z"]) == "2026-11-29T23:00:00.000Z"
    # Begun after the boundary: nothing to move.
    assert periods.reading_time("2026-11-01T00:00:00.000Z", [], first, last,
                                ["2026-11-29T23:00:00.000Z"]) == "2026-11-01T00:00:00.000Z"
    # Moved forward past a completed event first, then back to the earliest
    # incomplete one begun by then.
    assert periods.reading_time("2026-10-31T23:00:00.000Z", spanning, first, last,
                                ["2026-10-31T23:50:00.000Z"]) == spanning[0]["end"]
    assert periods.reading_time("2026-10-31T23:00:00.000Z", spanning, first, last,
                                ["2026-10-31T23:45:00.000Z", "2026-10-31T23:42:00.000Z"]) \
        == "2026-10-31T23:40:00.000Z"
    assert periods.reading_time("2026-10-31T23:50:00.000Z", spanning, first, last,
                                ["2026-10-31T23:45:00.000Z"]) == "2026-10-31T23:45:00.000Z"


def test_the_move_back_stops_at_the_end_of_a_completed_event_it_overlaps():
    """ADR-0048, point 2: a session begun a millisecond before the trip's
    end does not pull the reading before the trip's settled values."""
    spanning = [{"start": "2026-10-31T22:50:00.000Z", "end": "2026-10-31T23:40:00.000Z"}]
    first, last = "2026-10-01T00:00:00.000Z", "2026-11-30T00:00:00.000Z"
    assert periods.reading_time("2026-10-31T23:00:00.000Z", spanning, first, last,
                                ["2026-10-31T23:39:59.999Z"]) == spanning[0]["end"]
    # Begun before the completed event: no overlap to speak of, read at its start.
    assert periods.reading_time("2026-10-31T23:00:00.000Z", spanning, first, last,
                                ["2026-10-31T22:40:00.000Z"]) == "2026-10-31T22:40:00.000Z"


def test_a_per_cycle_drive_under_way_is_not_read_into_the_stock(tmp_path, capsys):
    """ADR-0048, point 1: the SoC a per-cycle vehicle reports while it
    drives is read no later than the departure, so the figures hold still
    and step once when the trip completes."""
    from test_trips import PER_CYCLE, Builder, upload
    roles = {"trip_distance": {"entity": "sensor.trip"}, "soc": {"entity": "sensor.s"},
             "lock": {"entity": "lock.v", "map": {"locked": ["locked"]}},
             "engine": {"entity": "binary_sensor.engine", "map": {"running": ["on"]}}}
    b = Builder(tmp_path, capsys, roles=roles, parameters=dict(PER_CYCLE, battery_net_kwh=10))
    b.heartbeat(0)
    b.state(5, "lock", "unlocked", before=4).state(6, "engine", "on", before=4)
    for i, soc in enumerate((78, 76, 74)):
        b.state(8 + 2 * i, "soc", soc, "%", before=6 + 2 * i)
    b.heartbeat(14)
    m = month(tmp_path)
    assert m["battery_kwh"] == 0 and m["soc_corrected"] is True and m["distance_km"] == 0
    b.state(52, "engine", "off", before=50)
    upload(b, 52, 30.0, 1030)
    b.heartbeat(55)
    m = month(tmp_path)
    assert m["battery_kwh"] == 0 and m["distance_km"] == 0                # a leg, not yet complete
    b.heartbeat(90)
    m = month(tmp_path)
    assert m["distance_km"] == 30 and m["battery_kwh"] == 0.6             # 80 → 74 % of 10 kWh


def test_the_fuel_a_period_consumed_is_its_trips_trip_computer_sum_where_complete(tmp_path, capsys):
    """ADR-0049, point 1: two legs the computer measured at 1.75 and 0.8 L
    while the level fell 3.5 L and 1 L; one trip the computer missed turns
    the period back to the level's stock."""
    from test_trips import CYCLE_ROLES, PER_CYCLE, Builder, upload
    from test_trips import at as t_at  # the builder's own clock
    roles = dict(CYCLE_ROLES, trip_consumption={"entity": "sensor.avg"}, fuel_level={"entity": "sensor.f"})
    b = Builder(tmp_path, capsys, roles=roles, parameters=dict(
        PER_CYCLE, fuel="petrol", fuel_level_resolution_l=1.0, tank_capacity_l=40))
    b.state(-30, "trip_distance", 100.0, "km").state(-30, "trip_consumption", 6.0, "L/100 km")
    b.heartbeat(0)
    b.state(10, "lock", "unlocked", before=9).state(12, "lock", "locked", before=11)
    upload(b, 40, 125.0, 1025)
    b.state(40, "trip_consumption", 6.2, "L/100 km", before=38)
    b.state(43, "fuel_level", 17.0, "L", before=38)
    b.heartbeat(100)
    b.state(160, "lock", "unlocked", before=159)
    upload(b, 180, 10.0, 1035)
    b.state(180, "trip_consumption", 8.0, "L/100 km", before=178)
    b.state(183, "fuel_level", 16.0, "L", before=43)
    b.heartbeat(230)
    m = month(tmp_path)
    assert m["distance_km"] == 35 and m["fuel_level_corrected"] is True
    assert (m["fuel_consumed_l"], m["fuel_consumed_l_quality"]) == (2.55, "measured")   # not 20.5 − 16 = 4.5
    assert m["fuel_consumed_source"] == "trip_computer"
    # A late upload across a capture gap joins the second trip, and its
    # average cannot be read across the gap — the stock is the figure.
    b.heartbeat(290)
    b.state(300, "lock", "unlocked", before=299)
    b.run("stop", *b.b, "--t", t_at(310), "--reason", "reload")
    b.run("start", *b.b, "--t", t_at(312), "--homeassistant", "2026.9.4", "--snapshot", "[]")
    upload(b, 320, 30.0, 1055)
    b.state(323, "fuel_level", 14.0, "L", before=183)
    b.heartbeat(380)
    m = month(tmp_path)
    assert m["distance_km"] == 55
    assert (m["fuel_consumed_l"], m["fuel_consumed_l_quality"]) == (6.5, "estimated")   # 20.5 − 14
    assert m["fuel_consumed_source"] == "fuel_level"


def charged_then_driven(base, capsys, charging_state=True):
    """A metered charge at home, SoC 50 → 60 % at 0.30 (1.25 kWh, cost
    0.38 as the session rounds it), then a 30 km trip back to 50 %."""
    v = vehicle(base, capsys, charging_state=charging_state)
    home = chargepoint(base, capsys, "home", HOME, HOME_TARIFFS)
    if charging_state:
        charge(v, 0, 50, 60, minutes=60, meter=home, reading=5000.0, kwh=1.25)
    else:
        for i in range(1, 7):
            v.state(10 * i, "soc", round(50 + 10 * i / 6, 1), "%")
            home.state(10 * i, "energy_meter", round(5000 + 1.25 * i / 6, 3), "kWh")
    trip(v, 100, 1000, 30, soc=50)
    return v, home


def month(base):
    return by_period(periods.derive_from(base, V))["month"]


def test_a_charge_under_way_at_the_last_line_is_not_read_into_the_stock(tmp_path, capsys):
    v, home = charged_then_driven(tmp_path, capsys)
    v.state(200, "charging_state", "Charging")
    for i in range(1, 7):
        v.state(200 + 10 * i, "soc", 50 + 5 * i, "%")
        home.state(200 + 10 * i, "energy_meter", 5001.25 + 0.5 * i, "kWh")
    v.heartbeat(265)
    home.heartbeat(265)
    m = month(tmp_path)
    # SoC read at the charge's start, 50 % as at capture's: 1 kWh for 30 km,
    # not 1 kWh − 3 kWh of a charge nothing counts yet (ISSUE-0046).
    assert m["battery_kwh"] == 1 and m["soc_corrected"] is True
    assert m["electricity_eur_per_100km"] == pytest.approx(m["electricity_cost_eur"] / 30 * 100, abs=1e-3)
    assert m["eur_per_100km"] > 0
    # Done, and the meter's stream past its end: the charge and its stock
    # change enter together, and the battery side stays what was used.
    v.state(271, "charging_state", "Done")
    v.heartbeat(300)
    home.heartbeat(300)
    m = month(tmp_path)
    assert m["grid_kwh"] == 4.25 and m["battery_kwh"] == 1


def test_a_charge_waiting_for_the_meter_is_not_read_into_the_stock(tmp_path, capsys):
    v, home = charged_then_driven(tmp_path, capsys)
    v.state(200, "charging_state", "Charging")
    v.state(230, "soc", 80, "%")
    v.state(261, "charging_state", "Done")
    home.heartbeat(250)     # the meter's stream short of the end, and not silent:
    v.heartbeat(300)        # the session waits for it (ADR-0027)
    assert len(list(l1.derive(tmp_path, V, "charging"))) == 1
    assert month(tmp_path)["battery_kwh"] == 1


def test_a_trip_not_yet_complete_is_not_read_into_the_stock(tmp_path, capsys):
    v, home = charged_then_driven(tmp_path, capsys)
    trip(v, 200, 1030, 10, soc=30)          # stops at +245; complete at +275
    v.heartbeat(255)
    home.heartbeat(255)
    m = month(tmp_path)
    assert m["distance_km"] == 30 and m["battery_kwh"] == 1
    v.heartbeat(300)
    m = month(tmp_path)
    assert m["distance_km"] == 40 and m["battery_kwh"] == 3


def test_a_soc_run_not_yet_over_is_not_read_into_the_stock(tmp_path, capsys):
    v, home = charged_then_driven(tmp_path, capsys, charging_state=False)
    for i in range(1, 4):
        v.state(200 + 10 * i, "soc", 50 + 5 * i, "%")
    v.heartbeat(235)
    home.heartbeat(235)
    assert month(tmp_path)["battery_kwh"] == 1


def replayed_line_by_line(live, replay):
    """The vehicle's stream of ``live`` written again line by line, the live
    writer run after each, its periods equal to a fresh derivation of the
    same lines every time."""
    shutil.copytree(live / "chargepoint-home", replay / "chargepoint-home")
    src = live / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst = replay / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst.parent.mkdir(parents=True)
    with open(dst, "a") as f:
        for line in src.read_text().splitlines(keepends=True):
            f.write(line)
            f.flush()
            l1.incremental(replay, V)
            assert list(l1.read(replay, V, "period")) == periods.derive_from(replay, V)
    return replay


def test_line_by_line_the_periods_equal_a_fresh_derivation_across_completions(tmp_path, capsys):
    """Each line appended and the live writer run equals deriving the same
    prefix afresh — while the charge and the trip are under way, and at the
    moment each becomes an event."""
    live = tmp_path / "live"
    v, home = charged_then_driven(live, capsys)
    v.state(200, "charging_state", "Charging")
    for i in range(1, 4):
        v.state(200 + 10 * i, "soc", 50 + 10 * i, "%")
    v.state(231, "charging_state", "Done")
    trip(v, 300, 1030, 10, soc=70)
    v.heartbeat(400)
    home.heartbeat(400)
    replay = replayed_line_by_line(live, tmp_path / "replay")
    assert len(list(l1.read(replay, V, "charging"))) == 2 and len(list(l1.read(replay, V, "trip"))) == 2
    # 1 kWh each trip: 1 + 3 charged, less the SoC risen from 50 to 70 %.
    assert month(replay)["battery_kwh"] == 2


def test_a_refuelling_moves_the_stock_reading_like_the_other_events():
    first, last = "2026-10-01T00:00:00.000Z", "2026-11-30T00:00:00.000Z"
    fill = {"start": "2026-10-31T22:58:00.000Z", "end": "2026-10-31T23:01:00.000Z",
            "settled_at": "2026-10-31T23:07:00.000Z"}
    own = {"start": "2026-10-31T22:30:00.000Z", "end": "2026-10-31T23:30:00.000Z"}
    moving = periods.spanning({"refuelling": [fill, own]})
    # Forward to where its level after was read (ADR-0034, point 1); a
    # receipt that met nothing read no level and moves nothing.
    assert moving == [{"start": fill["start"], "end": fill["settled_at"]}]
    assert periods.reading_time("2026-10-31T23:00:00.000Z", moving, first, last) == fill["settled_at"]


def test_a_refuelling_still_settling_is_not_read_into_the_stock(tmp_path, capsys):
    """A fill three minutes before the last line, T_settle six: the level
    is read as it stood before the rise (ADR-0034, point 2), then the fill
    enters whole once settled."""
    v = vehicle(tmp_path, capsys)
    trip(v, 0, 1000, 30, fuel=17)
    v.state(100, "fuel_level", 37, "L")
    v.heartbeat(103)
    m = month(tmp_path)
    assert m["fuel_purchased_l"] == 0 and m["fuel_consumed_l"] == 3     # not −17 (ISSUE-0047)
    v.heartbeat(110)
    m = month(tmp_path)
    assert m["fuel_purchased_l"] == 20 and m["fuel_consumed_l"] == 3


def test_line_by_line_a_refuelling_settles_into_the_periods_as_derived_afresh(tmp_path, capsys):
    live = tmp_path / "live"
    v, home = charged_then_driven(live, capsys)
    v.state(150, "fuel_level", 19, "L")     # read at rest after the trip
    v.state(200, "fuel_level", 30, "L")
    v.state(202, "fuel_level", 37, "L")     # the pump seen in two steps
    v.heartbeat(240)
    home.heartbeat(240)
    replay = replayed_line_by_line(live, tmp_path / "replay")
    assert len(list(l1.read(replay, V, "refuelling"))) == 1
    m = month(replay)
    # 18 L bought, 1 L used: 20 at capture's start, 37 at the last line.
    assert m["fuel_purchased_l"] == 18 and m["fuel_consumed_l"] == 1


def reloaded(v, stop_at, start_at, soc_since, fuel_since, reason="reload"):
    """Capture stopped and started again with a snapshot of the day as it
    stands — every value repeated, each role's ``since`` as given."""
    v.run("stop", *v.b, "--t", at(stop_at), "--reason", reason)
    snapshot = [
        {"role": "odometer", "entity": "sensor.o", "state": "1060", "unit": "km", "since": at(245)},
        {"role": "position", "entity": "device_tracker.v", "state": "home", "since": at(-120),
         "attrs": {"latitude": HOME[0], "longitude": HOME[1], "gps_accuracy": 0}},
        {"role": "soc", "entity": "sensor.s", "state": "50", "unit": "%", "since": soc_since},
        {"role": "fuel_level", "entity": "sensor.f", "state": "37", "unit": "L", "since": fuel_since},
        {"role": "charging_state", "entity": "sensor.c", "state": "Done", "since": at(161)},
    ]
    v.run("start", *v.b, "--t", at(start_at), "--homeassistant", "2026.9.4",
          "--snapshot", json.dumps(snapshot))


STOCK = ("soc_corrected", "fuel_level_corrected", "fuel_consumed_l", "battery_kwh",
         "electric_distance_share")


def test_a_restart_whose_snapshot_held_through_the_gap_keeps_the_stock_correction(tmp_path, capsys):
    """ADR-0035 (ISSUE-0048): the snapshot repeats every value with a since
    before the stop, so each is a reading at the start line, and the open
    month's figures stay what they were — for as long as nothing changes."""
    v = a_day(tmp_path, capsys)
    before = month(tmp_path)
    assert before["soc_corrected"] and before["fuel_level_corrected"]
    reloaded(v, 401, 401, soc_since=at(245), fuel_since=at(280))
    v.run("heartbeat", *v.b, "--t", at(402), "--lines", "0")
    m = month(tmp_path)
    assert m["gaps"] == 1
    assert {k: m[k] for k in STOCK} == {k: before[k] for k in STOCK}
    v.heartbeat(440)
    assert {k: month(tmp_path)[k] for k in STOCK} == {k: before[k] for k in STOCK}
    assert [x.t for x in series.load(tmp_path, V).series["soc"] if x.t >= at(401)] == [at(401)]
    # The role's next change moves the stock as it always did.
    v.state(441, "soc", 49, "%")
    assert month(tmp_path)["battery_kwh"] == 2.1


def test_a_snapshot_entry_whose_since_lies_inside_the_gap_is_no_reading_after_it(tmp_path, capsys):
    """ADR-0035, the limit: a state written inside the gap — Home Assistant
    restarted, or the entity re-created — vouches for nothing across it,
    and that role's correction waits for its next change as before."""
    v = a_day(tmp_path, capsys)
    reloaded(v, 401, 405, soc_since=at(403), fuel_since=at(280), reason="shutdown")
    v.run("heartbeat", *v.b, "--t", at(406), "--lines", "0")
    m = month(tmp_path)
    assert m["soc_corrected"] is False and m["fuel_level_corrected"] is True
    assert m["fuel_consumed_l"] == 6 and m["gaps"] == 1
    assert [x.t for x in series.load(tmp_path, V).series["soc"] if x.t >= at(401)] == []


def test_line_by_line_the_periods_equal_a_fresh_derivation_across_a_restart(tmp_path, capsys):
    live = tmp_path / "live"
    v = a_day(live, capsys)
    reloaded(v, 401, 401, soc_since=at(245), fuel_since=at(280))
    trip(v, 420, 1060, 30, fuel=34, soc=40)
    v.heartbeat(520)
    replay = replayed_line_by_line(live, tmp_path / "replay")
    assert len(list(l1.read(replay, V, "trip"))) == 3
    m = month(replay)
    assert m["soc_corrected"] and m["fuel_level_corrected"] and m["gaps"] == 1


def partial_fills(base, capsys, resolution):
    """Three refuellings 100 km apart, the first full, the others partial
    (VER-10): levels after 42, 39 and 51 L; receipts 30, 5 and 20 L."""
    v = vehicle(base, capsys, parameters={"fuel_level_resolution_l": resolution})
    trip(v, 0, 1000, 100, fuel=12)
    v.state(80, "fuel_level", 42, "L")
    trip(v, 200, 1100, 100, fuel=34)
    v.state(280, "fuel_level", 39, "L")
    trip(v, 400, 1200, 100, fuel=31)
    v.state(480, "fuel_level", 51, "L")
    v.heartbeat(600)
    receipt(base, capsys, 80, 30, 52.50)
    receipt(base, capsys, 280, 5, 9.00, full=False)
    receipt(base, capsys, 480, 20, 36.00, full=False)


def test_the_interval_is_extended_until_its_error_is_below_the_threshold(tmp_path, capsys):
    partial_fills(tmp_path, capsys, resolution=0.5)
    life = by_period(periods.derive_from(tmp_path, V))["lifetime"]
    # 2 × 0.5 / 20 = 5 %: not below 5. Over 25 L: 4 %, from the full tank:
    # (42 − 51 + 25) L over 200 km.
    assert life["consumption_l_per_100km"] == 8 and life["consumption_quality"] == "estimated"
    assert life["consumption_receipts"] == 3 and life["consumption_error_pct"] == 4
    assert life["consumption_from"] == at(80)


def test_without_a_resolution_the_latest_interval_is_reported_with_its_error_unknown(tmp_path, capsys):
    partial_fills(tmp_path, capsys, resolution=None)
    life = by_period(periods.derive_from(tmp_path, V))["lifetime"]
    # (39 − 51 + 20) L over 100 km; nothing is full to full.
    assert life["consumption_l_per_100km"] == 8 and life["consumption_error_pct"] is None
    assert (life["consumption_from"], life["consumption_receipts"]) == (at(280), 2)


def test_an_unreceipted_refuelling_breaks_the_interval(tmp_path, capsys):
    v = vehicle(tmp_path, capsys)
    trip(v, 0, 1000, 30, fuel=17)
    v.state(80, "fuel_level", 37, "L")
    trip(v, 200, 1030, 30, fuel=34)
    v.state(280, "fuel_level", 40, "L")       # no receipt for this one
    trip(v, 400, 1060, 30, fuel=37)
    v.state(480, "fuel_level", 40, "L")
    v.heartbeat(600)
    receipt(tmp_path, capsys, 80, 20, 35.00)
    receipt(tmp_path, capsys, 480, 3, 5.40)
    lines = by_period(periods.derive_from(tmp_path, V))
    assert lines["lifetime"]["consumption_l_per_100km"] is None
    # The purchase still counts the candidate, by its sensor delta.
    assert lines["month"]["fuel_purchased_l"] == 29 and lines["month"]["fuel_purchased_l_quality"] == "estimated"
    assert lines["month"]["fuel_cost_eur"] == 40.40 and lines["month"]["fuel_cost_eur_quality"] == "incomplete"


def test_l1_holds_periods_and_line_by_line_equals_one_rebuild(tmp_path, capsys):
    live = tmp_path / "live"
    a_day(live, capsys)
    l1.rebuild(live, V)
    d = l1.l1_dir(live, V)
    assert (d / "periods.jsonl").is_file()
    assert [x["period"] for x in l1.read(live, V, "period")] == ["month", "year", "rolling", "lifetime"]
    assert not (l1.l1_dir(live, Subject("chargepoint", "home")) / "periods.jsonl").exists()
    assert "period" not in l1.read_manifest(live, V)["through"]

    replay = tmp_path / "replay"
    shutil.copytree(live / "chargepoint-home", replay / "chargepoint-home")
    shutil.copy(live / "vehicle-a7c1" / "receipts.jsonl", replay / "receipts.jsonl")
    src = live / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst = replay / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst.parent.mkdir(parents=True)
    shutil.move(replay / "receipts.jsonl", replay / "vehicle-a7c1" / "receipts.jsonl")
    with open(dst, "a") as f:
        for line in src.read_text().splitlines(keepends=True):
            f.write(line)
            f.flush()
            l1.incremental(replay, V)
    for kind in ("trip", "charging", "refuelling", "period"):
        name = l1.FILES[kind]
        assert (l1.l1_dir(replay, V) / name).read_bytes() == (d / name).read_bytes(), name


def test_the_verb(tmp_path, capsys):
    a_day(tmp_path, capsys)
    vb = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    assert main(["derive", "periods", *vb, "--write"]) == 2      # no L1 yet
    assert "derive all --write first" in capsys.readouterr().err
    assert main(["derive", "periods", *vb]) == 0
    out = capsys.readouterr()
    assert [json.loads(x)["period"] for x in out.out.splitlines()] == ["month", "year", "rolling", "lifetime"]
    assert "4 period(s)" in out.err
    assert main(["derive", "all", *vb, "--write"]) == 0
    capsys.readouterr()
    (l1.l1_dir(tmp_path, V) / "periods.jsonl").unlink()
    assert main(["derive", "periods", *vb, "--write"]) == 0
    assert "periods.jsonl: 4 period(s)" in capsys.readouterr().out
    assert main(["l1", "read", *vb, "--kind", "period"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["period"] == "lifetime"
    assert main(["derive", "periods", "--base", str(tmp_path), "--chargepoint", "home"]) == 2


def test_an_empty_stream_has_no_periods(tmp_path):
    assert periods.derive_from(tmp_path, V) == []
    assert clock.parse(periods.spans("2026-10-09T10:00:00Z", "2026-10-09T10:00:00Z",
                                     BERLIN, 30)[-1][1]) == clock.parse("2026-10-09T10:00:00Z")


def test_the_current_lines_are_the_last_of_each_period(tmp_path, capsys):
    v = a_day(tmp_path, capsys)
    # Into November, by the stream: the month line holding its last line is current.
    v.beat = 23 * 1440
    v.heartbeat(23 * 1440)
    vb = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    assert l1.current_periods(tmp_path, V) == dict.fromkeys(l1.CURRENT)    # no L1 yet
    assert main(["derive", "all", *vb, "--write"]) == 0
    capsys.readouterr()
    lines = list(l1.read(tmp_path, V, "period"))
    assert [x["period"] for x in lines] == ["month", "month", "year", "rolling", "lifetime"]
    current = l1.current_periods(tmp_path, V)
    assert current == {"month": lines[1], "year": lines[2], "rolling": lines[3], "lifetime": lines[4]}
    assert current["month"]["start"] == "2026-11-01T00:00:00.000Z" and current["month"]["open"]

    assert main(["l1", "read", *vb, "--kind", "period", "--current"]) == 0
    out = capsys.readouterr()
    assert [json.loads(x) for x in out.out.splitlines()] == lines[1:]
    assert "4 event(s)" in out.err
    assert main(["l1", "read", *vb, "--kind", "trip", "--current"]) == 2
    assert main(["l1", "read", *vb, "--kind", "period", "--current", "--last", "1"]) == 2
