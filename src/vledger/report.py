# SPDX-License-Identifier: BSD-3-Clause
"""The metrics report (CLI-04): the metrics of a period line (ADR-0014) for
a span chosen freely, and the tank-to-tank consumption intervals lying in
it, each with the mean outside temperature over it (VER-09).

A report is computed from the events L1 holds — receipts already applied,
a receipt that met nothing an event of its own — and from the stream, for
the fuel level and SoC at the span's bounds, its gaps and its temperature;
never from ``periods.jsonl``, whose lines are the calendar's. So any span
works, and a span that is a calendar month gives that month's line, the
``period`` key aside. ``vledger report metrics`` prints it, as a table and
as JSON.
"""

from __future__ import annotations

from pathlib import Path

from vledger import clock, l1, periods, series
from vledger.layout import Subject
from vledger.series import Stream

#: The event kinds a report reads from L1.
KINDS = ("trip", "charging", "refuelling")


def intervals(s: Stream, refuellings: list[dict], since: str, until: str) -> list[dict]:
    """Every tank-to-tank interval (:func:`periods.intervals`) whose both
    receipts lie in the span, latest first, each with
    ``mean_outside_temperature_c`` — the mean of the outside temperature
    samples between its two refuellings, ``None`` without any."""
    lo, hi = clock.parse(since), clock.parse(until)
    temps = s.series.get("outside_temperature", [])
    out = []
    for x in periods.intervals(s, refuellings):
        if lo <= clock.parse(x["from"]) and clock.parse(x["to"]) <= hi:
            out.append(dict(
                x, l_per_100km=round(x["l_per_100km"], 3),
                error_pct=None if x["error_pct"] is None else round(x["error_pct"], 2),
                mean_outside_temperature_c=series.mean(series.between(temps, x["from"], x["to"]))))
    return out


def build(s: Stream, events: dict[str, list[dict]], since: str | None = None,
          until: str | None = None, under_way: list[str] = ()) -> dict:
    """``{"report": line, "intervals": [...]}``: the metrics of the span as
    a period line with ``period`` ``report``, plus the consumption chosen
    among the span's intervals as the lifetime line chooses among all
    (VER-10); and those intervals. The bounds default to the stream's
    first and last line."""
    if s.first_t is None:
        raise ValueError(f"no stream for {s.subject.dirname}")
    since = clock.to_text(clock.parse(since)) if since else s.first_t
    until = clock.to_text(clock.parse(until)) if until else s.last_t
    if clock.parse(until) < clock.parse(since):
        raise ValueError(f"--until {until} lies before --since {since}")
    line = periods.metrics(s, events, periods.REPORT, since, until, under_way)
    found = intervals(s, events.get("refuelling", []), since, until)
    line.update(periods.chosen(found, float(s.thresholds()["consumption_error_pct"])))
    return {"report": line, "intervals": found}


def from_l1(base: Path, subject: Subject, since: str | None = None,
            until: str | None = None) -> dict:
    """The report of a vehicle from the L1 on disk, as it is."""
    events = {kind: list(l1.read(base, subject, kind)) for kind in KINDS}
    return build(series.load(base, subject), events, since, until,
                 periods.under_way(base, subject, events))


# --- the table ---------------------------------------------------------------

def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def table(r: dict) -> str:
    """The report as a person reads it: one row per metric with its quality,
    the corrections and gaps, the consumption, then the intervals."""
    line, found = r["report"], r["intervals"]
    rows = [f"{line['subject']}: {line['start']} to {line['end']}"
            + (", still running" if line["open"] else "")
            + f", {line['gaps']} capture gap(s), quality {line['quality']}"]
    width = max(len(k) for k in periods.METRICS)
    rows.append(f"{'metric':<{width}}  {'value':>10}  quality")
    for key in periods.METRICS:
        rows.append(f"{key:<{width}}  {_cell(line[key]):>10}  {line[key + '_quality'] or '-'}")
    rows.append(f"{'fuel_level_corrected':<{width}}  {_cell(line['fuel_level_corrected']):>10}")
    rows.append(f"{'soc_corrected':<{width}}  {_cell(line['soc_corrected']):>10}")
    if line["consumption_l_per_100km"] is None:
        rows.append("consumption: no tank-to-tank interval in the span")
    else:
        rows.append(f"consumption: {_cell(line['consumption_l_per_100km'])} L/100 km "
                    f"({line['consumption_quality']}), {line['consumption_from']} to "
                    f"{line['consumption_to']}, {line['consumption_receipts']} receipt(s), "
                    f"error {_cell(line['consumption_error_pct'])} %")
    if found:
        rows.append("")
        rows.append(f"{'from':<24}  {'to':<24}  receipts  L/100 km  error %  quality    mean °C")
        for x in found:
            rows.append(f"{x['from']:<24}  {x['to']:<24}  {x['receipts']:>8}  {_cell(x['l_per_100km']):>8}  "
                        f"{_cell(x['error_pct']):>7}  {x['quality']:<9}  {_cell(x['mean_outside_temperature_c']):>7}")
    return "\n".join(rows) + "\n"
