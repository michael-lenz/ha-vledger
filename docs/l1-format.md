# L1 on disk

*Design document — the derivation's files, as decided in ADR-0009 of the
project's register. This page is what a reader of their own `l1/` needs;
the reasoning is in the decision.*

**Status:** `trips.jsonl`, `refuellings.jsonl`, `charging-sessions.jsonl`,
the manifest, the cursor, rebuilds and the `l1` verbs exist. Periods, and
the live derivation in Home Assistant that appends as events complete, are
planned.

## Files

```
<base>/vehicle-<subject>/l1/
  manifest.json              what this L1 was derived from, and by what
  trips.jsonl                one line per trip, in order of start
  charging-sessions.jsonl    one line per charging session
  refuellings.jsonl          one line per refuelling candidate
  periods.jsonl              one line per period with its metrics  (planned)
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
event written — the cursor; `l0_through` the `t` of the last L0 line read.

## Who writes what

- Event files are **append-only between rebuilds**: a rebuild writes them
  whole, the incremental derivation appends an event the moment it is
  complete, flushed and synced like an L0 line.
- `periods.jsonl` is **rewritten whole**: it is the current answer, not a
  log.
- A **rebuild replaces the directory atomically**: everything is derived
  into `l1.tmp/`, which is then renamed into place, so a reader never sees
  a half-built L1 and a crash mid-way leaves the old one.
- The manifest is rewritten after every rebuild and every incremental run.

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

## Determinism

A stream replayed line by line with the incremental derivation after
every line yields byte-identical event files to one rebuild of the whole
stream. That is a test in the repository, and it runs on every real stream
that becomes a fixture.
