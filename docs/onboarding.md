# Onboarding

*Operating manual — what a person or an agent joining ha-vledger needs to
know of this project, as autofac works it. It presumes pmtk's rules, which
`pmtk orient` states, and autofac's own, and restates neither: claiming,
branches, notes, done, identity, proposing and filing are theirs. Why this
file exists is ADR-0062 in the project's register.*

## What ha-vledger is

A vehicle ledger for Home Assistant: the `vledger` library and CLI under
`src/`, the integration under `custom_components/`, one version number for
both. What the code must do is in the register, below. What is public is
in `docs/`: the formats, the glossary, the derivations and the user guide.

## The register

Decisions, requirements, findings and the queue are records in the
private PM project `ha-vledger-pm`, kept with pmtk. Nothing of it is
mirrored in this repository.

```bash
git clone git@github.com:michael-lenz/ha-vledger-pm.git
cd ha-vledger-pm && pmtk orient
```

The rules the code keeps are requirements of four types:
`pmtk-req list --type maintainability`, and likewise `integrity`,
`process` and `documentation`. Read the relevant decisions and
requirements before changing behaviour.

A record of another register is always cited qualified, never by its bare
id: `pmtk.<id>` for a record of pmtk's own register, as in `pmtk.ADR-0108`.
A bare id is read as a record of this register.

## The checks

[developing.md](developing.md) is the manual: a venv,
`pip install -e ".[dev,ha]"`, and then

```bash
.venv/bin/python -m pytest -q                       # never a bare pytest
.venv/bin/ruff check src tests custom_components
```

Both have to be green. The package requires Python 3.13 or newer.

## Acceptance tests

A role text written for autofac itself, such as the examiner's or the
reviewer's, names autofac's command as the product a test meets. Here the
product is met in two ways, as [developing.md](developing.md) says under
`acceptance/`:

- **The CLI**: the `vledger` command run as a program,
  `[sys.executable, "-m", "vledger.cli", …]` in a process of its own
  (`acceptance/test_req_0001_two_layers.py`).
- **The integration**: a Home Assistant that loads it from
  `custom_components/`, driven as the browser drives it — the config and
  options flows through `/api/config/config_entries/…`, the states it
  shows (`acceptance/test_req_0010_ui_configuration.py`).

Either way a test imports nothing of `vledger` or of the integration.

The user documentation a test is derived from is
[user-guide.md](user-guide.md). Where it hands a format over to a format
document — [l0-format.md](l0-format.md), [l1-format.md](l1-format.md),
[receipts-format.md](receipts-format.md) — that document is what a user
reads for the format, and a test may take key names and values from it;
its reasoning, and every other design document, stays out of what a test
rests on.

A requirement's own tests are run by their file, not by the directory:

```bash
.venv/bin/python -m pytest -q acceptance/test_req_0024_*.py
```

## The documentation

The documentation is `README.md` and every file under `docs/`. It comes
in three genres: design document, operating manual, and
[user-guide.md](user-guide.md). Each document opens by declaring its
genre, and README's table indexes all of `docs/`. What is not built yet
is marked with a status line under the H1 or with `(planned)`. A change
maintains every genre it touches and deletes, in the same change, what it
makes obsolete.

Everything committed is English (ADR-0069). Design discussion may be in
German.

## Releasing

The owner cuts every release with `scripts/release.sh`. A change to how
releases or the version work keeps the script and
[releasing.md](releasing.md) in step (REQ-0128).

## Illustrative ids

Ids in the band 9990–9999, such as `TASK-9999`, are illustrative and
never handed out.

## What an agent here cannot do

- Push a tag or cut a release: both are the owner's.
- Run the checks where its sandbox has no Python 3.13. Its branch note
  then says that the checks did not run, and a person runs them before
  the merge.
