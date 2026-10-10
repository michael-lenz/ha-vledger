# Glossary

*Design document — the one English spelling of every domain term, fixed
here so code, documentation and the requirements register cannot drift
apart (ADR-0001). A term used anywhere in this repository means what this
page says; a new term is added here before it is used.*

## Data and layers

**L0, the raw log.** Unprocessed state changes, markers and configuration
states, as JSON Lines, one stream per vehicle and per charge point, rotated
by UTC month. Written once, never modified, never deleted.

**L1, the derivation.** Events and metrics computed from L0, receipts and
configuration — and from nothing else. Files next to L0; deletable and
regenerable at any time. The batch derivation's L1 is authoritative; what
Home Assistant shows is a rendering of it.

**Marker.** An L0 record that is not a state: the *start marker* with a
snapshot of every assigned role, the *stop marker* at orderly shutdown, and
the periodic *heartbeat*. A **capture gap** is a span the markers show
nothing was captured in; nothing is interpolated across one.

**Configuration state.** The complete configuration of a vehicle or charge
point, written to L0 whenever any of it changes, so a derivation can read
the configuration that was in force at any moment.

**Schema version.** The version of the L0 record layout, carried by every
record; every earlier version stays readable.

## Sources

**Source entity.** An entity of any other integration that reports vehicle
or charge point data. The system reads it and never asks it to refresh.

**Role.** The domain meaning of a source entity: `odometer`, `position`,
`trip_distance`, `fuel_level`, `soc`, `charging_state`, `plug_state`,
`ignition`, `engine`, `lock`, `in_use`, `fuel_flap`, `outside_temperature`,
`fuel_price` for a vehicle;
`energy_meter` for a charge point — and `power`, which nothing assigns
yet (ISSUE-0023). Every role is optional; what a vehicle can do follows
from the roles it has.

**Movement role.** `odometer`, `position` or `trip_distance` — a role whose
change means the vehicle moved. At least one is mandatory.

**Sampling interval.** The actual time between two updates of a source
entity, as measured, not as promised: a state line's time minus the last
report of the value it replaces. It bounds what can be detected: T_still
must be at least twice the longest sampling interval of the movement
roles. It is Home Assistant's update cadence; a source that updates more
slowly than it is polled shows its own only through `measured_at`.

**Change interval.** The time between two changes of a role's value — two
of its L0 lines. A change happens at an update, but not at every one, so
the change interval is an upper bound on the sampling interval and never
stands in for it.

**State mapping.** For an enumerated role such as `charging_state`, the
configured correspondence between the source's values and the domain
states (`charging`, not charging).

## Events

**Event.** A trip, a charging session or a refuelling — the things the
derivation finds in L0.

**Trip.** The span between two standstills, with start and destination,
zones, distance, waypoints and mean outside temperature.

**Standstill.** A span of at least **T_still** in which no movement role
changes and the vehicle is not reported in use.

**Leg.** On a vehicle that reports once per driving cycle (ADR-0023): one
drive from its **departure** — an unlock, an engine start, the trip
counter's reset — to its **arrival**, the upload of odometer, trip counter
and position at the stop. An unlock just before or after an arrival is the
driver getting out. Legs whose stops are shorter than T_still are one
trip; a stop is measured from the arrival to the next departure.

**In use.** The vehicle's own report that it is being used — on the
reference vehicle the integration's car connection, `car_in_use`. A span in
use is movement from its first report to its last, however far apart they
are (ADR-0021); it is no movement role, so it neither satisfies the
mandatory one nor gives a distance. The T_still rule counts its interval:
at 30 minutes, T_still has to be 60.

**Not-driving marker.** A change of ignition, plug state, charging state, lock or engine
that says when the vehicle was not driving: *ignition off*, *plugged in*
and *charging* begin not driving and can end a trip; *ignition on* and
*unplugged* end it and can start one, and so do *unlocked* and *engine
running* — which never end a trip, since a car locks itself on driving off
and an engine stops while a hybrid drives on (ADR-0021). The end of
charging is none — it comes when the battery is full, not when the driver
leaves. A marker only moves the boundary of a trip the movement roles
found, by at most T_still, never creates one (FAH-02, ADR-0012).

**Waypoint.** A position recorded during a trip, plus the one the vehicle
stood at before it moved. A fix's `gps_accuracy` of 0 means unknown, not
exact — the reference vehicle reports 0 for every fix (ISSUE-0004).

**Charging session.** From the charging state turning to `charging` until
it turns away, with SoC at start and end, position, charge point and the
energies below. A vehicle without a charging state has a session where
its SoC rises at standstill by more than the **charging threshold**.

**Refuelling.** A rise of the fuel quantity, at unchanged odometer, by at
least the refuelling threshold within one sampling interval; where no
odometer is assigned, while no other movement role moved. Rises that
follow one another within **T_settle** at unchanged odometer are one
refuelling. Its level after is read only once T_settle has elapsed after
the last rise — the value in effect then, or the first after the sensor
came back if it had dropped out. A rise across movement counts too when
the vehicle stopped and started again in between, or its fuel flap opened
(ADR-0022): a vehicle reporting its level once per driving cycle shows a
refuelling only at its next stop. A fuel level in % is litres of the tank
capacity; without one, no refuelling is detected.

**Candidate.** An event the derivation detected that still waits for a
receipt: every refuelling, and every charging session whose charge point is
foreign. A candidate without a receipt stays visible as *unconfirmed* and is
never silently dropped. A charging session at a configured charge point is
not a candidate — the charge point confirms it.

## Receipts

**Receipt.** A manually entered record of a refuelling (litres, price, full
tank or not) or of a charge (billed kWh, price). Receipts are stored apart
from L0, append-only, each under a UUID of its own; a correction or
cancellation is a new receipt pointing at the old one.

**Anchor time.** The time a receipt names as the moment of its event. A
receipt refers to its event by anchor time only, never by a derived id,
because derived ids do not survive a recomputation.

**Matching.** Pairing receipts with detected events, redone on every
derivation: a receipt goes to the nearest event of its kind within the
**matching tolerance** — every candidate, and a charging session at a
configured charge point too, which waits for no receipt but accepts one.
The tolerance covers the imprecision of a freely typed anchor time, not
lateness of entry; a receipt entered from a candidate carries the
candidate's time and matches without tolerance. An ambiguous match is
flagged, never guessed; a receipt that meets nothing is an event of its
own. The rules in full: [receipts-format.md](receipts-format.md#matching).

**Full tank.** A refuelling receipt that says the tank was filled. Not a
precondition for consumption — only the case in which the sensor term of
the tank-to-tank formula vanishes.

## Charging

**Charge point.** A fixed charging place configured across vehicles: name,
position, radius, optionally an energy meter entity, and a tariff. A
session whose position lies within no charge point's radius is at a
**foreign** charge point.

**Tariff.** A price per kWh valid from a date; versioned in time, and 0 is a
valid price. Cost always uses the tariff valid at the event's time, also on
recomputation.

**Battery-side energy.** ΔSoC × net battery capacity: what went into the
battery, estimated.

**Grid-side energy.** What was drawn from the grid: from a receipt, else the
charge point's meter difference, else battery-side energy × the **charging
loss factor**, else nothing.

**Charging loss.** Grid-side minus battery-side energy; reported only when
the net battery capacity is a parameter, and never used to calibrate that
capacity, which would be circular.

## Metrics

**Quality flag.** On every derived value, one of `measured` (from a sensor),
`receipt` (from a receipt), `estimated` (computed under an assumption) or
`incomplete` (spans a capture gap). A value computed from several inherits
the weakest.

**Tank-to-tank consumption.** Fuel consumption over an interval between two
refuelling receipts: (level after A − level after B + litres receipted
after A up to and including B) / odometer difference. `receipt` when both
ends are full tanks, `estimated` otherwise, with an error bounded by twice
the fuel level sensor's **resolution**.

**Consumption error threshold.** The relative sensor error below which a
tank-to-tank interval is reported as *the* consumption; intervals are
extended over consecutive receipts until one qualifies.

**Period.** A calendar month, a calendar year — local to the vehicle's
time zone — or the **rolling period** (default 30 days, ending at the
stream's last line) the metrics are reported for; the **lifetime** is the
whole of capture. Purchase figures (litres
refuelled, fuel cost) count what was bought in the period; rates (€/100 km,
kWh/100 km) use what was consumed, corrected for the change in fuel level
and SoC stock between the period's start and end.

**Electric energy share.** Electricity's share of the energy put in, by the
fuel's lower heating value; no assumptions, and explicitly not a distance
share.

**Electric distance share.** An estimate of the share of distance driven
electrically, from the same energies weighted by assumed efficiencies
(battery to wheel, fuel to wheel). Always `estimated`.

**Charge cycles.** Full cycle equivalents: Σ ΔSoC over charging sessions /
100 %. SoC gained outside sessions (regeneration, the engine charging) does
not count.

**Tank-fill equivalents.** Σ litres refuelled / tank capacity.

Both counters run cumulatively from the start of capture plus a configured
starting value, and per period; across capture gaps they are lower bounds,
reported together with the number of gaps.

## Parameters

**Vehicle parameters.** Tank capacity, net battery capacity, fuel type, the
two counters' starting values, the fuel level sensor's resolution, the
charging loss factor, the efficiencies, and how the vehicle reports
movement — sampled while driving, or once per driving cycle (ADR-0023). Each is mandatory only when an
enabled derivation needs it.

**Thresholds and time constants.** T_still, the refuelling threshold,
T_settle, the charging threshold (SoC fallback), the matching tolerance,
the plausibility threshold receipt/sensor, the heartbeat interval, the
outage threshold, the rolling period, the consumption error threshold, the
heating values, the thermal expansion coefficients and the temperature
low-pass time constant, and the exit window around an arrival. All
configurable per vehicle; each requirement
that uses one states its default.
