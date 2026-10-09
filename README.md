# ha-vledger

A vehicle ledger for Home Assistant. It keeps a raw log of the handful of
vehicle states that matter to a ledger — odometer, position, fuel level,
state of charge, charging state — and derives from it what the vehicle's
own app never tells you reliably: trips, charging sessions, refuellings,
consumption and cost. Passively, from whatever integration already exposes
the vehicle, without a line of manufacturer-specific code, and without
touching anything else the vehicle reports: it is a ledger, not a monitor.

**Status:** structure only. Nothing captures or derives yet. Every
behaviour described below is the design, held as requirements in the
project's register (`ha-vledger-pm`); a section is marked *(planned)*
until it exists.

## What it does *(planned)*

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
  or charging receipt, entered from a notification, a dashboard or an
  action, matched to the detected event by time. Receipt values beat sensor
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
  so a tank that is never filled up still gets a figure.

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
| [docs/glossary.md](docs/glossary.md) | The one English spelling of every domain term, and what it means |

Decisions, requirements and the work queue are records in the project's
register, `ha-vledger-pm`, not prose here.

## Installing *(planned)*

Through HACS, as a custom repository, once the first release exists; or by
copying `custom_components/vledger` into your configuration directory. The
library installs on its own with `pip install vledger` and brings the
`vledger` command.

## Developing

```bash
pip install -e ".[dev]"        # library and CLI
pip install -e ".[dev,ha]"     # plus the Home Assistant test stack, for the integration
python -m pytest
ruff check src tests custom_components
```

The integration's manifest pins a library version that may not be on PyPI
yet; Home Assistant skips the install when the package already imports, so
a development instance needs the editable install above first.

## Privacy

The position history is personal data. It stays on your instance — nothing
is transmitted anywhere — and is redacted from logs and diagnostics, but
the default storage path lies under the Home Assistant configuration
directory and is therefore part of your backups.

## License

BSD-3-Clause — see [LICENSE](LICENSE).
