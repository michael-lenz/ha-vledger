# User guide

*What a participant types. The design is in the other documents under
`docs/`; this page is the tour of the `vledger` command, verb by verb.*

**Status:** every verb, entity and action on this page exists.

## In Home Assistant

*Settings → Devices & services → Add integration → Vehicle Ledger*, then
choose what to add.

**A vehicle**, in four steps:

1. **Name.**
2. **Roles.** Pick the entity that reports each thing the vehicle reports —
   odometer, position, fuel level, state of charge, charging state and so
   on. Every role is optional; at least one movement role (odometer,
   position or trip counter) is required. What the ledger can do for the
   vehicle follows from the roles it has. An engine status (a plug-in
   hybrid's *running* or *not running*) is assigned as **engine**, not as
   the ignition: it can start a trip, never end one, since the engine stops
   while the car drives on electrically. A **lock** likewise only starts
   one — the car locks itself on driving off. A plug state and a charging
   state refine trips too — a vehicle that is plugged in or charging is not
   driving — so assign them even if the ledger is not to account for
   charging. **In use** is for an entity that reports the vehicle being
   used (a car connection saying *car in use*): it keeps a trip whole while
   the odometer and position only arrive at the stop; set T_still to twice
   its update interval (60 minutes for one updated every 30) — unless the
   vehicle is set to report per driving cycle (below). **Fuel flap**
   dates a refuelling to the pump. **Trip computer: average consumption**
   is the car's own average since its trip counter was reset, in
   L/100 km; assigned next to the trip counter, it gives every trip the
   fuel the car itself measured, which is finer than the fuel level by an
   order of magnitude.
3. **Mapping**, only when an enumerated role was assigned: tick the
   source's values that mean *charging*, *plugged in*, *ignition on*,
   *engine running*, *locked*, *in use* or *fuel flap open*. *unavailable*
   and *unknown* hold the last known state; any other value means the
   opposite — not charging, unplugged, off, stopped, unlocked, not in
   use, closed.
4. **Parameters.** Fuel, tank capacity, net battery capacity — only what a
   derivation needs; the tank capacity is required when the fuel level is
   reported in percent. Everything else keeps its default. *Net* battery
   capacity means the usable capacity, what a full battery delivers: the
   figure a vehicle integration reports as the battery's capacity may be
   the gross one, and every battery energy, electricity cost and metric
   scales with this number.

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
role, every capture gap, and the source values an enumerated role met
that its mapping does not list — read as the opposite (*not charging*,
*unplugged*, *off*, *unlocked*, …), and worth a tick in the mapping if
that was wrong. Positions are redacted.

**Options** (the entry's *Configure*): a menu over the same steps,
pre-filled, plus the thresholds and time constants, the remaining
parameters, where notifications go ([below](#events-and-notifications)),
and the data directory. For a charge point: the meter, and
*add a tariff from a date* — tariffs are never edited; a new price is a new
entry, and a correction is a new entry under the same date. Saving any of
them reloads the entry, which the stream records as stop, start and the
new configuration.

Among the remaining parameters, **How movement is reported** says whether
the vehicle reports its odometer and position while driving (*sampled*,
the default) or uploads them once per driving cycle, at the stop
(*per driving cycle*) — a vehicle whose odometer never changes during a
drive, only when it is parked. Set it to *per driving cycle* for such a
vehicle: its trips are then read as legs from the departure (an unlock,
the engine starting, the trip counter resetting) to the upload at the stop,
and a stop is measured to the minute. The **exit window** (5 minutes) is
how close to an arrival an unlock counts as getting out rather than
setting off. The **fuel level sensor resolution** and the **state of
charge sensor resolution** (1 %) say what each sensor is good to, not its
display step — a level shown to 0.1 L can be off by a litre — and they
bound every figure read from a sensor delta: a trip's consumption from
the level needs the first, and without it the level yields none.

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
| **Last trip fuel consumed** | a vehicle with a fuel | the litres it used | start, end, distance, the litres used with their source — the trip computer, or the fuel level — and possible error |
| **Last trip fuel per 100 km** | a vehicle with a fuel | its litres per 100 km | as *Last trip fuel consumed* |
| **Last trip battery energy** | a vehicle with a net battery capacity | the kWh it took from the battery | start, end, distance, the kWh used with their possible error |
| **Last trip battery energy per 100 km** | a vehicle with a net battery capacity | its kWh per 100 km, battery-side | as *Last trip battery energy* |
| **Waiting for a receipt** | a vehicle with either | how many refuellings and charging sessions are *unconfirmed* or *ambiguous* | the count per kind and confirmation |

Only completed events are shown: a trip under way appears once its
standstill has elapsed. The attributes are the event's line in L1, key
for key ([l1-format.md](l1-format.md)), with **Quality of the state**
added — whether the number shown was measured, taken from a receipt,
estimated or is incomplete. The route of a trip is not an attribute; it
is in the GPX export ([below](#exports-vledger-export)). Positions are
attributes but are not kept in Home Assistant's history.

The four trip figures show the last trip's own fuel and battery energy,
never the vehicle's — that is the tank-to-tank consumption of the
periods. A figure is shown when its sign is certain, or when it is right
to the step it is shown in (0.1 per 100 km); otherwise the quantity and
its rate are *unknown* together, and the attributes still show the
quantity and its possible error. With the trip computer assigned, the
fuel figure is the car's own measurement, and an electric trip shows
0 litres. From the fuel level alone it is an estimate that needs the
sensor's resolution among the parameters, and a trip using less than the
level can be wrong by — about 2 L at a resolution of 1 L — is *unknown*,
an electric trip included: the level cannot tell 0 L from 2 L. A
negative battery figure means the engine charged the battery during the
trip.

Units follow the instance: a US-customary instance shows miles and
gallons, and an entity's settings choose another unit. Attributes stay in
the units L1 uses, which each name says — `distance_km`, `quantity_l`,
`cost_eur`; for miles from an attribute, use a template:

```yaml
{{ (state_attr('sensor.volvo_last_trip', 'distance_km') / 1.609344) | round(1) }}
```

None of these keeps long-term statistics except **Waiting for a
receipt**: the last trip's distance is one trip, not a level worth
averaging. Totals per month and year are the metric entities, below.

### Metrics per month, year and rolling period

The vehicle's device also shows the metrics of `periods.jsonl`
([below](#periods)): one entity per metric for the current month, the
current year and the rolling period. "Current" is the month holding the
stream's last line, so a new month begins with the first line written
in it, not with the clock. Which metrics a vehicle gets follows its
energies, as the receipt forms do:

| metric | shown for | kind |
|---|---|---|
| **Distance**, **Cost per 100 km** | every vehicle | sum, rate |
| **Fuel purchased**, **Fuel consumed**, **Fuel cost**, **Tank fills** | a vehicle with a fuel | sums |
| **Grid energy**, **Battery energy**, **Electricity cost**, **Charge cycles** | a vehicle with a net battery capacity | sums |
| **Grid energy per 100 km**, **Battery energy per 100 km** | a vehicle with a net battery capacity | rates |
| **Fuel cost per 100 km**, **Electricity cost per 100 km**, **Electric share of energy**, **Electric share of distance** | a vehicle with both | rates, shares |

Each is named after its period — *Distance this month*, *Distance this
year*, *Distance in the rolling period*. The month's entities are
enabled; the year's and the rolling period's exist but are disabled, and
*Settings → Entities* enables the ones you want. Nine more show the
lifetime line, the vehicle's overall figures: **Charge cycles in total**
and **Tank fills in total**, both including the starting values
configured; **Fuel per 100 km in total**, tank to tank, with the receipts
it spans and its relative error as attributes; **Grid energy per 100 km in
total** and **Battery energy per 100 km in total**, the electricity
consumption on the whole distance since capture began; and the totals
**Distance in total**, **Fuel consumed in total**, **Grid energy in
total** and **Battery energy in total**. The fuel consumption on the
whole distance is the tank-to-tank figure, never a sum of the trips'.

Every metric entity's attributes are its line's **Start**, **End**,
**Still running** and **Capture gaps**, and **Quality of the state**;
fuel metrics add whether the fuel level corrected them, electricity
metrics whether the state of charge did.

What they keep as statistics:

- A month's or year's **sum** is a total that restarts with its period,
  so Home Assistant's statistics add the months up and a correction after
  a rebuild counts as the change it is. The rolling period's sums keep
  no statistics: a sliding window is neither a level nor a running sum.
- A **rate** or **share** is a level, kept as one.
- **Charge cycles in total**, **Tank fills in total** and the four
  totals are running totals; **Fuel per 100 km in total** and the two
  overall rates are levels.

Distances and litres follow the instance's units, as above. The
electricity rates, `kWh/100km`, carry a device class, so an entity's
settings can show them as mi/kWh, Wh/km or km/kWh and the statistics
convert with them; `L/100km` and the costs per 100 km are shown as they
are, without conversion; shares in per cent. Costs are shown in the
instance's currency, the one the receipt form asks for.

The month's and year's energy sums can be added to Home Assistant's
energy dashboard. Do not add **Grid energy** next to a wallbox meter
that already measures the same charging: the dashboard would count it
twice.

**Corrected history.** An entity's statistics keep what it showed at the
time, so when a rebuild corrects a past month — a corrected receipt, a
changed parameter — its statistics keep the old value. The ledger
therefore also writes every metric as a statistic of its own, one value
per month, and writes the whole series again whenever a run changes the
months. These are named after the vehicle and the metric — *Golf
Distance* — with the id `vledger:<subject>_<metric>`; in a **Statistics
graph** card, pick them with the period *Month* (the change for a sum,
the mean for a rate). Use the entities for the month under way and these
for the history. Removing a vehicle leaves them in the recorder; delete
them under *Developer tools → Statistics*.

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

### Events and notifications

Every event the ledger completes — a trip, a refuelling, a charging
session — fires the Home Assistant event **`vledger_event`** the moment it
is written to L1. One that waits for a receipt (*unconfirmed* or
*ambiguous*: every refuelling and a charging session away from a
configured charge point) fires **`vledger_candidate`** right after it.
Both carry `config_entry_id`, `vehicle` (the entry's name), `subject` and
`kind`, then the event's line from L1 as the event entities show it —
the same keys, without positions, which Home Assistant would otherwise
keep in its history:

```yaml
triggers:
  - trigger: event
    event_type: vledger_candidate
    event_data:
      vehicle: Volvo
      kind: refuelling
actions:
  - action: notify.persistent_notification
    data:
      message: >
        Refuelled {{ trigger.event.data.sensor_delta_l }} L —
        enter the receipt.
```

Only what is new fires. A rebuild of L1 — at startup after an update, or
after a changed configuration, a receipt or `vledger.recompute` — fires
nothing, since it re-derives what was already reported, and a receipt that
confirms a candidate changes that event rather than making a new one. An
event completed while Home Assistant was down fires at the next start. One
completed while a rebuild is due becomes part of that rebuild and does not
fire.

**Notifications.** *Configure → Notifications* picks a notify action for
the vehicle — `notify.mobile_app_<phone>`, a group, or any other. Left
empty, the default, nothing is sent. Changing it reloads the entry but
never rebuilds L1. Each new candidate then gets one notification: the
vehicle, *refuelling detected* or *charging away from home*, the local
start time and the quantity the sensor saw, and for a refuelling the
price the fuel price role suggests. A run that finds more than three at
once — after a long outage, say — sends one notification with their
number instead. On the Companion app the notification has the action
**Enter receipt**, which opens the vehicle's device page with the receipt
form, the newest candidate first in its event list. If the target refuses
a notification, it is logged and nothing else changes.

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

The verbs the integration writes a stream with, usable by hand to build
one — and the verbs to read a stream back, check it and find its gaps. The format is [l0-format.md](l0-format.md).

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
next such span. A vehicle reported in use is moving, from the first
report to the last. Ignition, plug state and charging state, where
assigned, refine both ends, lock and engine the start only: the start
moves back to the latest *ignition on*, *unplugged*, *unlocked* or *engine
running* within T_still before the first moving sample, the end forward
to the earliest *ignition off*, *plugged in* or *charging* within T_still
after the last — each a moment the vehicle was not driving, so the closest
one is the best bound ([glossary](glossary.md), *Not-driving marker*).
`refined_by` names the role that set each end, and
`movements_while_plugged` counts moving samples taken while the vehicle
was plugged in or charging: a few at a trip's end are sample timing, many
mean the plug or charging mapping is wrong. Where a slowly polled
odometer reports a trip's last kilometres after a faster trip counter has
stopped, the trip ends with the counter; the odometer's value still gives
the distance. A vehicle set to report per driving cycle is read as legs
instead: each from its departure to the upload at its stop, and legs whose
stops are shorter than T_still make one trip ([glossary](glossary.md),
*Leg*). Every trip carries its own consumption: `fuel_consumed_l` with
its source (`trip_computer` where the trip computer's average is
assigned, else `fuel_level`), quality and possible error, and
`fuel_l_per_100km` on the trip's distance; `battery_consumed_kwh` from
the change in state of charge, and `battery_kwh_per_100km`. A rate is
empty when its source cannot tell it from zero to one step of 0.1 per
100 km — the quantity within its error of zero, and that error over the
distance more than a step — while the quantity and the error are always
there.
Every trip carries its distance with its source and quality (`odometer`
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
([glossary](glossary.md), *Refuelling*) — set the threshold above the
largest rise your level sensor shows without a refuelling, 5 L for a
sensor that errs by 3, or every slosh becomes a candidate; a pump the sensor sees in
several steps is one candidate, from `start`, the first sample that rose,
to `end`, the last. It carries the position and zone where the vehicle
stood, `level_before_l`, `level_after_l` read once T_settle (6 min) has
passed after `end` together with `settled_at`, the time of that reading,
the `sensor_delta_l` between the two — always `estimated`, the receipt
says what was bought — and, with a fuel price role assigned, its value at
`start` as `price_suggestion`. No fuel flap is needed. A rise across a
drive counts as well when the vehicle stopped and started again in
between, or its fuel flap opened — a vehicle that reports its level only
at each stop shows the refuelling at the next one; such a candidate runs
from the reading at the pump to the next, and `flap_opened_at` dates it
to the pump. Its `sensor_delta_l` is then off by what was burnt since and
by the sensor's own error: the receipt says what was bought. A candidate still
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
sessions are printed — one still charging appears once it ends and the
charge point's and the other vehicles' streams have reached that moment,
a heartbeat at the latest ([derivations.md](derivations.md#charging-sessions)).

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
vledger l1 update --vehicle a7c1                 # what the integration runs live: append what completed, print what is new
vledger l1 read --vehicle a7c1 --kind period --current   # the current month, year, rolling and lifetime lines
vledger l1 clean --vehicle a7c1                  # delete l1/ — it is regenerable
```

L1 holds completed events only: a trip is written once its standstill has
elapsed, a refuelling once it has settled, a charging session once it has
ended, judged by the stream's last line rather than the clock, so the
same stream always yields the same files. `l1 status` exits 1 when a
rebuild is due — no manifest, a different library version, a changed
configuration of the vehicle or of a charge point, or changed receipts —
which is what the integration checks
at startup. `l1 update` rebuilds if one is due, else appends from the
cursor, and prints the events new to L1 in order of start — exactly those
that fire `vledger_event` in Home Assistant.

## Exports: `vledger export`

For a spreadsheet, another tool or a map: renderings of L1 as it is on
disk. An export derives nothing — run `derive all --write` first; it says
so when L1 is not current.

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

**In Home Assistant**, the action **Vehicle Ledger: Export**
(`vledger.export`) writes the same files into the instance's media folder,
`media/vledger/<vehicle>/` — where the media browser, in the frontend and
in the companion app, lists them for download, and a dashboard links one
as `/media/local/vledger/<vehicle>/trips.csv`:

```yaml
action: vledger.export
data:
  config_entry_id: 01J…            # the vehicle
  format: csv                      # csv, json or gpx
  kind: trip                       # trip, charging, refuelling or period; not for gpx
  since: "2026-10-01 00:00:00"     # optional, local time, by the event's start
  until: "2026-10-31 23:59:59"     # optional
  filename: october-trips.csv      # optional; default: the kind's name, trips.csv
response_variable: written         # written.path, written.count, written.current
```

Without a file name the file is the kind's own — `trips.csv`,
`charging-sessions.json`, `refuellings.csv`, `periods.csv`, `trips.gpx` —
and the next export of that kind replaces it, so a link or an automation
can point at a name that does not change; a file name of your own keeps
several. A name is a bare name with the format's extension, nothing else:
the action writes nowhere but that folder. The answer says where the file
is, how many events it holds and whether L1 was current; it is refused
while the vehicle has no L1 yet. Exports are copies: a renamed vehicle
gets a new folder, and the old one stays until you delete it.

## Reports: `vledger report`

The metrics of a span you choose — a quarter, a holiday, the time between
two services — computed from the events in L1 and the raw log rather than
read from the periods, so any span works:

```bash
vledger report metrics --vehicle a7c1                                   # the whole of capture
vledger report metrics --vehicle a7c1 --since 2026-07-01T00:00:00Z --until 2026-09-30T23:59:59Z
vledger report metrics --vehicle a7c1 --since 2026-07-01T00:00:00Z --json --out q3.json
```

The table has every metric of a period line
([above](#periods)) with its quality, whether the fuel level and the
state of charge could be read at both bounds, the fuel consumption chosen
among the span's tank-to-tank intervals the way the lifetime line chooses
among all of them — and then every such interval that lies in the span:
its two receipts, how many it spans, litres per 100 km, its possible
error, and the mean outside temperature between the two refuellings. The
charge cycles and tank fills are the span's own; the starting values from
before capture are the lifetime line's. `--since` and `--until` are
inclusive and default to the stream's first and last line; `--json` gives
the same as one object, `report` and `intervals`, for a spreadsheet or
a script. Like an export, a report reads L1 as it is and says when it is
not current.

## The atoms: `vledger calc`

The computations the derivations are built from, each callable on its own
so any step can be checked by hand:

```bash
vledger calc distance 51.4437 7.1413 51.4812 7.2166     # great-circle, km
vledger calc convert 630 mi --quantity distance         # 1013.89 km
vledger calc convert 72 "°F" --quantity temperature     # 22.2222 °C
vledger calc convert 7.5 "l/100km" --quantity consumption   # 7.5 L/100 km
```
