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

- **Measuring roles** (`odometer`, `trip_distance`, `trip_consumption`,
  `fuel_level`, `soc`, `outside_temperature`, `fuel_price`, `energy_meter`,
  …) become numeric samples in L1 units (km, L, kWh, %, °C, L/100 km),
  converted from the unit the line states. A fuel level in % becomes litres of `tank_capacity_l`;
  without a capacity there is no fuel series at all, and the refuelling
  detection says why rather than guess (ISSUE-0009). A state that is not
  a number — `unavailable`, `unknown` — is a **dropout**, not a value:
  the series holds the last number through it, and so does every value
  read from it (ADR-0046) — a stock at a period boundary, a price at a
  refuelling. Two readers treat a dropout as a clearing and say so: the
  settled level of a refuelling waits for the first number after it, and
  the outside-temperature filter restarts at the next sample.
- **Positions** become fixes: latitude, longitude, accuracy (an accuracy
  of 0 is unknown) and the tracker's zone, from the `position` role or the
  sensor pair `position_latitude` and `position_longitude`.
- **Enumerated roles** (`ignition`, `plug_state`, `charging_state`,
  `engine`, `lock`, `in_use`, `fuel_flap`) become
  domain states through the configured map (ADR-0008): a mapped value is
  the positive state, `unavailable` and `unknown` hold the last known
  state, anything else is the negative state.
- A **start line's snapshot** seeds every series with the value before its
  first change, read at the entry's `since` but no earlier than the
  stream's last line before the start, so that a restart never reaches
  back into an event already completed (ADR-0028); an entry that repeats
  the value its role already holds is dropped. An entry whose `since` is
  not later than that last line held through the gap — Home Assistant kept
  the value while capture was not listening — and is a sample at the start
  line's time as well, repeated or not (ADR-0035): what is in effect after
  a restart is read from the start line on, not from the role's next
  change. A derivation from a cursor is seeded with the last value of
  every role before it (ADR-0009, point 3).
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
capture gap that could hide a standstill — any gap for a sampled
vehicle's trip, one of T_still or longer for a per-cycle trip or a
charging session (ADR-0054) — `receipt` when it was made from a receipt alone, else
`measured`; a period's is the weakest of its metrics. A value computed
from several carries the weakest of them.

## Trips

**Reads:** movement roles `odometer`, `trip_distance`, `position` (or the
sensor pair), and `in_use`; markers `ignition`, `plug_state`,
`charging_state`, `engine`, `lock`;
`soc`, `fuel_level`, `trip_consumption`, `outside_temperature`.
**Thresholds:** `t_still_s` (T_still, 1800 s), `min_move_m` (50 m),
`t_settle_s` (360 s), `trip_consumption_step_l_per_100km` (0.1).
**Parameters:**
`fuel_level_resolution_l`, `soc_resolution_pct` (1), `battery_net_kwh`.

1. **Movement events.** An odometer sample higher than the previous one;
   a trip counter sample higher than the previous one (going down is a
   reset, not a movement); a fix at least `min_move_m` from the previous
   fix, so GPS jitter at rest is not a trip — a tracker that jitters more
   is tuned there (ADR-0030); the first and the last report of a
   span the vehicle was reported in use (ADR-0021) — the last being the
   `reported_before` of the line that leaves it.
2. **Spans.** A trip runs from the first movement event after a
   standstill to the last before the next; a standstill is at least
   T_still without a movement event, and none lies inside a span in use,
   however far apart its reports are. A span still open at the stream's
   end is movement up to its last line, and the trip it belongs to is not
   complete. A capture gap between two movements
   ends the span before it and starts a new one. The trip after a
   gap is `incomplete`, since it may have begun inside it; the trip before
   it too when the gap begins within T_still of its last movement, since
   its end is then unknown. That answer is fixed the moment the trip
   completes, whatever the stream holds later (ISSUE-0014). A gap the
   vehicle's distance source held through — the first of odometer, trip
   counter and position the stream holds has, at the gap's end, the
   restart's reading that repeats its last one before the gap (ADR-0035)
   — with no movement of any role inside it, is **bridged**: it is no gap
   to the trips (ADR-0037). It neither ends a span nor marks a trip, a
   standstill runs through it, and two readings that agree stand in for
   the span of nothing between them. Everything else — `gaps`, the
   periods, charging sessions and refuellings — reads the gap as before.
3. **Refining by markers** (ADR-0012, ADR-0021). A start marker —
   ignition turning `on`, the plug turning `unplugged`, the lock turning
   `unlocked`, the engine turning `running` — within T_still before the first
   movement moves the start back to it, the latest such marker winning; an
   end marker — ignition `off`, plug `plugged`, charging state `charging`
   — within T_still after the last movement moves the end forward to it,
   the earliest winning. Each is a time the vehicle was not driving, so
   each bounds the true boundary; none ever crosses the previous trip's
   end, and none makes a trip without movement. A marker written in the
   same poll as the boundary movement — within a second on its far side,
   in whichever order Home Assistant wrote the two lines — bounds the
   trip too: it moves nothing, the movement being the outer line, but
   `refined_by` names it (ISSUE-0031). Lock and engine are never
   end markers: the car locks itself on driving off, and an engine stops
   while a hybrid drives on. `refined_by` names the role that moved each
   end, or `null`.
   The last movement is the trip's, except a slower role's late report
   (ADR-0021, point 4): a sample later than a faster role's last movement
   by no more than its own measured interval — its old value was last
   heard no later than that movement — does not move the end; the faster
   role had stopped changing, so the vehicle had stopped. Roles are ranked
   by the median interval the stream measures for them (ADR-0011) between
   the previous trip's end and this trip's last movement, a window the
   live path and a rebuild see alike. The late value still counts: the
   distance, the waypoints and the deltas read up to it.
4. **Distance** (FAH-04 as ADR-0055 reads it), the first that works: the
   trip counter's difference where the counter only rose over the trip
   and lies within one odometer step — the place of the odometer's
   finest decimal, 1 km on one that reports whole kilometres — of the
   odometer's difference, `measured`, source `trip_counter`; else the
   odometer at the end minus the odometer before the first movement —
   `measured`, `odometer`; else the monotone counter alone; else the
   straight lines between the waypoints — `estimated`, `waypoints`. The
   value before the trip is the last sample before its first movement,
   unless a gap lies between them.
5. **The rest.** Waypoints are the fix before it moved, then every fix
   inside the trip; start and end position and zone are the first and
   last of them. `outside_temperature_c` is the mean of the samples
   inside it. `delta_soc_pct` and `delta_fuel_l` are the value after the
   trip minus the value before the start — `estimated` always (FAH-05);
   the value after is the latest sample not later than `t_settle_s` past
   the trip's end, its arrival, and not later than a charging session or
   refuelling begun since (ADR-0051): for the SoC the sample before the
   next `charging` marker, for either the sample before its next rise.
   `delta_fuel_15c_l` is the fuel delta at 15 °C ([Fuel at
   15 °C](#fuel-at-15-c)). `movements_while_plugged` counts movement
   events inside the trip while plug or charging state says the vehicle
   could not drive — the upload that plugged it in aside, whose plug line
   precedes its movement lines by milliseconds — sample timing or a wrong
   mapping, reported and never corrected (ADR-0012, point 5). `gaps`
   counts the capture gaps inside the trip, whatever their length.
6. **Consumption** (ADR-0025, ADR-0026) — the trip's own figure, never the
   vehicle's, which is tank to tank ([Metrics](#metrics)). The fuel the
   trip used, `fuel_consumed_l`, comes from the **trip computer** where
   `trip_consumption` is assigned: the fuel since the counter's reset at
   a reading is `trip_distance` × the average / 100, the trip's fuel is
   that at the settled end minus that before the start — zero where the
   counter went down in between, its reset — `measured`, source
   `trip_computer`, with an error of half the average's display step over
   the distance at each reading. Else it is −`delta_fuel_l` plus the
   `sensor_delta_l` of every refuelling candidate inside the trip — the
   level delta is bought minus burnt (ADR-0053); `fuel_refuelled_l` says
   how much — `estimated`, source `fuel_level`, error
   2 × `fuel_level_resolution_l` once more per fill; without that
   parameter the level gives no figure. `battery_consumed_kwh` is
   −`delta_soc_pct` / 100 × `battery_net_kwh`, `estimated`, error
   2 × `soc_resolution_pct` / 100 × the capacity, negative when the
   battery gained. Each quantity has its error beside it whatever its
   size; the rates `fuel_l_per_100km` and `battery_kwh_per_100km`, on the
   trip's distance, are `null` where the quantity is within its error of
   zero *and* that error over the distance is more than one step of the
   rate, 0.1 per 100 km (`trips.RATE_STEP`) — a figure right to the step
   it is shown in is shown whatever its sign, so an electric trip read
   from the trip computer is 0, not `null` — and carry the error as a
   share, `…_error_pct`, where the quantity is not zero. A trip across a
   gap is `incomplete` in all of them — for a sampled vehicle any gap,
   for a per-cycle vehicle one of T_still or longer (ADR-0054).
7. **Complete** once T_still has elapsed after the last movement, by the
   stream — a use reported from within T_still of it keeps the trip open,
   one that began later belongs to the next trip (ADR-0048, point 3).

**Per-cycle vehicles** (ADR-0024). A vehicle whose `movement_reporting`
is `per_cycle` uploads odometer, trip counter and position once per
driving cycle, at the stop, and is read as legs instead of steps 2 and 3:

1. **Arrivals.** Only a movement event (step 1, in use aside) of the
   fastest movement role opens an arrival — the role with the shortest
   median interval the stream measures between the previous trip's end
   and the event — and joins the current one while no departure marker
   lies between. A slower role's event belongs to the latest arrival
   before it when no departure lies between them, else to the next one
   when none lies between those, else it opens its own: a slow poll
   reports the upload before or after the fast one, whichever way the
   next departure falls. A value that replaces a dropout measures the
   dropout, not the role's cadence, and is left out of the intervals
   (ADR-0052). Without measured intervals every event after a
   departure opens an arrival. The arrival's time is its fastest event,
   its values are read up to its last. Trips are read one after another,
   each from the previous one's end — the live path's cursor — so a late
   report after a trip's end belongs to it and opens nothing.
2. **Departure markers.** The lock turning `unlocked`, the engine
   `running`, the ignition `on`, the plug `unplugged`, and the trip
   counter going down. An unlock within `exit_window_s` (300 s) of an
   arrival, either side, is the driver getting out: it separates nothing
   and departs nothing — before the arrival only when no departure
   marker or in-use report lies between, since a first leg can be
   shorter than the window (ADR-0057). An unlock that T_still later has
   produced no sign of driving — another marker, an in-use report, a
   movement — was a door and departs nothing either (ADR-0050); a marker
   of another role is a door only when, with no such sign within
   T_still, a later departure marker precedes the next movement — a
   drive ends in an arrival, a door in the next departure (ADR-0057).
   Until the stream knows, a marker is a departure like any other.
3. **Legs.** A leg's departure is the latest departing unlock after the
   previous arrival; without one, the earliest of the other markers and
   the first in-use report after it; without any, the leg's start is
   unknown and it starts at its arrival.
4. **Trips.** Consecutive legs whose stops — from an arrival to the next
   departure — are each shorter than T_still are one trip, from the first
   departure to the last arrival; a capture gap in a stop ends the trip,
   one of T_still or longer inside it makes it `incomplete` — a shorter
   one hides no standstill, is counted in `gaps` and leaves it `measured`
   (ADR-0054). `refined_by.start` names the role that departed,
   `refined_by.end` is `null`. Steps 4 to 6 above give the values.
5. **Complete** once the stream's last line is T_still past the last
   arrival, no departure followed within T_still, and the vehicle is not
   reported in use by a span that began within T_still of the arrival — a
   use that began later is the next trip's (ADR-0048, point 3).
6. **Under way.** A departure after the last arrival, with the engine
   running, the ignition on or the vehicle in use at the stream's last
   line, is a drive under way from that departure (ADR-0048, point 1): the
   periods read the stock no later than it, as for a trip not yet
   complete. An unlock alone is a door.

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
- A trip computer reset by the driver in the middle of a trip, on a
  vehicle that samples while driving, loses what was burnt before the
  reset; on a per-cycle vehicle resets come with departures and lose
  nothing. A source that reports its average in miles per gallon is not a
  factor away from L/100 km and stays unread.

### A trip's cost

Computed when a trip is read — by `vledger.read_events`, `vledger derive
trips` and the exports — and never stored in L1 (ADR-0043): a receipt
entered after the trip prices it the next time it is read, and a finished
event is not reopened. Prices are the last known at the trip's start,
from the receipts and sessions L1 holds:

- `fuel_cost_eur` — `fuel_consumed_l` × the `unit_price` of the last
  refuelling with a receipt starting at or before the trip's start, the
  rule a period uses when it has no receipt of its own
  ([Metrics](#metrics)); its quality is the consumption's.
- `electricity_cost_eur` — `battery_consumed_kwh` × `charging_loss_factor`
  × `cost_eur` / `grid_kwh` of the last priced charging session starting
  at or before the trip's start; `estimated` at best. A battery that
  gained gives a negative cost: that energy is priced again when a later
  trip spends it.
- `cost_eur` — their sum, as good as the weaker.

A quantity within its error of zero — |`fuel_consumed_l`| ≤
`fuel_consumed_error_l`, |`battery_consumed_kwh`| ≤
`battery_consumed_error_kwh` — costs 0, `estimated`, at any price, though
its rate stays withheld (ADR-0059): the sensors bound the cost to the
error's price, and at the cent that is zero. A part is `null` and
`incomplete` when its quantity is not read, when a quantity beyond its
error has no shown rate (step 6: no distance) or, for anything but zero,
when no price is known before the trip; the sum is then `null` too. An energy the
vehicle does not use — no `fuel`, or no `battery_net_kwh` — is `null`
without a quality and stays out of the sum. Amounts are in the instance's
currency ([l1-format.md](l1-format.md#files)).

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
   `sensor_delta_l` the settled level minus that — `estimated` always;
   `sensor_delta_15c_l` the same at 15 °C ([Fuel at 15 °C](#fuel-at-15-c)).
   `position` and `zone` are the fix of the start's own upload — the
   first fix within seconds after it, else the last before; `price_suggestion` is
   the `fuel_price` in effect at the start (TNK-05), a suggestion for the
   receipt and nothing more.
5. **Complete** once the level after has been read, by the stream. No
   fuel flap is needed (TNK-03).
6. **Across a stop** (ADR-0022). A rise of at least the threshold between
   two consecutive samples across movement is a candidate too when a
   start marker (step 3 of trips) or an opening of the `fuel_flap` lies
   between them — the vehicle stopped and started again, as at a pump on a
   vehicle that reports its level once per driving cycle. It spans the two
   samples, its level after is the later one (`settled_at` its time),
   `position` is the fix at the earlier one. `flap_opened_at` is the
   flap's opening between them, on any candidate; a receipt is matched to
   that moment rather than to the span.

**Limitations.** A rise between two odometer samples taken while driving
looks like one at rest; the threshold is what keeps a sloshing tank from
being a refuelling, and a top-up smaller than it is not seen. A level
that errs by more than its display step — `fuel_level_resolution_l` is
that error, not the step ([glossary](glossary.md), *Sensor resolution*) —
makes the delta err by twice as much, which is why the receipt's litres
come first; a rise within the error looks like a top-up, which is why the
threshold has to exceed it.

## Fuel at 15 °C

Fuel expands with its temperature, so a level sensor reads more litres on
a hot afternoon than the same fuel holds at night. Beside a refuelling's
`sensor_delta_l` and a trip's `delta_fuel_l` stands the same delta at
15 °C — `sensor_delta_15c_l`, `delta_fuel_15c_l` — as a plausibility
figure (VER-08, ADR-0040). It feeds nothing: not a trip's consumption,
not a period's metrics, not receipt matching or the plausibility check
against a receipt, which stay on the raw values and the receipts (VER-01).

1. **The formula.** V₁₅ = V / (1 + β·(T − 15)), β the vehicle's
   `beta_per_k` (petrol 9.5·10⁻⁴ /K, diesel 8.0·10⁻⁴ /K). The delta is V₁₅
   of the later reading at its time minus V₁₅ of the earlier at its own:
   for a refuelling the settled level at `settled_at` and the level
   before; for a trip the two readings `delta_fuel_l` is taken from.
2. **The temperature.** T is the `outside_temperature` through a
   first-order low-pass filter with time constant `temperature_tau_s`
   (default 3 h), exact for a value that holds until the next sample:
   between two samples the filtered value y moves toward the held value x
   as y ← x + (y − x)·e^(−Δt/τ).
3. **A fixed window.** The filter runs over [t − 5τ, t] only, started at
   the value in effect at the window's start, or without one at its first
   sample. Its answer is then a function of L0 alone, the same whether a
   derivation reads the whole stream or from its cursor; a live run whose
   cursor lies inside the window reads the window from L0 itself. What the
   cut-off drops weighs e⁻⁵ ≈ 0.7 % of a temperature difference.
4. **Nothing across a gap.** A dropout or capture gap inside the window
   restarts the filter at the next sample. One after the last sample, or
   no value in effect at the window's start and no sample in it, leaves no
   temperature — and the delta `null`, as it is without an
   `outside_temperature` role or without either fuel reading.

**Limitations.** The outside temperature stands in for the fuel's, which
it reaches only slowly; fresh fuel from an underground tank is nearer
15 °C than the air on a summer day. The figure says how much of a delta
the weather can explain, not what the tank holds.

## Charging sessions

**Reads:** `charging_state` — or, without one, `soc` — `soc`, `position`,
the movement roles; the charge points' streams (`energy_meter`) and
configuration (position, `radius_m`, `meter`, `power_kw`, `tariffs`); the other
vehicles' streams. **Parameters:** `battery_net_kwh`,
`charging_loss_factor`. **Thresholds:** `charging_threshold_pct` (2 %),
`t_still_s`.

1. **Boundaries** (LAD-03). From the charging state turning to `charging`
   to the next change away from it; `unavailable` and `unknown` hold, so
   a dropout mid-charge does not end it. A session still charging at the
   stream's last line is not complete, and neither is one whose end a
   stream it reads has not reached yet (step 8). A capture gap of T_still
   or longer inside a session makes it `incomplete`; a shorter one is
   counted in `gaps` and leaves it `measured` (ADR-0054).
2. **Without a charging state** (LAD-04): a run of SoC rises at standstill
   whose total exceeds `charging_threshold_pct`. A run is broken by a
   sample that does not rise, a movement, a capture gap, or T_still
   without a rise — L0 logs only changes, so a run that simply stopped
   rising has no sample to say so. It is complete once broken, by the
   stream; it never crosses a gap.
3. **SoC.** At the start, the last sample at or before it (for an SoC
   session, the value before the first rise); at the end, the last at or
   before it. `battery_kwh` is ΔSoC × `battery_net_kwh` — `estimated`.
4. **Where** (LAD-02, ADR-0044). The session's position is the first fix
   inside it, provided no capture gap and no movement lies between the
   start and that fix — a movement of the start's own upload aside: one
   less than an interval of the fastest movement role after the start,
   ranked from the end of the last trip before the start, as trips rank
   it (ADR-0024). Without a measured interval, any movement counts.
   Failing that, the last fix at or before the start, unless a gap lies
   between. The position places the session at the configured charge
   point whose radius holds it, the nearest if several do, else at
   `foreign`; so does no position at all.
5. **Grid-side energy** (LAD-06), the first that works: the charge point's
   meter difference over the session — `measured`, source `meter` — but
   only when no other vehicle charged at that charge point at any time
   during it (LAD-07, `meter_attributable`), the meter did not go
   backwards and its stream has no gap there; else the charging time ×
   the charge point's declared `power_kw` — `estimated`,
   `charge_point_power` (ADR-0038) — but only for a session the charging
   state bounds, not across a capture gap, and again only when no other
   vehicle charged there meanwhile; an upper bound where the rate tapers
   near full; else `battery_kwh` × `charging_loss_factor` — `estimated`,
   `loss_factor`; else none. A receipt, when one meets the session, comes
   before all of them (*Matching*, below).
6. **Cost** (LAD-08). The tariff valid at the session's start, never
   re-priced; a tariff of 0 costs exactly 0 whatever the energy. Its flag
   is the energy's. With an energy from the meter or the declared power,
   `kwh_per_pct` and `charging_loss_kwh` follow (LAD-09) — never from the
   loss factor, and neither feeds back into a parameter (LAD-10).
7. `movements_while_charging` counts movement events inside the session:
   reported, never corrected (ADR-0012, point 5).
8. **Complete** once the session's own boundary has passed (steps 1 and
   2) and every stream it reads has reached its end (ADR-0027, point 2):
   the charge point's where it has a meter, and every other vehicle's. A
   stream has reached a time when it holds a line at or after it, or
   ends in a `stop` before it, or had fallen silent by then — an `open`
   gap, its last line further behind than its heartbeat interval plus
   tolerance, judged against the vehicle's own last line, never the
   clock. Those streams are read against that last line, so the live path
   and a rebuild judge them alike; a meter is not read across the open gap
   of a charge point whose capture died. In Home Assistant that is a
   heartbeat at the latest. A change to a charge point's configuration
   makes a rebuild of every vehicle's L1 due ([l1-format.md](l1-format.md#when-l1-is-rebuilt)).

**Limitations.** A vehicle that moved before its first fix inside the
session, outside the start's upload, is placed by the fix before the
start, which may predate its arrival. By SoC, a session ends
with the last rise, so a charge that stopped short of a sample ends early,
and a rise below the threshold is not a session.

## Matching receipts

Every derivation pairs the current receipts with the detected refuellings
and charging sessions, by anchor time to the nearest event within
`matching_tolerance_s` (6 h), never guessing: a receipt with two nearest
events, or two receipts with one, is `ambiguous` and assigned to nothing.
A matched event carries the receipt's values first, flagged `receipt`,
the sensor value beside them and a plausibility check against
`plausibility_pct` (15 %); a receipt whose nearest is a candidate not yet
complete waits for it, nowhere in L1 until then (ADR-0061); a receipt
that meets nothing is an event of its own (ADR-0013). The rules are
[receipts-format.md](receipts-format.md#matching),
what the events carry [l1-format.md](l1-format.md#receipts-in-events).

## Metrics

The periods are computed from the event files after every derivation
(ADR-0014): every event in the one period its start falls in, the fuel
level and SoC read at the boundaries for the stock correction, never
across a gap and never inside an event not yet complete (ADR-0033,
ADR-0034), and
each metric flagged with the weakest of what it rests on. The fuel a
period consumed is the sum of its trips' trip-computer figures where
every trip has one, else the level's stock correction, the source named
on the line (ADR-0049). Fuel consumption is tank to tank between receipts, corrected by the
level sensor, with its error bound beside it. The definitions are
[l1-format.md](l1-format.md#periods). The same arithmetic over a span
chosen freely is the report, `vledger report metrics`, computed from the
event files and the stream on demand; it also lists every tank-to-tank
interval in the span with the mean outside temperature between its two
refuellings (VER-09).

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
