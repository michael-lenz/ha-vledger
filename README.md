# ha-vledger

> **Beta.** Everything the design describes is built — capture, trips,
> refuellings, charging sessions, receipts, metrics, entities, statistics,
> exports — and runs on the author's vehicle, against whose drives every
> derivation has been checked; against nobody else's yet. The options may
> still change between releases, a release may rebuild the derivation from
> the raw log, and there is no support. The log format is decided and will
> be read by every later version, so what is captured now is kept. Install
> it through HACS as a custom repository if you want to try it; this
> notice goes when the register's checklist for leaving it is met
> (TASK-0032).

A vehicle ledger for Home Assistant. It keeps a raw log of the handful of
vehicle states that matter to a ledger — odometer, position, fuel level,
state of charge, charging state — and derives from it what the vehicle's
own app never tells you reliably: trips, charging sessions, refuellings,
consumption and cost. Passively, from whatever integration already exposes
the vehicle, without a line of manufacturer-specific code, and without
touching anything else the vehicle reports: it is a ledger, not a monitor.

**Status:** capture works. The integration sets up a vehicle or a charge
point through the UI, writes its raw log and shows it in numbers as
diagnostic entities and diagnostics — the format, and the `vledger l0`
verbs that write, read, validate and count it and list its gaps, are the
library's. Three derivations exist: `vledger derive trips` finds the
trips in a stream, `vledger derive refuellings` the refuelling candidates
and `vledger derive charging` the charging sessions, with the charge
point's meter and tariff; L1 — the derivation on disk, with its manifest
and cursor — is written and read by the `l1` verbs, and the integration
keeps it live, with a `vledger.recompute` action to rebuild it; receipts
are entered and matched by the `receipt` verbs, and in Home Assistant by
three actions and a dashboard form, and every new event fires a Home
Assistant event and, where a receipt is due, a notification; the metrics come per month, year,
rolling period and lifetime in L1, and for any span from `vledger report
metrics`; L1 is exported as CSV, JSON and GPX, from the shell and by a
Home Assistant action into the media folder; every trip carries its own
consumption, from the car's trip computer where it reports one, and the
last trip's is an entity. In Home Assistant the current month's, year's
and rolling period's metrics are entities, and every corrected month is
kept as a statistic. Every behaviour described below is the design,
held as requirements in the project's register (`ha-vledger-pm`); what
does not exist yet is marked *(planned)*.

## What it does

- **Captures, losslessly.** Every change of a source entity you assign to
  a role becomes one record in a raw log (L0): JSON Lines, append-only, one
  stream per vehicle and per charge point, rotated monthly, kept outside
  the Home Assistant recorder and its retention. Start, stop and heartbeat
  markers make any capture gap visible; nothing is interpolated across one.
- **Derives, deterministically.** Trips (between standstills), charging
  sessions (from the charging state), refuellings (a fuel rise at unchanged
  odometer) and metrics are computed from L0, receipts and configuration
  (L1) — the same logic live in Home Assistant and in batch from the
  command line, recomputable from scratch at any time. Every derived value
  carries a quality flag: `measured`, `receipt`, `estimated` or
  `incomplete`.
- **Takes receipts.** Price and exact quantity come from you: a refuelling
  or charging receipt, entered from a dashboard or an action — a
  notification of the new candidate opens the form — matched to the
  detected event by time. Receipt values beat sensor
  values; a detected event without a receipt stays visible as unconfirmed.
- **Knows your charge points.** Home, work, anywhere fixed: position,
  radius, tariff, optionally a meter. A charge point with a tariff but no
  meter still yields cost, estimated through a charging loss factor; a
  tariff of 0 is cost 0. Anything else is a foreign charge and asks for a
  receipt.
- **Reports.** Per month, year and rolling period: distance, litres and kWh,
  fuel and electricity cost, €/100 km per energy carrier, the electric
  share two ways (energy by heating value, and an estimated distance
  share), charge cycles and tank-fill equivalents. Fuel consumption is
  tank-to-tank between any two receipts, corrected by the fuel level sensor,
  so a tank that is never filled up still gets a figure. A single trip's
  consumption is its own figure beside that, from the trip computer or
  the sensor deltas, shown only when it exceeds its possible error.

## What it is built of

One repository, two packages, one version number (ADR-0002):

```
src/vledger/               the library and the vledger CLI: all derivation
                           logic, no Home Assistant, no dependencies;
                           published to PyPI as `vledger`
custom_components/vledger/ the Home Assistant integration, a shell over the
                           library: capture, config and options flows,
                           entities, actions, diagnostics; pins the library
                           by the same version in manifest.json
tests/                     pytest: library tests on L0 fixtures, integration
                           tests with pytest-homeassistant-custom-component
docs/                      design documentation
```

A vehicle is a config entry; its source entities are assigned to roles
(`odometer`, `position`, `fuel_level`, `soc`, `charging_state`, …), and
what the vehicle can do follows from the roles it has. Everything is
configured in the UI; no YAML.

## Documentation

| Document | What it is |
|---|---|
| [docs/user-guide.md](docs/user-guide.md) | What a participant types: the `vledger` command, verb by verb |
| [docs/l0-format.md](docs/l0-format.md) | The raw log's layout, version 4 — the specification a reader of their own files needs |
| [docs/l1-format.md](docs/l1-format.md) | The derivation on disk: files, manifest, cursor, rebuilds, and the exports rendered from it |
| [docs/derivations.md](docs/derivations.md) | How each detection works as built: algorithm, roles and parameters, quality flags, limitations |
| [docs/receipts-format.md](docs/receipts-format.md) | Receipts on disk, corrections and cancellations, and how they meet events |
| [docs/glossary.md](docs/glossary.md) | The one English spelling of every domain term, and what it means |
| [docs/developing.md](docs/developing.md) | From a fresh clone to green tests, and the conditions behind each step |
| [docs/releasing.md](docs/releasing.md) | What the person cutting a release does, in order |

Decisions, requirements and the work queue are records in the project's
register, `ha-vledger-pm`, not prose here.

## Installing

Not yet — see the notice at the top. For the author's own test
instances: in HACS, *Integrations → ⋮ → Custom repositories*, add
`https://github.com/michael-lenz/ha-vledger` as an *Integration*, then
install it and restart; or copy `custom_components/vledger` into the
configuration directory by hand. Either way Home Assistant installs the
library from PyPI (`vledger==<version>`, pinned in the manifest), so the
version has to be published first — [docs/releasing.md](docs/releasing.md).
Then *Settings → Devices & services → Add integration → Vehicle Ledger*:
the [user guide](docs/user-guide.md) walks the four steps. The library on
its own: `pip install vledger`, which brings the `vledger` command.

## Developing

[docs/developing.md](docs/developing.md) is the manual; the short form:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,ha]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests custom_components
```

The library's tests run in any Python with `.[dev]`; the integration's
need the `ha` extra and a venv. `CLAUDE.md` says how a Claude session
works here and with the register.

## Privacy

The position history is personal data. It stays on your instance — nothing
is transmitted anywhere — and is redacted from logs and diagnostics, but
the default storage path lies under the Home Assistant configuration
directory and is therefore part of your backups.

## License

BSD-3-Clause — see [LICENSE](LICENSE).
