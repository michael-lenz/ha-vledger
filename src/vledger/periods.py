# SPDX-License-Identifier: BSD-3-Clause
"""Periods and their metrics (ADR-0014; VER-01 to VER-07, VER-10, VER-11).

``periods.jsonl`` is the current answer, rewritten whole (ADR-0009): one
line per local calendar month and year since capture began, one rolling
line ending at the stream's last line, and one lifetime line that also
carries the cumulative counters and the vehicle's fuel consumption.

Everything is computed from the events as L1 holds them — receipts
already applied — plus L0 for the fuel level and SoC at the boundaries.
An event belongs to the period its ``start`` falls in, and nothing is
split; the stock is read at a boundary moved forward to the end of any
trip or session under way across it, so what an event used or added
counts where its distance and energy count.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from vledger import __version__, clock, l1, series
from vledger import config as vconfig
from vledger.layout import Subject
from vledger.series import Stream

RECEIPT, MEASURED, ESTIMATED, INCOMPLETE = "receipt", "measured", "estimated", "incomplete"
#: Strongest first; an aggregate is as good as its weakest input (ADR-0014, 4).
ORDER = (RECEIPT, MEASURED, ESTIMATED, INCOMPLETE)

MONTH, YEAR, ROLLING, LIFETIME = "month", "year", "rolling", "lifetime"
#: A span chosen freely, by the report (CLI-04): never a line of periods.jsonl.
REPORT = "report"

#: The metrics of a period line (ADR-0014, point 4), in order; each with
#: ``<key>_quality`` beside it.
METRICS = ("distance_km", "fuel_purchased_l", "fuel_cost_eur", "fuel_consumed_l",
           "grid_kwh", "electricity_cost_eur", "battery_kwh",
           "grid_kwh_per_100km", "battery_kwh_per_100km", "fuel_eur_per_100km",
           "electricity_eur_per_100km", "eur_per_100km", "electric_energy_share",
           "electric_distance_share", "charge_cycles", "tank_fills")

#: What the lifetime line — and a report — say about the vehicle's fuel
#: consumption (ADR-0014, point 5).
CONSUMPTION = ("consumption_l_per_100km", "consumption_quality", "consumption_from",
               "consumption_to", "consumption_receipts", "consumption_error_pct")


def weakest(*qualities: str | None) -> str:
    """The weakest of the given qualities; ``measured`` for none at all —
    an empty sum is exactly zero."""
    given = [q for q in qualities if q is not None]
    return max(given, key=ORDER.index) if given else MEASURED


def _r(x: float | None, digits: int) -> float | None:
    return None if x is None else round(x, digits)


# --- the periods -------------------------------------------------------------

def _month_start(t: datetime, zone) -> datetime:
    local = t.astimezone(zone)
    return datetime(local.year, local.month, 1, tzinfo=zone)


def _next_month(t: datetime, zone) -> datetime:
    y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return datetime(y, m, 1, tzinfo=zone)


def spans(first_t: str, last_t: str, zone, rolling_days: float) -> list[tuple[str, str, str]]:
    """``(period, start, end)`` of every line, in file order: the calendar
    months and years from the one holding ``first_t`` to the one holding
    ``last_t``, local to ``zone``; the rolling period ending at ``last_t``;
    the lifetime."""
    first, last = clock.parse(first_t), clock.parse(last_t)
    out = []
    m = _month_start(first, zone)
    while m <= last:
        nxt = _next_month(m, zone)
        out.append((MONTH, clock.to_text(m), clock.to_text(nxt)))
        m = nxt
    y = datetime(first.astimezone(zone).year, 1, 1, tzinfo=zone)
    while y <= last:
        nxt = datetime(y.year + 1, 1, 1, tzinfo=zone)
        out.append((YEAR, clock.to_text(y), clock.to_text(nxt)))
        y = nxt
    out.append((ROLLING, clock.to_text(last - timedelta(days=rolling_days)), last_t))
    out.append((LIFETIME, first_t, last_t))
    return out


def _within(e: dict, period: str, start: str, end: str) -> bool:
    """Whether an event belongs to a period: by its start, the period's
    end excluded — except where the end is the stream's last line, or a
    bound somebody chose. The lifetime holds every event, a receipt
    anchored before capture too."""
    if period == LIFETIME:
        return True
    t = clock.parse(e["start"])
    lo, hi = clock.parse(start), clock.parse(end)
    return lo <= t and (t < hi or (period in (ROLLING, REPORT) and t == hi))


# --- the stock at a boundary -------------------------------------------------

def reading_time(t: str, spanning: list[dict], first_t: str, last_t: str) -> str:
    """Where the stock is read for a boundary at ``t``: moved forward to the
    end of any trip or session under way across it, and kept within the
    stream — a period that began before capture is read from capture's
    start, one still running at its last line."""
    at = clock.parse(t)
    moved = True
    while moved:
        moved = False
        for e in spanning:
            if clock.parse(e["start"]) < at < clock.parse(e["end"]):
                at, moved = clock.parse(e["end"]), True
    at = min(max(at, clock.parse(first_t)), clock.parse(last_t))
    return clock.to_text(at)


def _stock(s: Stream, role: str, t: str) -> float | None:
    x = series.in_effect(s, role, t)
    return None if x is None else x.value


# --- one period's metrics ----------------------------------------------------

def _refuelled(e: dict) -> tuple[float | None, str, float | None, str]:
    """A refuelling's litres and cost, with their qualities: the receipt's
    when it has one, else the sensor's delta at the price suggestion."""
    if e.get("confirmation") == RECEIPT:
        return e["quantity_l"], RECEIPT, e["price"], RECEIPT
    litres = e.get("sensor_delta_l")
    q = weakest(e.get("quality"), ESTIMATED)
    if litres is None:
        return None, INCOMPLETE, None, INCOMPLETE
    price = e.get("price_suggestion")
    if price is None:
        return litres, q, None, INCOMPLETE
    return litres, q, litres * price, q


def _fuel_price(refuellings: list[dict], all_refuellings: list[dict], end: str) -> float | None:
    """The period's mean receipt price, Σ price / Σ litres; with no receipt
    in the period, the unit price of the last one before its end."""
    mine = [e for e in refuellings if e.get("confirmation") == RECEIPT]
    litres = sum(e["quantity_l"] for e in mine)
    if litres:
        return sum(e["price"] for e in mine) / litres
    hi = clock.parse(end)
    before = [e for e in all_refuellings
              if e.get("confirmation") == RECEIPT and clock.parse(e["start"]) <= hi]
    return before[-1]["unit_price"] if before else None


def _electricity_price(sessions: list[dict], all_sessions: list[dict], end: str) -> float | None:
    """The period's mean price per grid-side kWh; with no priced session in
    it, the last priced session's before its end."""
    def priced(xs):
        return [e for e in xs if e.get("cost_eur") is not None and e.get("grid_kwh")]

    mine = priced(sessions)
    if not mine:
        hi = clock.parse(end)
        mine = priced([e for e in all_sessions if clock.parse(e["start"]) <= hi])[-1:]
    kwh = sum(e["grid_kwh"] for e in mine)
    return sum(e["cost_eur"] for e in mine) / kwh if kwh else None


def _per_100(x: float | None, km: float) -> float | None:
    return None if x is None or not km else x / km * 100


def metrics(s: Stream, events: dict[str, list[dict]], period: str, start: str, end: str) -> dict:
    """One line of ``periods.jsonl`` (ADR-0014, point 4), envelope included."""
    p, thr = s.parameters(), s.thresholds()
    first_t, last_t = s.first_t, s.last_t
    trips = [e for e in events.get("trip", []) if _within(e, period, start, end)]
    sessions = [e for e in events.get("charging", []) if _within(e, period, start, end)]
    all_refuellings = events.get("refuelling", [])
    refuellings = [e for e in all_refuellings if _within(e, period, start, end)]
    spanning = events.get("trip", []) + events.get("charging", [])
    t0 = reading_time(start, spanning, first_t, last_t)
    t1 = reading_time(min(end, last_t, key=clock.parse), spanning, first_t, last_t)

    # Distance: the trips, by their start (ADR-0014, 3).
    km = sum(e["distance_km"] for e in trips if e.get("distance_km") is not None)
    km_q = weakest(*(weakest(e.get("quality"), e.get("distance_quality"))
                     if e.get("distance_km") is not None else INCOMPLETE for e in trips))

    # Fuel: what was bought, then what was used (VER-06).
    bought = [_refuelled(e) for e in refuellings]
    litres = sum(x[0] for x in bought if x[0] is not None)
    litres_q = weakest(*(x[1] for x in bought))
    fuel_cost = sum(x[2] for x in bought if x[2] is not None)
    fuel_cost_q = weakest(*(x[3] for x in bought))
    l0_, l1_ = _stock(s, "fuel_level", t0), _stock(s, "fuel_level", t1)
    fuel_corrected = l0_ is not None and l1_ is not None
    consumed = litres + (l0_ - l1_ if fuel_corrected else 0.0)
    consumed_q = weakest(litres_q, ESTIMATED)

    # Electricity: grid-side as charged, battery-side corrected for the SoC
    # stock (VER-03).
    grid = sum(e["grid_kwh"] for e in sessions if e.get("grid_kwh") is not None)
    grid_q = weakest(*(weakest(e.get("grid_kwh_quality"))
                       if e.get("grid_kwh") is not None else INCOMPLETE for e in sessions))
    el_cost = sum(e["cost_eur"] for e in sessions if e.get("cost_eur") is not None)
    el_cost_q = weakest(*(e.get("cost_quality") if e.get("cost_eur") is not None else INCOMPLETE
                          for e in sessions))
    capacity = p.get("battery_net_kwh")
    s0, s1 = _stock(s, "soc", t0), _stock(s, "soc", t1)
    soc_corrected = s0 is not None and s1 is not None and bool(capacity)
    stock_kwh = (s0 - s1) / 100 * capacity if soc_corrected else 0.0
    battery, battery_q = None, None
    if capacity:
        charged = [e.get("battery_kwh") for e in sessions]
        battery = sum(x for x in charged if x is not None) + stock_kwh
        battery_q = weakest(*(ESTIMATED if x is not None else INCOMPLETE for x in charged), ESTIMATED)

    # The money per 100 km, on what was used (VER-06, VER-07).
    fuel_price = _fuel_price(refuellings, all_refuellings, end)
    if consumed == 0:
        fuel_eur_100, fuel_eur_q = (0.0 if km else None), consumed_q
    else:
        fuel_eur_100 = _per_100(consumed * fuel_price, km) if fuel_price is not None else None
        fuel_eur_q = weakest(consumed_q, fuel_cost_q if refuellings else None)
    el_price = _electricity_price(sessions, events.get("charging", []), end)
    stock_eur = 0.0
    if stock_kwh:
        factor = p.get("charging_loss_factor") or 1.0
        stock_eur = stock_kwh * factor * el_price if el_price is not None else None
    el_eur_100 = _per_100(None if stock_eur is None else el_cost + stock_eur, km)
    el_eur_q = weakest(el_cost_q, ESTIMATED)
    total_100 = (fuel_eur_100 + el_eur_100
                 if fuel_eur_100 is not None and el_eur_100 is not None else None)

    # The electric share, two ways (VER-05), always on the whole period.
    hu = thr.get("heating_value_kwh_per_l")
    energy_share = distance_share = None
    if battery is not None and hu:
        fuel_kwh = consumed * hu
        if battery + fuel_kwh:
            energy_share = battery / (battery + fuel_kwh)
        el, ice = p.get("eta_el"), p.get("eta_ice")
        if el and ice and el * battery + ice * fuel_kwh:
            distance_share = el * battery / (el * battery + ice * fuel_kwh)

    # The counters (VER-11): lower bounds across gaps, with the gaps beside.
    cycles = sum(e["delta_soc_pct"] for e in sessions if e.get("delta_soc_pct") is not None) / 100
    tank = p.get("tank_capacity_l")
    fills = litres / tank if tank else None
    if period == LIFETIME:
        cycles += p.get("charge_cycles_start") or 0
        if fills is not None:
            fills += p.get("tank_fills_start") or 0
    lo, hi = clock.parse(start), clock.parse(end)
    gaps = sum(1 for g in s.gaps if clock.parse(g.start) < hi and clock.parse(g.end) > lo)

    m = {   # in the order of METRICS
        "distance_km": (_r(km, 3), km_q),
        "fuel_purchased_l": (_r(litres, 3), litres_q),
        "fuel_cost_eur": (_r(fuel_cost, 2), fuel_cost_q),
        "fuel_consumed_l": (_r(consumed, 3), consumed_q),
        "grid_kwh": (_r(grid, 3), grid_q),
        "electricity_cost_eur": (_r(el_cost, 2), el_cost_q),
        "battery_kwh": (_r(battery, 3), battery_q),
        "grid_kwh_per_100km": (_r(_per_100(grid, km), 3), weakest(grid_q, km_q)),
        "battery_kwh_per_100km": (_r(_per_100(battery, km), 3),
                                  weakest(battery_q, km_q) if battery is not None else None),
        "fuel_eur_per_100km": (_r(fuel_eur_100, 3), weakest(fuel_eur_q, km_q)),
        "electricity_eur_per_100km": (_r(el_eur_100, 3), weakest(el_eur_q, km_q)),
        "eur_per_100km": (_r(total_100, 3), weakest(fuel_eur_q, el_eur_q, km_q)),
        "electric_energy_share": (_r(energy_share, 4),
                                  weakest(battery_q, consumed_q) if energy_share is not None else None),
        "electric_distance_share": (_r(distance_share, 4),
                                    ESTIMATED if distance_share is not None else None),
        "charge_cycles": (_r(cycles, 3), MEASURED),
        "tank_fills": (_r(fills, 3), (weakest(litres_q) if fills is not None else None)),
    }
    line = {
        "kind": "period", "subject": s.subject.id, "start": start, "end": end,
        "quality": weakest(*(q for v, q in m.values() if v is not None)),
        "version": __version__,
        "period": period,
        "open": clock.parse(end) > clock.parse(last_t),
    }
    assert tuple(m) == METRICS
    for key, (value, q) in m.items():
        line[key] = value
        line[key + "_quality"] = q if value is not None else None
    line["fuel_level_corrected"] = fuel_corrected
    line["soc_corrected"] = soc_corrected
    line["gaps"] = gaps
    if period == LIFETIME:
        line.update(consumption(s, all_refuellings))
    return line


# --- the vehicle's consumption: tank to tank (VER-01, VER-10) ---------------

def intervals(s: Stream, refuellings: list[dict]) -> list[dict]:
    """Every tank-to-tank interval between two refuelling receipts A before
    B, latest B first and, for each, the shortest first: consumption =
    (L_A − L_B + Σq) / (odometer_B − odometer_A). Both full: the sensor
    term vanishes and the value is ``receipt``. Otherwise the settled levels
    after both, ``estimated``, with the relative error 2 × resolution / Σq.
    An interval that cannot be computed — a level or an odometer missing,
    or an unreceipted refuelling inside it whose litres no receipt states —
    is left out."""
    res = s.parameters().get("fuel_level_resolution_l")
    ordered = sorted(refuellings, key=lambda e: clock.parse(e["start"]))
    receipted = [i for i, e in enumerate(ordered) if e.get("confirmation") == RECEIPT]
    out = []
    for jb in reversed(range(len(receipted))):
        b = ordered[receipted[jb]]
        odo_b = _stock(s, "odometer", b["start"])
        q_sum = 0.0
        for ja in reversed(range(jb)):
            a = ordered[receipted[ja]]
            q_sum += ordered[receipted[ja + 1]]["quantity_l"]
            if any(e.get("confirmation") != RECEIPT
                   for e in ordered[receipted[ja] + 1:receipted[jb]]):
                break       # litres nobody stated: no interval reaches past it
            odo_a = _stock(s, "odometer", a["start"])
            if odo_a is None or odo_b is None or odo_b <= odo_a:
                continue
            if a["full"] and b["full"]:
                term, q, err = 0.0, RECEIPT, 0.0
            else:
                la, lb = a.get("level_after_l"), b.get("level_after_l")
                if la is None or lb is None:
                    continue
                term, q = la - lb, ESTIMATED
                err = 2 * res / q_sum * 100 if res and q_sum else None
            out.append({"from": a["start"], "to": b["start"], "receipts": jb - ja + 1,
                        "l_per_100km": (term + q_sum) / (odo_b - odo_a) * 100,
                        "quality": q, "error_pct": err})
    return out


def chosen(found: list[dict], limit: float) -> dict:
    """The consumption among intervals as :func:`intervals` lists them
    (VER-10): the latest whose relative error is below ``limit``, extended
    over as many receipts as it takes; a full-to-full one always
    qualifies. With none, the latest interval with its error — unknown
    without a sensor resolution — and never suppressed."""
    x = next((x for x in found if x["error_pct"] is not None and x["error_pct"] < limit),
             found[0] if found else None)
    if x is None:
        return dict.fromkeys(CONSUMPTION)
    return {"consumption_l_per_100km": _r(x["l_per_100km"], 3),
            "consumption_quality": x["quality"],
            "consumption_from": x["from"], "consumption_to": x["to"],
            "consumption_receipts": x["receipts"],
            "consumption_error_pct": _r(x["error_pct"], 2)}


def consumption(s: Stream, refuellings: list[dict]) -> dict:
    """The vehicle's consumption: :func:`chosen` over every interval."""
    return chosen(intervals(s, refuellings), float(s.thresholds()["consumption_error_pct"]))


# --- the file ----------------------------------------------------------------

def derive(s: Stream, events: dict[str, list[dict]]) -> list[dict]:
    """Every line of ``periods.jsonl`` for a stream and its L1 events."""
    if s.subject.kind != "vehicle" or s.first_t is None:
        return []
    zone = vconfig.zone_of(s.config or {})
    days = float(s.thresholds()["rolling_period_d"])
    return [metrics(s, events, *span) for span in spans(s.first_t, s.last_t, zone, days)]


def from_events(base: Path, subject: Subject, events: dict[str, list[dict]]) -> list[dict]:
    return derive(series.load(base, subject), events)


def derive_from(base: Path, subject: Subject) -> list[dict]:
    """The periods as a fresh derivation of every kind would give them."""
    return from_events(base, subject, {k: l1.derive(base, subject, k) for k in l1.kinds(base, subject)})


l1.PERIODS = from_events
