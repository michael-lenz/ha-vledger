# User guide

*What a participant types. The design is in the other documents under
`docs/`; this page is the tour of the `vledger` command, verb by verb.*

**Status:** the integration captures; the `l0` verbs exist; `derive trips`,
`derive refuellings`, `derive charging` and the `calc` atoms exist; L1 is
written and read with `derive … --write` and the `l1` verbs. Receipts,
metrics, the live derivation in Home Assistant, exports and reports are
planned.

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
repair issue names it until another entity is assigned.

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
gap is `incomplete`, and nothing is read across a gap: the distance driven
inside one belongs to no trip.

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
charging receipt will take precedence over both once receipts exist.
`cost_eur` is `grid_kwh` × the charge point's tariff valid at the start,
with the energy's quality; a tariff of 0 costs 0 without any energy; a
foreign charge has no cost until a receipt gives one. Where the meter
measured, `kwh_per_pct` is the grid-side kWh per % SoC and
`charging_loss_kwh` grid- minus battery-side energy — never derived from
the loss factor, and the capacity is never calibrated from them.
`movements_while_charging` counts moving samples inside the session, as
trips count them while plugged. A session across a capture gap is
`incomplete`.

## The derivation on disk: `vledger l1`

What `derive` prints can also be kept: L1, files next to L0 under
`l1/`, one JSON Lines file per kind of event and a manifest saying what
they were derived from ([l1-format.md](l1-format.md)).

```bash
vledger derive trips --vehicle a7c1 --write      # replace l1/trips.jsonl with every completed trip
vledger derive refuellings --vehicle a7c1 --write   # likewise l1/refuellings.jsonl
vledger derive charging --vehicle a7c1 --write      # likewise l1/charging-sessions.jsonl
vledger derive all --vehicle a7c1 --write        # rebuild all of l1/ from scratch, swapped in whole
vledger l1 status --vehicle a7c1                 # the manifest, and whether a rebuild is due and why
vledger l1 read --vehicle a7c1 --kind trip       # the events, as l0 read prints lines
vledger l1 clean --vehicle a7c1                  # delete l1/ — it is regenerable
```

L1 holds completed events only: a trip is written once its standstill has
elapsed, a refuelling once it has settled, a charging session once it has
ended, judged by the stream's last line rather than the clock, so the
same stream always yields the same files. `l1 status` exits 1 when a
rebuild is due — no manifest, a different library version, a changed
configuration or changed receipts — which is what the integration will
check at startup.

## The atoms: `vledger calc`

The computations the derivations are built from, each callable on its own
so any step can be checked by hand:

```bash
vledger calc distance 51.4437 7.1413 51.4812 7.2166     # great-circle, km
vledger calc convert 630 mi --quantity distance         # 1013.89 km
vledger calc convert 72 "°F" --quantity temperature     # 22.2222 °C
```

## Planned

`vledger receipt …`, `derive periods`, `vledger export …` and
`vledger report …` follow the same shape: a noun, a verb, `--base` and the
subject.
