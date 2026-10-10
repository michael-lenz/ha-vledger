# L1 on disk

*Design document — the derivation's files, as decided in ADR-0009 and,
for the periods, ADR-0014 of the project's register. This page is what a
reader of their own `l1/` needs; the reasoning is in the decisions.*

**Status:** `trips.jsonl`, `refuellings.jsonl`, `charging-sessions.jsonl`,
`periods.jsonl`, the manifest, the cursor, rebuilds, the `l1` verbs,
receipts in events, the live derivation in Home Assistant and the
exports exist.

## Files

```
<base>/vehicle-<subject>/l1/
  manifest.json              what this L1 was derived from, and by what
  trips.jsonl                one line per trip, in order of start
  charging-sessions.jsonl    one line per charging session
  refuellings.jsonl          one line per refuelling candidate
  periods.jsonl              one line per period with its metrics
```

Event files hold **completed** events only — a trip once its standstill
has elapsed, a refuelling once T_settle has elapsed after its last rise,
a session once the charging state went away (by SoC, once its run of
rises was broken) — one JSON object per line, in order of `start`. Every event carries `kind` (`trip`,
`charging`, `refuelling`, `period`), `subject`, `start`, `end`, `quality`
(`measured`, `receipt`, `estimated`, `incomplete`) and `version`, then the
keys of its kind, exactly as `vledger derive …` prints them. A derived
event has no id: it is named by `(kind, subject, start)`, and a receipt
that matched it is referenced by the receipt's UUID in the event.

A charge point's `l1/` holds a manifest and nothing else: meter
attribution and cost are derived on the vehicle's side, which reads the
charge points' streams and config lines, and the other vehicles' streams
to know whether one of them charged at the same meter meanwhile (LAD-07).

## The manifest

```json
{"vledger": "0.1.0", "derived_at": "2026-10-10T06:00:00.000Z",
 "config": "sha256:…", "receipts": "sha256:…",
 "through": {"trip": "2026-10-09T15:12:00.000Z"},
 "l0_through": "2026-10-10T05:59:30.000Z"}
```

`config` is the hash of the latest `config` line's object, `receipts` the
hash of `receipts.jsonl`; `through` is, per kind, the `end` of the last
detected event written — the cursor, which an event made from a receipt
alone does not move; `l0_through` the `t` of the last L0 line read.

## Receipts in events

Refuellings and charging sessions are matched against the current
receipts on every derivation ([receipts-format.md](receipts-format.md#matching)).
Every such event carries:

| key | meaning |
|---|---|
| `receipt` | the id of the receipt it carries, or `null` |
| `confirmation` | `receipt`; `chargepoint` (a session at a configured charge point, without a receipt); `unconfirmed` (waiting for one); `ambiguous` |
| `contenders` | only when `ambiguous`: the ids of the receipts competing for it |

With a receipt, its values come first, flagged `receipt`, and the sensor
value they displaced stays beside them:

| kind | from the receipt | kept from the sensor |
|---|---|---|
| refuelling | `quantity_l`, `quantity_quality`; `price`, `unit_price`, `price_quality`; `full`, `place`, `fuel`, `note` | `sensor_delta_l` |
| charging | `grid_kwh`, `grid_kwh_quality` and `grid_kwh_source` all `receipt`; `cost_eur`, `cost_quality`; `place`, `provider`, `note` — and `kwh_per_pct`, `charging_loss_kwh` from the billed energy (LAD-09) | `sensor_grid_kwh`, `sensor_grid_kwh_quality`, `sensor_grid_kwh_source` |

`deviation_pct` is |receipt − sensor| / receipt × 100, and `implausible`
is `true` above `plausibility_pct` (default 15); without a sensor value
the deviation is `null` and nothing is flagged. A receipt that met no
event is an event of its own, `start` = `end` = its anchor, quality
`receipt`.

The incremental derivation appends a new refuelling or session only when
no current receipt is anchored later than the tolerance before it; when
one is, the new event could change a match on disk, and it rebuilds.

## What waits for a receipt

A refuelling or charging session whose `confirmation` is `unconfirmed` or
`ambiguous` waits for a person: a receipt to enter, or one to correct.
`vledger l1 status` counts them per kind, from the event files as
written, and Home Assistant's **Waiting for a receipt** shows the same
count (ADR-0016). Between a receipt being entered and the next run of
the writer, the count can still include the event it was entered for.

## Periods

`periods.jsonl` is the current answer, not a log: one line per calendar
month and per calendar year from the one capture began in to the one
holding the stream's last line, months without an event included; one
**rolling** line, the `rolling_period_d` days (30) ending at the stream's
last line; one **lifetime** line, from the first L0 line to the last.
Months and years begin at local midnight in the vehicle's `time_zone`
([l0-format.md](l0-format.md#config)), UTC without one; `start` and `end`
are UTC like every time in L1. A charge point's L1 has no periods.

Every line carries the envelope with `kind` `period`, then `period`
(`month`, `year`, `rolling`, `lifetime`) and `open`, `true` while the
period's end lies after the stream's last line. A line is named by
`(kind, subject, period, start)`.

**What a period holds.** An event belongs to the period its `start` falls
in — a receipt that met nothing, by its anchor — and to no other; nothing
is split. The fuel level and SoC are read at the period's boundaries:
the value in effect there, the reading moved forward to the end of any
trip or charging session under way across the boundary, and kept within
the stream — a month that began before capture is read from capture's
start, one still running at the last line. Nothing is read across a
capture gap; without both readings there is no correction, and
`fuel_level_corrected` or `soc_corrected` says so.

**The metrics.** Each comes with `<key>_quality`: the weakest of what it
was computed from, in the order `receipt`, `measured`, `estimated`,
`incomplete`; `null` beside a `null` value. The envelope's `quality` is
the weakest of the line's.

| key | what |
|---|---|
| `distance_km` | Σ the trips' distance |
| `fuel_purchased_l`, `fuel_cost_eur` | what was bought: receipt quantities and prices; an unreceipted candidate by its `sensor_delta_l` at its `price_suggestion`, `estimated`, its cost `incomplete` without a price |
| `fuel_consumed_l` | `fuel_purchased_l` + (level at start − level at end); always `estimated` |
| `grid_kwh`, `electricity_cost_eur` | Σ the sessions' `grid_kwh` and `cost_eur` as they carry them — priced at the tariff of their start, never re-priced here |
| `battery_kwh` | Σ the sessions' `battery_kwh` + (SoC at start − SoC at end) × the net battery capacity; `null` without a capacity |
| `grid_kwh_per_100km`, `battery_kwh_per_100km` | on the whole distance, also with two energy carriers |
| `fuel_eur_per_100km` | `fuel_consumed_l` at the period's mean receipt price (Σ price / Σ litres; with no receipt in the period, the last receipt's unit price before its end), per 100 km |
| `electricity_eur_per_100km` | the electricity cost plus the SoC stock change, as grid-side kWh (× the charging loss factor) at the period's mean price per grid-side kWh, per 100 km |
| `eur_per_100km` | the two together |
| `electric_energy_share` | `battery_kwh` / (`battery_kwh` + `fuel_consumed_l` × heating value) — the share of the energy put in, not of the distance |
| `electric_distance_share` | the same weighted by the efficiencies `eta_el` and `eta_ice`; always `estimated` |
| `charge_cycles` | Σ the sessions' ΔSoC / 100 %; SoC gained outside a session does not count |
| `tank_fills` | `fuel_purchased_l` / the tank capacity; `null` without one |
| `fuel_level_corrected`, `soc_corrected` | whether the stock correction could be made |
| `gaps` | the capture gaps overlapping the period: counts across them are lower bounds |

There are no litres per 100 km per period: fuel consumption is
tank-to-tank only, and a period's consumed litres rest on the level
sensor — they price the distance. A rate over no distance is `null`.

**The lifetime line** counts `charge_cycles` and `tank_fills` from the
configured starting values (`charge_cycles_start`, `tank_fills_start`),
and carries the vehicle's fuel consumption:

| key | what |
|---|---|
| `consumption_l_per_100km` | (L_A − L_B + Σq) / (odometer_B − odometer_A) between two refuelling receipts A and B, Σq the receipt litres after A up to B |
| `consumption_quality` | `receipt` when both are full tanks — the level term then vanishes — else `estimated`, from the levels after both |
| `consumption_from`, `consumption_to` | the starts of A and B |
| `consumption_receipts` | how many receipts the interval spans |
| `consumption_error_pct` | 2 × `fuel_level_resolution_l` / Σq; 0 for full to full, `null` without a resolution |

The value is the latest interval whose error is below
`consumption_error_pct` (5 %), extended back over as many receipts as it
takes; full to full always qualifies. With none, the latest interval is
reported with its error rather than suppressed. An interval needs the
odometer at both ends and, unless both are full, the settled level after
both; one with an unreceipted refuelling inside it is not formed, since
no receipt states its litres. The list of every interval is not stored:
`vledger report metrics` computes it for a span, with the mean outside
temperature over each ([user guide](user-guide.md#reports-vledger-report)).

## Reading

Every reader goes through the library: `l1.read` yields a kind's events
in order (`vledger l1 read`), `l1.last` its last N from the end of the
file without reading the rest (`vledger l1 read --last N`),
`l1.waiting` the count above, and `l1.current_periods` the current month,
year, rolling and lifetime lines of `periods.jsonl` — per period its last
line, the one holding the stream's last line (`vledger l1 read --kind
period --current`). A torn last line is skipped by all of them.
Home Assistant's event and metric entities read the last event of each
kind, the count and the current periods once at start and again after
every run of the writer, and show nothing else (ARC-05); the month lines,
read whole, become its external statistics.

## Who writes what

- Event files are **append-only between rebuilds**: a rebuild writes them
  whole, the incremental derivation appends an event the moment it is
  complete, flushed and synced like an L0 line.
- `periods.jsonl` is **rewritten whole**, atomically, from the event files
  as written: by a rebuild into `l1.tmp/` before the swap, and by every
  incremental run after its appends, whether or not it appended anything,
  since the rolling period moves with the stream. It has no cursor and no
  entry in the manifest's `through`.
- A **rebuild replaces the directory atomically**: everything is derived
  into `l1.tmp/`, which is then renamed into place, so a reader never sees
  a half-built L1 and a crash mid-way leaves the old one.
- The manifest is rewritten after every rebuild and every incremental run.
- In Home Assistant **one writer per subject** does all three, beside
  capture and never in its way: the rebuild check once the start's
  `config` line is on disk, then an incremental run on every heartbeat and
  at most once a minute after state lines, and a rebuild whenever the
  check says so or the action `vledger.recompute` asks. Runs are
  serialised, so an incremental run never meets a rebuild; while a rebuild
  runs, capture status reads `recomputing`.

## The cursor

The incremental derivation reads the manifest's `through`, re-reads L0
from there — seeded with the last value of every role before it, so a trip
that begins right after the cursor still knows where the odometer stood —
and appends only events whose `start` is later than that kind's `through`.
An event open at the time (a trip under way) is detected again from L0
on the next run; there is no state outside L0, receipts, configuration
and L1.

## When L1 is rebuilt

No manifest; a manifest from another library version; a `config` hash
that is not the latest config line's; a `receipts` hash that is not the
file's. `vledger l1 status` says which, and exits 1.

The hashes cover the vehicle's own configuration and receipts only. A
charging session also depends on the charge points' configuration and
streams and on the other vehicles' streams, and a change there — a
corrected tariff, above all — does not make a rebuild due; `derive all
--write` brings it in by hand (ISSUE-0013).

## Exports

JSON Lines is the L1 of record; CSV, JSON and GPX are renderings of it,
made by `vledger export` and by the Home Assistant action `vledger.export`
from the files as they are and never by the live path (ABL-05). Both
select and render through the same two library functions, so neither can
produce what the other cannot. An export reads nothing but L1, so it is
exactly as current as L1 and a stale one is reported, not re-derived. The
verb writes where `--out` says; the action writes into the instance's
local media directory under `vledger/<vehicle>/`, one file per kind named
as its L1 file (`trips.csv`, `trips.gpx`), replaced atomically by the next
export of the kind, or a bare file name of the caller's (ADR-0017) — never
anywhere else, and only under the writer's lock, so a rebuild cannot swap
`l1/` away underneath it.

- **CSV**, one kind: a header, then a row per event in file order. The
  columns are declared per kind in the library, the envelope first, so two
  exports of a kind line up whatever they hold; a key absent from an event
  is an empty cell. A key no declaration names would follow them, sorted,
  rather than be dropped. A position becomes four columns,
  `<key>.t`, `.latitude`, `.longitude`, `.accuracy_m`; `refined_by`
  becomes `refined_by.start` and `.end`; a list of ids is one cell, ids
  separated by spaces. Values are spelled as in JSON — `true`, `41.8` —
  and `null` is empty. A trip's `waypoints` are not in it: they are the
  GPX's.
- **JSON**, one kind: one array of the events, each exactly as its line.
- **GPX 1.1**, the trips: one `trk` per trip, `name` its `start`, `desc`
  its span, quality and distance, `number` its position in the export,
  and one `trkseg` of its waypoints as `trkpt` with `time`, the fix before
  it moved first. A trip without a waypoint is a track with an empty
  segment, so the tracks count the trips.

## Determinism

A stream replayed line by line with the incremental derivation after
every line yields byte-identical event files and `periods.jsonl` to one rebuild of
the whole stream. That is a test in the repository, and it runs on every real stream
that becomes a fixture.
