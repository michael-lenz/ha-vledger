# The L0 record format

*Design document — the layout of the raw log, version 5, as decided in
ADR-0004, ADR-0011, ADR-0021, ADR-0022, ADR-0025 and ADR-0036 of the project's register. This page is the
specification a reader of their own files needs; the reasoning is in the
decisions.*

**Status:** writing, reading, validation and gap detection exist, as
`vledger l0 …` ([user guide](user-guide.md)), and the integration captures
into it.

## One envelope for every line

L0 is JSON Lines, UTF-8, one JSON object per line. Every line has four
fixed keys, then the keys of its kind:

```json
{"v": 5, "t": "2026-10-09T07:12:03.412Z", "kind": "state", "subject": "a7c1…", …}
```

| key | meaning |
|---|---|
| `v` | schema version, integer; this page defines 5, and what earlier ones lack ([Versioning](#versioning)) |
| `t` | the Home Assistant time of the event: UTC, ISO 8601, milliseconds, `Z` |
| `kind` | `state`, `start`, `stop`, `heartbeat` or `config` |
| `subject` | the vehicle or charge point this line belongs to |

Unknown keys are ignored by every reader. A line of an unknown kind is
skipped by readers and reported by validation, never fatal. Values that
Home Assistant holds as strings stay strings — `"state": "123456.0"` — and
parsing is the derivation's job: L0 holds what was reported and interprets
nothing.

## The five kinds

### `state`

One line per change of the state, or of a role-relevant attribute, of an
assigned entity:

```json
{"v":5,"t":"2026-10-09T07:12:03.412Z","kind":"state","subject":"a7c1…",
 "role":"odometer","entity":"sensor.volvo_odometer","state":"123456","unit":"km",
 "reported_before":"2026-10-09T06:57:03.120Z"}
{"v":5,"t":"2026-10-09T07:12:03.418Z","kind":"state","subject":"a7c1…",
 "role":"position","entity":"device_tracker.volvo","state":"not_home",
 "attrs":{"latitude":48.1371,"longitude":11.5754,"gps_accuracy":12,"source_type":"gps"}}
{"v":5,"t":"2026-10-09T07:14:00.001Z","kind":"state","subject":"a7c1…",
 "role":"charging_state","entity":"sensor.volvo_charging","state":"unavailable"}
```

| key | meaning |
|---|---|
| `role` | the configured role ([glossary](glossary.md)); the sensor-pair variant of position logs under `position_latitude` and `position_longitude` |
| `entity` | the entity id at the time of writing; a rename is a configuration change and the stream continues |
| `state` | the state string exactly as Home Assistant holds it, `unavailable` and `unknown` included |
| `unit` | `unit_of_measurement` at the time of writing, when present — the source unit the derivation converts from |
| `attrs` | only the role-relevant attributes, by a fixed list per role (below); absent when empty |
| `measured_at` | the source's own measurement time, only when the role's configuration names an attribute that carries one |
| `reported_before` | when Home Assistant last heard the value this line replaces — its `last_reported` (since version 2) |

An update that repeats a value writes no line; it only moves that value's
`last_reported`. So `t − reported_before` is one **sampling interval** of
the role — the time between two updates — where the time between two lines
is only the time between two changes ([glossary](glossary.md)).
`reported_before` is absent only when there was no old state. A line
after an `unavailable` or `unknown` one still carries it, but the time since
an outage began is no sampling interval, and `vledger l0 stats` does not
count it (ADR-0011).

Role-relevant attributes, unchanged since version 1: `position` →
`latitude`, `longitude`, `gps_accuracy`, `source_type`; every other role
→ none. What is not on this list is not captured and cannot be captured
retroactively; adding to the list is a new schema version.

### `start`

Written when capture for a subject begins, with a snapshot of every
assigned role as it stands:

```json
{"v":5,"t":"2026-10-09T06:00:00.000Z","kind":"start","subject":"a7c1…",
 "vledger":"0.6.0","homeassistant":"2026.10.1",
 "snapshot":[{"role":"odometer","entity":"sensor.volvo_odometer","state":"123456",
              "unit":"km","since":"2026-10-08T22:41:10.000Z"}]}
```

`snapshot` holds one entry per assigned role, with these keys:

| key | meaning |
|---|---|
| `role` | the configured role, as on a state line |
| `entity` | the entity assigned to the role, as on a state line |
| `state` | the state string exactly as Home Assistant holds it, as on a state line |
| `since` | when the value last changed (below) |
| `unit` | `unit_of_measurement`, when present, as on a state line |
| `attrs` | the role-relevant attributes, by the same fixed list as a state line; absent when empty |

`role`, `entity`, `state` and `since` are always present. An entry carries
no `measured_at` and no `reported_before`. An assigned entity for which
Home Assistant has no state at the start gives `state` `unavailable` and
the start line's own `t` as its `since`, with no `unit` and no `attrs`.

`since` is when the snapshotted value last changed — the entity's
`last_changed`, or its `last_updated` for a role whose attributes are part
of its value, those the whitelist above names: `position` (ADR-0036; before
version 5 it was `last_updated` for every role, which an attribute update
moves too). The snapshot is the only L0 content that is not a change; the
derivation reads each entry's value at its `since`, but no earlier than
the stream's last line before the start — a value that
changed after capture stopped listening is placed where the stream can
still vouch for it, never inside an event the previous run completed
(ADR-0028) — and drops an entry that repeats the value its role already
holds. An entry whose `since` is not later than that last line held
through the gap: Home Assistant kept the value while capture was not
listening, and the derivation reads it at the start line's time as well,
a reading on the gap's near side (ADR-0035).

### `stop`

At orderly end: `{"kind":"stop","reason":"shutdown"}`, `reason` one of
`shutdown`, `unload`, `reload`. A start without a stop before it is a
crash.

### `heartbeat`

At a fixed interval (default 60 minutes): `{"kind":"heartbeat","lines":4213}`,
`lines` counting state lines since start — a cheap self-check for
validation.

### `config`

The complete configuration of the subject, written at start (after the
start line) and on every change:

```json
{"v":5,"t":"2026-10-09T06:00:00.050Z","kind":"config","subject":"a7c1…",
 "config":{"name":"Volvo",
   "roles":{"odometer":{"entity":"sensor.volvo_odometer"},
            "charging_state":{"entity":"sensor.volvo_charging","map":{"charging":["Charging"]}}},
   "parameters":{"tank_capacity_l":71,"battery_net_kwh":14.7,"fuel":"petrol"},
   "thresholds":{"t_still_s":1800,"refuel_threshold_l":3,"heartbeat_s":3600},
   "time_zone":"Europe/Berlin"}}
```

An enumerated role's `map` lists the source values that mean its one
positive domain state and nothing else: every other value the source
reports means the negative state, and `unavailable` and `unknown` hold
the last known one (ADR-0008). A vehicle's `time_zone` is the IANA zone
its calendar months and years begin in; the integration writes Home
Assistant's own at every start. A config line without one — every line
written before it existed — reads as UTC.

Every key a vehicle's `config` takes is listed in the user guide under
[The raw log → Writing](user-guide.md#writing): the roles, the key of
each enumerated role's positive state in its `map`, the parameters —
among them `movement_reporting`, `sampled` or `per_cycle` (ADR-0024) —
and the thresholds, among them the exit window `exit_window_s`. A
parameter or threshold the line leaves out reads as its default, and
one whose default depends on the fuel reads as the default of the
line's `fuel`, as a petrol vehicle's without one.

A charge point is its own subject with its own stream, and tariffs are
never in a vehicle's stream. Its `config` line:

```json
{"v":5,"t":"2026-10-09T06:00:00.050Z","kind":"config","subject":"c3d4…",
 "config":{"name":"Home","latitude":48.1371,"longitude":11.5754,"radius_m":100.0,
   "meter":{"entity":"sensor.wallbox_energy"},"power_kw":null,
   "tariffs":[{"from":"2026-01-01","eur_per_kwh":0.3},{"from":"2026-07-01","eur_per_kwh":0.32}]}}
```

| key | meaning |
|---|---|
| `name` | the charge point's name |
| `latitude`, `longitude` | its position, in decimal degrees |
| `radius_m` | in metres: a charging session whose position lies within it is at this charge point — at the nearest, where several hold it |
| `meter` | `{"entity": …}`, the energy meter entity, whose readings the stream logs under the role `energy_meter`; `null` without one |
| `power_kw` | the constant charging power where the owner declared one (ADR-0038), otherwise `null`; a line without it reads as none |
| `tariffs` | the tariff history: entries `{"from": …, "eur_per_kwh": …}`, `from` the date the price is valid from (`YYYY-MM-DD`), `eur_per_kwh` the price, 0 included |

A session takes the entry with the latest `from` not after the date, in
UTC, of its start; of entries with the same `from`, the one listed last.
So a tariff is never edited: a new price is an entry appended, and a
correction an entry appended under the same date. Before the first `from`
a session has no tariff. A config line is a charge point's because it
carries `tariffs`.

`vledger l0 config` writes the JSON it is given unchecked. `vledger l0
validate` checks that `config` is an object and, for a charge point's
line, that the derivation can read it: `latitude`, `longitude` and a
positive `radius_m` numbers, `power_kw` `null` or positive, `meter` `null`
or naming an entity, and each tariff entry an object with a `from` date
and an `eur_per_kwh` number not below 0; each part that is not is an
error. `vledger derive charging` refuses to run while a charge point's
latest configuration has such an error, rather than failing part way.
The derivation reads configuration
from L0, never from Home Assistant — which is what lets the CLI run without
one.

## Files

```
<base>/
  vehicle-<subject>/
    l0/2026-10.jsonl      one file per UTC month, by the line's own t
    receipts.jsonl        append-only, apart from L0
    l1/…                  the derivation, regenerable
  chargepoint-<subject>/
    l0/2026-10.jsonl
    l1/…
```

`<base>` defaults to `<config>/vledger/` under Home Assistant. A line goes
to the file its own `t` names, so a stream rotates without a gap at the
month boundary. Writes append, flushed and synced per line, off the event
loop; a line is on disk whole or not at all, and a torn last line after a
crash is skipped on reading. The next write after the crash finds the file
without its last line break and writes one before its own line, changing
nothing already on disk: the torn line stays a line of its own, which
readers skip wherever it stands, and every line after it reads. A line
that is not JSON is such a torn line, a warning of `vledger l0 validate`;
a line that is JSON but no object is an error, since neither the writer
nor a crash leaves one.

## Subjects

A subject is a UUID the integration generates when a vehicle or charge
point is created, kept in the config entry and never offered for editing.
It is not the entry id and not any entity id, so the stream survives a
change of source integration and a rename of every entity. Deleting the
config entry ends the stream; a new entry is a new subject.

## Gaps

Capture gaps are recognisable from the markers alone:

| reason | what the markers show |
|---|---|
| `crash` | a start with no stop before it — nothing from the last line to that start |
| `stopped` | an orderly stop and the next start — capture was off |
| `silence` | longer than the heartbeat interval plus tolerance between two lines while running |
| `open` | the stream ends without a stop, and now is further away than that |

The heartbeat interval is read from the latest `config` line
(`thresholds.heartbeat_s`), else 3600 s.

## Versioning

`v` is per line, so a stream may hold several versions after an upgrade. A
reader of version N reads every version ≤ N and refuses a newer one; a new
version may add keys and kinds; anything that changes the meaning of an
existing key or kind is a new version. `vledger l0 validate` names the
versions a stream holds.

| version | adds |
|---|---|
| 1 | the format as first decided (ADR-0004) |
| 2 | `reported_before` on `state` lines (ADR-0011); a version 1 stream has no sampling interval to measure |
| 3 | the roles `engine`, `lock`, `in_use` (ADR-0021) and `fuel_flap` (ADR-0022) |
| 4 | the role `trip_consumption`, the trip computer's average since its reset (ADR-0025) |
| 5 | a snapshot entry's `since` is when its value last changed, `last_changed` for every role but `position` (ADR-0036); in earlier versions it is `last_updated`, which is never earlier, so a reader treats both alike |
