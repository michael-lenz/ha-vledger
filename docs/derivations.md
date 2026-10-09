# The derivations

*Design document — how each detection works as built: the algorithm, the
roles and parameters it reads, the quality flag each value gets, and what
it cannot see. The files it writes are [l1-format.md](l1-format.md), the
terms the [glossary](glossary.md); the reasoning is in the decisions of
the project's register, cited by number.*

**Status:** trips, refuelling candidates, charging sessions, receipt
matching and the periods' metrics are built and described here as built.
Gap detection is L0's ([l0-format.md](l0-format.md)) and is described
here only as the derivations use it.

## What every derivation reads

A derivation reads a stream once, as series ([`series.py`](../src/vledger/series.py)),
and never parses a line itself:

- **Measuring roles** (`odometer`, `trip_distance`, `fuel_level`, `soc`,
  `outside_temperature`, `fuel_price`, `energy_meter`, …) become numeric
  samples in L1 units (km, L, kWh, %, °C), converted from the unit the
  line states. A fuel level in % becomes litres of `tank_capacity_l`;
  without a capacity there is no fuel series at all, and the refuelling
  detection says why rather than guess (ISSUE-0009). A state that is not
  a number — `unavailable`, `unknown` — is a **dropout**, not a value:
  the series holds the last number through it.
- **Positions** become fixes: latitude, longitude, accuracy (an accuracy
  of 0 is unknown) and the tracker's zone, from the `position` role or the
  sensor pair `position_latitude` and `position_longitude`.
- **Enumerated roles** (`ignition`, `plug_state`, `charging_state`) become
  domain states through the configured map (ADR-0008): a mapped value is
  the positive state, `unavailable` and `unknown` hold the last known
  state, anything else is the negative state.
- A **start line's snapshot** seeds every series with the value before its
  first change; a derivation from a cursor is seeded with the last value
  of every role before it (ADR-0009, point 3).
- **Capture gaps** come from the markers: a start without a stop
  (`crash`), a stop and the next start (`stopped`), silence longer than
  `heartbeat_s` plus 300 s while running (`silence`), and a stream that
  ends without a stop longer ago than that (`open`). **Nothing is read
  across a gap** (ABL-04): a value on its far side is never a value at
  this side, and an event that spans one is `incomplete`.
- **Completion is judged by the stream's last line, never by the clock**
  (ABL-01): an event is complete once its own closing condition has
  elapsed in the stream, so the same stream yields the same events
  whenever it is asked — the property the determinism test checks.

## Quality flags

Every derived value carries one (ABL-03), from strongest to weakest:

| flag | means |
|---|---|
| `receipt` | stated by a person on a receipt |
| `measured` | read from a sensor, or computed exactly from readings |
| `estimated` | computed through an assumption: a sensor delta, a loss factor, a straight line between fixes, a stock change |
| `incomplete` | something it rests on is missing — a capture gap inside it, a price that is not there |

An event's envelope `quality` is `incomplete` when the event crosses a
capture gap, `receipt` when it was made from a receipt alone, else
`measured`; a period's is the weakest of its metrics. A value computed
from several carries the weakest of them.

## Trips

**Reads:** movement roles `odometer`, `trip_distance`, `position` (or the
sensor pair); markers `ignition`, `plug_state`, `charging_state`;
`soc`, `fuel_level`, `outside_temperature`. **Thresholds:** `t_still_s`
(T_still, 1800 s), `t_settle_s` (360 s).

1. **Movement events.** An odometer sample higher than the previous one;
   a trip counter sample higher than the previous one (going down is a
   reset, not a movement); a fix at least 50 m from the previous fix, so
   GPS jitter at rest is not a trip.
2. **Spans.** A trip runs from the first movement event after a
   standstill to the last before the next; a standstill is at least
   T_still without a movement event. A capture gap between two movements
   always ends the span before it and starts a new one. The trip after a
   gap is `incomplete`, since it may have begun inside it; the trip before
   it too when the gap begins within T_still of its last movement, since
   its end is then unknown. That answer is fixed the moment the trip
   completes, whatever the stream holds later (ISSUE-0014).
3. **Refining by markers** (ADR-0012). A start marker — ignition turning
   `on`, the plug turning `unplugged` — within T_still before the first
   movement moves the start back to it, the latest such marker winning; an
   end marker — ignition `off`, plug `plugged`, charging state `charging`
   — within T_still after the last movement moves the end forward to it,
   the earliest winning. Each is a time the vehicle was not driving, so
   each bounds the true boundary; none ever crosses the previous trip's
   end, and none makes a trip without movement. `refined_by` names the
   role that moved each end, or `null`.
4. **Distance** (FAH-04), the first that works: the odometer at the end
   minus the odometer before the first movement — `measured`, source
   `odometer`; else a trip counter that only rose over the trip —
   `measured`, `trip_counter`; else the straight lines between the
   waypoints — `estimated`, `waypoints`. The value before the trip is the
   last sample before its first movement, unless a gap lies between them.
5. **The rest.** Waypoints are the fix before it moved, then every fix
   inside the trip; start and end position and zone are the first and
   last of them. `outside_temperature_c` is the mean of the samples
   inside it. `delta_soc_pct` and `delta_fuel_l` are the value once
   `t_settle_s` has passed after the end minus the value before the start
   — `estimated` always (FAH-05). `movements_while_plugged` counts
   movement events inside the trip while plug or charging state says the
   vehicle could not drive: sample timing or a wrong mapping, reported and
   never corrected (ADR-0012, point 5).
6. **Complete** once T_still has elapsed after the last movement, by the
   stream.

**Limitations.**

- **A stop shorter than the sampling interval merges into the trip**
  (FAH-06): two drives with a stop between them that no sample falls into
  are one trip, and a stop shorter than T_still is one trip by design.
  This is a limit of the source, not a defect.
- A trip starts at the first sample that moved, not when the wheels
  turned; with an odometer sampled every 15 minutes the start is late by
  up to that much unless a marker refines it. The same holds for the end.
- A trip of a single movement sample has `start` = `end` and the distance
  from before it.
- Without an odometer or trip counter, the distance is the chord between
  fixes and is short on every curve.

## Refuelling candidates

**Reads:** `fuel_level` (litres, or % with `tank_capacity_l`),
`odometer` — or, without one, every movement role — `position`,
`fuel_price`. **Thresholds:** `refuel_threshold_l` (3 L), `t_settle_s`
(T_settle, 360 s).

1. **Rises.** A step between two consecutive fuel samples of at least
   `refuel_threshold_l`, while the odometer did not go up between them —
   without an odometer, while no movement event fell between them.
2. **One refuelling, several steps.** Rises that follow one another within
   T_settle at unchanged odometer are one candidate: a pump the sensor
   sees in steps. It starts at the first rise and ends at the last.
3. **Settling** (TNK-02). The level after is the value in effect once
   T_settle has elapsed after the last rise; if the sensor had dropped
   out by then, the first value after it came back. A gap before that
   reading leaves it unread and the candidate `incomplete`.
4. **Values.** `level_before_l` is the sample before the first rise,
   `sensor_delta_l` the settled level minus that — `estimated` always.
   `position` and `zone` are the fix at the start; `price_suggestion` is
   the `fuel_price` in effect at the start (TNK-05), a suggestion for the
   receipt and nothing more.
5. **Complete** once the level after has been read, by the stream. No
   fuel flap is needed (TNK-03).

**Limitations.** A rise between two odometer samples taken while driving
looks like one at rest; the threshold is what keeps a sloshing tank from
being a refuelling, and a top-up smaller than it is not seen. A sensor
that reports in coarse steps (`fuel_level_resolution_l`) makes the delta
coarse too — which is why the receipt's litres come first.

## Charging sessions

**Reads:** `charging_state` — or, without one, `soc` — `soc`, `position`,
the movement roles; the charge points' streams (`energy_meter`) and
configuration (position, `radius_m`, `meter`, `tariffs`); the other
vehicles' streams. **Parameters:** `battery_net_kwh`,
`charging_loss_factor`. **Thresholds:** `charging_threshold_pct` (2 %),
`t_still_s`.

1. **Boundaries** (LAD-03). From the charging state turning to `charging`
   to the next change away from it; `unavailable` and `unknown` hold, so
   a dropout mid-charge does not end it. A session still charging at the
   stream's last line is not complete.
2. **Without a charging state** (LAD-04): a run of SoC rises at standstill
   whose total exceeds `charging_threshold_pct`. A run is broken by a
   sample that does not rise, a movement, a capture gap, or T_still
   without a rise — L0 logs only changes, so a run that simply stopped
   rising has no sample to say so. It is complete once broken, by the
   stream; it never crosses a gap.
3. **SoC.** At the start, the last sample at or before it (for an SoC
   session, the value before the first rise); at the end, the last at or
   before it. `battery_kwh` is ΔSoC × `battery_net_kwh` — `estimated`.
4. **Where** (LAD-02). The position at the start places the session at the
   configured charge point whose radius holds it, the nearest if several
   do, else at `foreign`.
5. **Grid-side energy** (LAD-06), the first that works: the charge point's
   meter difference over the session — `measured`, source `meter` — but
   only when no other vehicle charged at that charge point at any time
   during it (LAD-07, `meter_attributable`), the meter did not go
   backwards and its stream has no gap there; else `battery_kwh` ×
   `charging_loss_factor` — `estimated`, `loss_factor`; else none. A
   receipt, when one meets the session, comes before both (*Matching*,
   below).
6. **Cost** (LAD-08). The tariff valid at the session's start, never
   re-priced; a tariff of 0 costs exactly 0 whatever the energy. Its flag
   is the energy's. With a measured energy, `kwh_per_pct` and
   `charging_loss_kwh` follow (LAD-09).
7. `movements_while_charging` counts movement events inside the session:
   reported, never corrected (ADR-0012, point 5).

**Limitations.** A session's cost depends on the charge points'
configuration and the other vehicles' streams, and a change there does not
make a rebuild due (ISSUE-0013). A position fix taken before the vehicle
arrived places the session where that fix was. By SoC, a session ends
with the last rise, so a charge that stopped short of a sample ends early,
and a rise below the threshold is not a session.

## Matching receipts

Every derivation pairs the current receipts with the detected refuellings
and charging sessions, by anchor time to the nearest event within
`matching_tolerance_s` (6 h), never guessing: a receipt with two nearest
events, or two receipts with one, is `ambiguous` and assigned to nothing.
A matched event carries the receipt's values first, flagged `receipt`,
the sensor value beside them and a plausibility check against
`plausibility_pct` (15 %); a receipt that meets nothing is an event of its
own (ADR-0013). The rules are [receipts-format.md](receipts-format.md#matching),
what the events carry [l1-format.md](l1-format.md#receipts-in-events).

## Metrics

The periods are computed from the event files after every derivation
(ADR-0014): every event in the one period its start falls in, the fuel
level and SoC read at the boundaries for the stock correction, never
across a gap, and each metric flagged with the weakest of what it rests
on. Fuel consumption is tank to tank between receipts, corrected by the
level sensor, with its error bound beside it. The definitions are
[l1-format.md](l1-format.md#periods).

**Limitations.** Counts over a period with a capture gap are lower bounds,
and `gaps` says how many there were. Without `battery_net_kwh` there is no
battery-side energy, without `tank_capacity_l` no tank fills, without
`fuel_level_resolution_l` no error bound on a partial-fill consumption.

## Where these are tested

Every detection has its scenarios in `tests/`, each stream built through
the verbs (ADR-0005), and the determinism test for each kind replays a
stream line by line against one rebuild. Real streams, anonymised with
`vledger l0 anonymise`, run through the same checks as fixtures
([developing.md](developing.md#real-streams-as-fixtures)).
