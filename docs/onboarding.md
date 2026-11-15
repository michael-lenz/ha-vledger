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

## The checks

[developing.md](developing.md) is the manual: a venv,
`pip install -e ".[dev,ha]"`, and then

```bash
.venv/bin/python -m pytest -q                       # never a bare pytest
.venv/bin/ruff check src tests custom_components
```

Both have to be green. The package requires Python 3.13 or newer.

## The documentation

The documentation is `README.md` and every file under `docs/`. It comes
in three genres: design document, operating manual, and
[user-guide.md](user-guide.md). Each document opens by declaring its
genre, and README's table indexes all of `docs/`. What is not built yet
is marked with a status line under the H1 or with `(planned)`. A change
maintains every genre it touches and deletes, in the same change, what it
makes obsolete.

Everything committed is English (ADR-0001). Design discussion may be in
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
