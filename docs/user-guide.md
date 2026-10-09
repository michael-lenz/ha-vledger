# User guide

*What a participant types. The design is in the other documents under
`docs/`; this page is the tour of the `vledger` command, verb by verb.*

**Status:** the `l0` verbs exist. Receipts, derivations, exports and reports
are planned; the Home Assistant integration captures nothing yet.

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
123457`, never a number the shell made of it.

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

## Planned

`vledger receipt …`, `vledger derive …`, `vledger export …` and `vledger
report …` follow the same shape: a noun, a verb, `--base` and the subject.
