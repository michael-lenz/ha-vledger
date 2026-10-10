# User guide

*What a participant types. The design is in the other documents under
`docs/`; this page is the tour of the `vledger` command, verb by verb.*

**Status:** the integration captures; the `l0` verbs exist, `anonymise`
among them; `derive trips`,
`derive refuellings`, `derive charging`, `derive periods` and the `calc`
atoms exist; L1 is written and read with `derive … --write` and the `l1`
verbs, and the integration keeps it live; receipts are entered,
corrected, cancelled and matched with the `receipt` verbs and `derive
match`, and in Home Assistant with actions and a dashboard form; the
last trip, refuelling and charging session and what waits for a receipt
are entities; L1 is exported as CSV, JSON and GPX with the `export`
verbs. Metric entities, reports and an export action in Home Assistant
are planned.

## In Home Assistant

*Settings → Devices & services → Add integration → Vehicle Ledger*, then
choose what to add.

**A vehicle**, in four steps:

1. **Name.**
2. **Roles.** Pick the entity that reports each thing the vehicle reports —
   odometer, position, fuel level, state of charge, charging state and so
   on. Every role is optional; at least one movement role (odometer,
   position or trip counter) is required. What the ledger can do for the
   vehicle follows from the roles it has. If the vehicle reports only its
   combustion engine, not an ignition (a plug-in hybrid driving on the
   battery says *not running*), assign that as the ignition: it refines a
   trip's boundaries only when the engine ran, and never starts or ends a
   trip on its own. A plug state and a charging state refine them too — a
   vehicle that is plugged in or charging is not driving — so assign them
   even if the ledger is not to account for charging.
3. **Mapping**, only when a charging state, plug state or ignition was
   assigned: tick the source's values that mean *charging*, *plugged in*
   or *ignition on*. *unavailable* and *unknown* hold the last known
   state; any other value means the opposite — not charging, unplugged,
   off.
4. **Parameters.** Fuel, tank capacity, net battery capacity — only what a
   derivation needs; the tank capacity is required when the fuel level is
   reported in percent. Everything else keeps its default.

**A charge point**, in one step: name, location and radius on the map, an
optional energy meter entity, and the first tariff with the date it is
valid from. A charge point serves every vehicle.

A vehicle's months and years are those of Home Assistant's time zone
(*Settings → System → General*); the ledger records it at every start, and
a changed zone makes a rebuild of L1 due.

From the moment an entry is set up its raw log is written under
`<config>/vledger/`, and the entity **Capture status** shows `running`
with the lines written since start, the last line's time and the last
heartbeat. Stopping Home Assistant, unloading or reloading the entry ends
the stream orderly; a crash does not, and the next start shows as a gap.

Next to it, in the device's *Diagnostic* section, the raw log in numbers:
the size of the current month file and of the whole log, the number of
month files, when the last line, the last heartbeat and the last state
line (with its role) were written, the state lines since start and
today, the number of capture gaps, and the latest gap's length with its
reason, start and end. They are counted once when capture starts and
then kept up to date from the lines written — the same counts as
`vledger l0 stats`. Sizes are shown in KiB and gaps in seconds; the
entity's settings switch either to another unit (MiB, hours).

**Download diagnostics** (the entry's menu) adds what only a full count of
the log gives: the measured sampling and change interval of every role
(median and 95th percentile of each; [glossary](glossary.md)), the last
value of every
role, every capture gap, and the source values a charging state, plug
state or ignition met that its mapping does not list — read as *not
charging*, *unplugged* or *off*, and worth a tick in the mapping if that
was wrong. Positions are redacted.

**Options** (the entry's *Configure*): a menu over the same steps,
pre-filled, plus the thresholds and time constants, the remaining
parameters, and the data directory. For a charge point: the meter, and
*add a tariff from a date* — tariffs are never edited; a new price is a new
entry, and a correction is a new entry under the same date. Saving any of
them reloads the entry, which the stream records as stop, start and the
new configuration.

If an assigned entity disappears, it is logged as `unavailable` and a
repair issue names it. One that stays `unavailable` or `unknown` for longer
than the outage threshold (24 h, in the options) gets a repair issue too,
counted from the moment it stopped reporting — a restart in between does
not reset it. Either issue clears the moment the entity reports a value
again; an entity that is gone for good is replaced in the options.

The integration also keeps the derivation on disk, L1
([below](#the-derivation-on-disk-vledger-l1)), the moment events complete.
At startup it checks whether L1 has to be rebuilt — none yet, another
version, changed configuration or receipts — and if so rebuilds it while
capture goes on; meanwhile **Capture status** shows `recomputing`. To
rebuild on demand, call the action **Vehicle Ledger: Recompute**
(`vledger.recompute`) — for one vehicle or charge point, or, left empty,
for all:

```yaml
action: vledger.recompute
data:
  config_entry_id: 01J…      # optional
```

### What the vehicle shows

Every vehicle's device shows what the ledger derived, as L1 holds it —
updated whenever a trip, refuelling or charging session completes or a
receipt is entered, and *unavailable* while L1 is rebuilt:

| entity | shown for | state | attributes |
|---|---|---|---|
| **Last trip** | every vehicle | its distance | start, end, zones, positions, how the distance was measured, the fuel and charge it used, … |
| **Last refuelling** | a vehicle with a fuel | the receipt's litres, or the sensor's before a receipt | start, end, level before and after, price, full tank, place, the receipt and how well it matched, … |
| **Last charging session** | a vehicle with a net battery capacity | the energy from the grid | start, end, state of charge, charge point, cost, charging loss, the receipt, … |
| **Waiting for a receipt** | a vehicle with either | how many refuellings and charging sessions are *unconfirmed* or *ambiguous* | the count per kind and confirmation |

Only completed events are shown: a trip under way appears once its
standstill has elapsed. The attributes are the event's line in L1, key
for key ([l1-format.md](l1-format.md)), with **Quality of the state**
added — whether the number shown was measured, taken from a receipt,
estimated or is incomplete. The route of a trip is not an attribute; it
is in the GPX export ([below](#exports-vledger-export)). Positions are
attributes but are not kept in Home Assistant's history.

Units follow the instance: a US-customary instance shows miles and
gallons, and an entity's settings choose another unit. Attributes stay in
the units L1 uses, which each name says — `distance_km`, `quantity_l`,
`cost_eur`; for miles from an attribute, use a template:

```yaml
{{ (state_attr('sensor.volvo_last_trip', 'distance_km') / 1.609344) | round(1) }}
```

These four keep no long-term statistics except **Waiting for a
receipt**: the last trip's distance is one trip, not a level worth
averaging. Totals per month and year are the metric entities (planned).

### Receipts in Home Assistant

**From a dashboard.** A vehicle with a fuel set gets a refuelling form, one
with a net battery capacity a charging form — a plug-in hybrid both. A
form is a handful of entities on the vehicle's device; put them on a card:

- **Event** — *Enter time below*, or one of the unconfirmed candidates the
  ledger detected, newest first, by start time and the quantity the sensor
  saw. Picking a candidate gives the receipt that event's time exactly.
- **Time** — when it happened, used only with *Enter time below*. A few
  hours off is fine: the receipt meets the nearest event.
- **Litres** or **Energy**, **Total price**, for a refuelling **Price per
  litre** (one of the two prices is enough) and **Full tank** (on unless
  you switch it off), **Place** and **Note**.
- **Enter refuelling receipt** / **Enter charging receipt** — writes the
  receipt and clears the form. If the receipt is refused (no time, no
  price, a candidate that is gone), the message says why and the form keeps
  what you typed.

What is typed is held in memory only: a restart clears a half-filled form,
and nothing counts until the button is pressed.

**As actions**, for automations, scripts and *Developer tools → Actions*:
`vledger.add_refuelling_receipt`, `vledger.add_charging_receipt` and
`vledger.cancel_receipt`. Each names the vehicle by its entry and takes
the options of the `receipt` verbs below, under the same names without
the dashes ([receipts-format.md](receipts-format.md#entering)):

```yaml
action: vledger.add_refuelling_receipt
data:
  config_entry_id: 01J…            # the vehicle
  anchor: "2026-10-12 18:40:00"    # or from_candidate: the event's start
  quantity_l: 41.37
  total_price: 72.36
  full: true
response_variable: receipt         # the line written; receipt.id is its UUID
```

Correcting (`replaces`) and cancelling need a receipt's id, so in Home
Assistant they are actions only: `vledger.cancel_receipt` with
`config_entry_id` and `receipt`. Times are the instance's local time.
Every receipt changes the receipts file, so L1 is rebuilt right after it,
as at startup.

## Where the data is

Every verb works on a data directory, given as `--base DIR` or by the
environment variable `VLEDGER_BASE`, and defaults to the current directory.
Under Home Assistant that directory is `<config>/vledger/`. A stream is
named by what it belongs to: `--vehicle ID` or `--chargepoint ID`, the
subject id the integration minted. Times are UTC ISO 8601; a verb that
writes takes `--t` and defaults to now.

```bash
export VLEDGER_BASE=~/vledger
```

## The raw log: `vledger l0`

The verbs the integration will use to write a stream, usable by hand to
build one — and the verbs to read a stream back, check it and find its
gaps. The format is [l0-format.md](l0-format.md).

### Writing

```bash
vledger l0 start --vehicle a7c1 --homeassistant 2026.10.1 \
    --snapshot '[{"role":"odometer","entity":"sensor.volvo_odometer","state":"123456","unit":"km","since":"2026-10-08T22:41:10Z"}]'
vledger l0 config --vehicle a7c1 --config config.json
vledger l0 state --vehicle a7c1 --role odometer --entity sensor.volvo_odometer --state 123457 --unit km
vledger l0 state --vehicle a7c1 --role position --entity device_tracker.volvo --state not_home \
    --attr latitude=48.1371 --attr longitude=11.5754 --attr gps_accuracy=12 --attr battery=80
vledger l0 heartbeat --vehicle a7c1 --lines 2
vledger l0 stop --vehicle a7c1 --reason shutdown
```

`--snapshot` and `--config` take JSON inline, a file name, or `-` for
stdin. `--attr` may be repeated; only the attributes relevant to the role
are kept — `battery` above is dropped, and the verb prints the line it
wrote so you can see what was kept. A state is always a string: `--state
123457`, never a number the shell made of it. `--measured-at` and
`--reported-before` take a time each, as [l0-format.md](l0-format.md)
defines the keys.

### Reading

```bash
vledger l0 read --vehicle a7c1                                  # the whole stream, in order
vledger l0 read --vehicle a7c1 --kind state --role odometer     # one role
vledger l0 read --vehicle a7c1 --since 2026-10-01T00:00:00Z --until 2026-10-31T23:59:59Z
```

Prints JSON Lines, so it composes: `vledger l0 read … | jq .state`.

### Checking

```bash
vledger l0 validate --vehicle a7c1
vledger l0 validate --vehicle a7c1 --json
```

Counts files and lines by kind, names the schema versions the stream holds,
and lists every problem: an error is a line the writer could not have
written, a warning is a line readers skip (a torn last line, an unknown
kind) or something odd (time running backwards). The exit code is 1 when
there is an error.

### Counting

```bash
vledger l0 stats --vehicle a7c1
vledger l0 stats --vehicle a7c1 --since 2026-10-09T00:00:00Z --json
```

Counts a stream in one pass: month files and their sizes, lines by kind
and state lines by role, the state lines since the last start (and since
`--since`, when given), the last line, heartbeat and state line, two
intervals per role — the sampling interval, from each line's
`reported_before`, and the change interval, between two of its lines;
median and 95th percentile of each, never across a gap or a start, and
*no sampling interval measured* where no line says, rather than the change
interval in its place — the values the
state mapping does not list, and the gaps, judged against `--now` as
`gaps` does. What Home Assistant shows about the log is this count.

### Gaps

```bash
vledger l0 gaps --vehicle a7c1
vledger l0 gaps --vehicle a7c1 --min 300 --json
```

Lists every span in which nothing was captured, with its reason: `crash`,
`stopped`, `silence` or `open` ([l0-format.md](l0-format.md), *Gaps*).
`--tolerance` is how late a heartbeat may be (default 300 s); `--min` hides
gaps shorter than that; `--now` judges the end of the stream against a time
other than now.

### Anonymising

```bash
vledger l0 anonymise --shift 0.05,-3.2 --to ~/fixture             # every subject under --base
vledger l0 anonymise --shift=-0.05,3.2 --to ~/fixture --vehicle a7c1   # one; '=' before a leading minus
```

Copies the streams and receipts into a fresh directory with every position
moved by the same offset in degrees, latitude first, and every name
dropped: subject names, entity ids (now `<domain>.<role>`), zones other
than `home` and `not_home` (now `zone_1`, …), a receipt's place, provider
and note. Times and values are kept, so the copy derives the same trips,
refuellings and sessions. The longitude shift keeps every distance; a
latitude shift scales east–west distances by a little, about 2 % per
degree at 51° N, so keep it small. Anonymise all subjects in one run: the
charge points then move with the vehicles and sessions still fall within
their radius. What becomes of the copy is
[developing.md](developing.md#real-streams-as-fixtures).

## The derivations: `vledger derive`

Each derivation on its own, reading a stream and printing what it found as
JSON Lines — one event per line, so it composes with `jq` like `l0 read`.

```bash
vledger derive trips --vehicle a7c1
vledger derive trips --vehicle a7c1 --since 2026-10-01T00:00:00Z | jq '{start, end, distance_km, distance_source}'
vledger derive refuellings --vehicle a7c1 | jq '{start, zone, level_before_l, level_after_l}'
vledger derive charging --vehicle a7c1 | jq '{start, chargepoint, delta_soc_pct, grid_kwh, grid_kwh_source, cost_eur}'
```

A trip is the span between two standstills: from the first sample that
moved after at least T_still of nothing moving, to the last before the
next such span. Ignition, plug state and charging state, where assigned,
refine both ends: the start moves back to the latest *ignition on* or
*unplugged* within T_still before the first moving sample, the end forward
to the earliest *ignition off*, *plugged in* or *charging* within T_still
after the last — each a moment the vehicle was not driving, so the closest
one is the best bound ([glossary](glossary.md), *Not-driving marker*).
`refined_by` names the role that set each end, and
`movements_while_plugged` counts moving samples taken while the vehicle
was plugged in or charging: a few at a trip's end are sample timing, many
mean the plug or charging mapping is wrong. Every trip carries its distance with its source and quality (`odometer`
measured, `trip_counter` measured, `waypoints` estimated), the positions
and zones at both ends, every fix in between, the mean outside
temperature, and ΔSoC and Δfuel as estimates. A trip that spans a capture
gap is `incomplete`, and so is one whose standstill a gap falls into —
its end is unknown; a gap after the standstill has elapsed leaves the
completed trip as it was. Nothing is read across a gap: the distance
driven inside one belongs to no trip.

What the sampling cannot show, a trip cannot show either: with positions
and the odometer every 15 minutes, a stop of 20 minutes may look like 35
between moving samples and split the trip — that is the sampling, not the
stop ([glossary](glossary.md), *Sampling interval*).

A refuelling candidate is a rise of the fuel level by at least the
refuelling threshold (3 L) between two samples while the odometer stood
([glossary](glossary.md), *Refuelling*); a pump the sensor sees in
several steps is one candidate, from `start`, the first sample that rose,
to `end`, the last. It carries the position and zone where the vehicle
stood, `level_before_l`, `level_after_l` read once T_settle (6 min) has
passed after `end` together with `settled_at`, the time of that reading,
the `sensor_delta_l` between the two — always `estimated`, the receipt
says what was bought — and, with a fuel price role assigned, its value at
`start` as `price_suggestion`. No fuel flap is needed. A candidate still
settling is printed with `level_after_l` empty and not yet written to L1;
one whose settle time spans a capture gap is `incomplete`, without a level
after. A fuel level in % is converted with the tank capacity; without
one, nothing is detected, and the verb says why on stderr.

A charging session runs from the charging state turning to `charging`
until it turns to anything else; `unavailable` and `unknown` hold, so a
sensor dropping out mid-charge does not split the session. A vehicle with
no charging state assigned gets its sessions from its SoC instead: a run
of rises at standstill whose total exceeds the charging threshold, broken
by a sample that does not rise, a movement, a capture gap, or T_still
without a rise (`source` says `charging_state` or `soc`). Only finished
sessions are printed — one still charging appears once it ends.

Every session carries SoC at both ends, the position, and the charge
point whose radius holds that position — its subject id, or `foreign`.
`battery_kwh` is ΔSoC × the net battery capacity, an estimate, and only
when the capacity is set. `grid_kwh` comes from the charge point's meter
(`grid_kwh_source` `meter`, measured), read from the charge point's own
stream, when no other vehicle charged there during the session —
otherwise `meter_attributable` is `false` — else from `battery_kwh` × the
charging loss factor (`loss_factor`, estimated), else it is empty. A
charging receipt takes precedence over both (*Receipts*, below).
`cost_eur` is `grid_kwh` × the charge point's tariff valid at the start,
with the energy's quality; a tariff of 0 costs 0 without any energy; a
foreign charge has no cost until a receipt gives one. Where a receipt or
the meter gives the grid-side energy, `kwh_per_pct` is the grid-side kWh per % SoC and
`charging_loss_kwh` grid- minus battery-side energy — never derived from
the loss factor, and the capacity is never calibrated from them.
`movements_while_charging` counts moving samples inside the session, as
trips count them while plugged. A session across a capture gap is
`incomplete`.

### Periods

The metrics come per calendar month, per calendar year, for the rolling
period (30 days up to the stream's last line) and for the whole of
capture:

```bash
vledger derive periods --vehicle a7c1 | jq 'select(.period == "month") | {start, distance_km, fuel_eur_per_100km, grid_kwh_per_100km, eur_per_100km}'
vledger derive periods --vehicle a7c1 | jq 'select(.period == "lifetime") | {consumption_l_per_100km, consumption_error_pct, charge_cycles, tank_fills}'
```

Each line has the distance; the litres and the fuel cost bought, from the
receipts; the litres actually used, corrected by the fuel level at the
period's start and end; the grid-side kWh and electricity cost of the
charging sessions; the battery-side kWh, corrected by the state of charge
at both ends; kWh and euro per 100 km; the electric share of the energy,
and an estimate of the electric share of the distance; charge cycles and
tank fills. Every value has its quality beside it. An event counts in the
period it starts in, so a trip across midnight on the last of the month
counts in that month, whole. `gaps` says how many capture gaps lie in the
period: what happened inside one is missing from the counts.

The fuel consumption in litres per 100 km is on the lifetime line only,
measured tank to tank between two receipts — the latest such interval
whose possible error is under 5 %, which is always the case between two
full tanks. Between partial fills it needs the fuel level sensor's
resolution among the parameters; without it only full to full counts.
Receipt the refuellings: one without a receipt breaks the interval.

## Receipts: `vledger receipt`

A receipt is what you state about a refuelling or a charge: what the
pump or the bill says. It is kept in `receipts.jsonl` next to L0, never
edited ([receipts-format.md](receipts-format.md)).

```bash
vledger receipt add refuelling --vehicle a7c1 --anchor 2026-10-12T16:40Z \
    --quantity-l 41.37 --total-price 72.36 --full --place "motorway services"
vledger receipt add charging --vehicle a7c1 --anchor 2026-10-14T09:10Z \
    --energy-kwh 11.8 --total-price 6.49 --provider "roaming card"
vledger receipt list --vehicle a7c1             # the receipts that count, by anchor time
vledger receipt list --vehicle a7c1 --all       # every line, corrected and cancelled ones too
```

`add` prints the line it wrote, with the receipt's id. `--anchor` is when
it happened, and may be a few hours off: the receipt meets the nearest
refuelling or charging session within the matching tolerance (6 h by
default), however long after the fact you enter it. To enter a receipt
for an event the ledger detected, give its start as `--from-candidate`
instead; it then meets exactly that event. `--partial` instead of
`--full` says the tank was not filled; `--unit-price` may stand beside or
instead of `--total-price`.

A mistake is corrected or cancelled by a receipt of its own:

```bash
vledger receipt add refuelling --vehicle a7c1 --replaces 5b0e… --anchor … --quantity-l 41.73 …
vledger receipt cancel 5b0e… --vehicle a7c1 --note "entered twice"
vledger derive match --vehicle a7c1             # which receipt met which event, and which did not
```

A correction is the whole receipt again, not just the field that was
wrong. Only the current receipt can be corrected or cancelled. A receipt
that meets two events equally, or that another receipt competes with, is
reported as ambiguous and assigned to nothing: correct its anchor.

## The derivation on disk: `vledger l1`

What `derive` prints can also be kept: L1, files next to L0 under
`l1/`, one JSON Lines file per kind of event and a manifest saying what
they were derived from ([l1-format.md](l1-format.md)).

```bash
vledger derive trips --vehicle a7c1 --write      # replace l1/trips.jsonl with every completed trip
vledger derive refuellings --vehicle a7c1 --write   # likewise l1/refuellings.jsonl
vledger derive charging --vehicle a7c1 --write      # likewise l1/charging-sessions.jsonl
vledger derive periods --vehicle a7c1 --write    # rewrite l1/periods.jsonl from the events on disk
vledger derive all --vehicle a7c1 --write        # rebuild all of l1/ from scratch, swapped in whole
vledger l1 status --vehicle a7c1                 # the manifest, what waits for a receipt, and whether a rebuild is due and why
vledger l1 read --vehicle a7c1 --kind trip       # the events, as l0 read prints lines
vledger l1 read --vehicle a7c1 --kind trip --last 1   # only the last one, read from the end of the file
vledger l1 clean --vehicle a7c1                  # delete l1/ — it is regenerable
```

L1 holds completed events only: a trip is written once its standstill has
elapsed, a refuelling once it has settled, a charging session once it has
ended, judged by the stream's last line rather than the clock, so the
same stream always yields the same files. `l1 status` exits 1 when a
rebuild is due — no manifest, a different library version, a changed
configuration or changed receipts — which is what the integration checks
at startup.

## Exports: `vledger export`

For a spreadsheet, another tool or a map: renderings of L1 as it is on
disk. An export derives nothing — run `derive all --write` first; it says
so when L1 is not current — and nothing in Home Assistant writes one.

```bash
vledger export csv --vehicle a7c1 --kind trip               # a row per trip, on stdout
vledger export csv --vehicle a7c1 --kind period --out periods.csv   # the metrics, as a file
vledger export json --vehicle a7c1 --kind refuelling        # one JSON array, as L1 holds them
vledger export gpx --vehicle a7c1 --since 2026-10-01T00:00:00Z --out october.gpx   # a track per trip
```

`--kind` is `trip`, `charging`, `refuelling` or `period`; `--since` and
`--until` pick events by their start. A CSV's columns are the same for a
kind whatever it holds; how nested values become columns, and what a GPX
track holds, is [l1-format.md](l1-format.md#exports).

## The atoms: `vledger calc`

The computations the derivations are built from, each callable on its own
so any step can be checked by hand:

```bash
vledger calc distance 51.4437 7.1413 51.4812 7.2166     # great-circle, km
vledger calc convert 630 mi --quantity distance         # 1013.89 km
vledger calc convert 72 "°F" --quantity temperature     # 22.2222 °C
```

## Planned

`vledger report …` follows the same shape: a noun, a verb, `--base` and
the subject.
