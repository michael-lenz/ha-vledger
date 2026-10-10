# Working in this repository

ha-vledger is a vehicle ledger for Home Assistant: the `vledger` library
and CLI under `src/`, the integration under `custom_components/`, one
version number. This file is working knowledge only. What the code must
do is in the register, below. What is public is in `docs/`: the formats,
the glossary, the derivations and the user guide.

## The register

Decisions, requirements, findings and the queue are records in the
private PM project `ha-vledger-pm`, kept with PMTK. Nothing is mirrored
here. Register commands run from inside that clone:

```bash
git clone git@github.com:michael-lenz/pmtk.git && pip install -e ./pmtk
git clone git@github.com:michael-lenz/ha-vledger-pm.git
cd ha-vledger-pm && pmtk orient
```

`pmtk orient` shows the queue worst first. The rules the code keeps are
requirements: `pmtk-req list --type maintainability`, and likewise
`integrity`, `process` and `documentation`. Read the relevant ADRs and
requirements before changing behaviour. PMTK's own `CLAUDE.md` states the
toolkit's rules in full. These are the ones that bind here:

- **Identity, per session, in both clones.** The handle names the model
  (`claude-fable-5-1`). A session that is not told its model, or may not
  write it, uses plain `claude`.

  ```bash
  cd ha-vledger-pm && pmtk identity set --handle <handle>
  cd ha-vledger    && git config user.name <handle> \
                   && git config user.email <handle>@pmtk.local
  ```

  GitHub shows these commits as unverified. That is expected.
- **Claim first.** `pmtk-task claim <id> --branch <branch>` (or
  `pmtk-issue`), then `pmtk sync` at once. `claim`, `release` and `done`
  only write the record; `sync` commits and pushes it, and `add` pushes on
  its own. When you finish: `pmtk-task done <id> -m '<outcome>'`, then
  `pmtk sync`. If you stop early, `release` with a note.
- **Decisions come before development.** Claude proposes (`pmtk-adr add`,
  on mainline) and never accepts. Work that rests on a decision waits for
  the owner's acceptance. An accepted decision is superseded or amended,
  never edited.
- **Found something? Register it**, even when fixing it now:
  `pmtk-issue add "..." --severity high --affects <path> -d '...'`.
- **Open with a synopsis.** Asked to act on an item, start the reply with
  its id, one line of what it asks, and the first step. Two or three
  lines at most.
- **Close with the signal.** "done here, nothing left to do", or "stopped
  here, waiting on ADR-9999".

## Branches

Claude currently works on `main` in both repositories. A session told
otherwise names its branch `TASK-9999_short-slug` after the driving
record. Ids in the 9990–9999 band are illustrative and never handed out.

## Getting set up

`docs/developing.md` is the manual: a venv, `pip install -e ".[dev,ha]"`,
`python -m pytest` (never a bare `pytest`), `ruff check src tests
custom_components`. Both have to be green.

## Releasing

The owner cuts every release with `scripts/release.sh`. Claude does not:
the session may not push tags. A change to how releases or the version
work keeps the script and `docs/releasing.md` in step (REQ-0128).

## Writing

Everything committed is English (ADR-0001). Design discussion may be in
German. Documentation comes in three genres: design document, operating
manual, and `docs/user-guide.md`. A change maintains every genre it
touches. Each document opens by declaring its genre, and README's table
indexes all of `docs/`. Mark what is not built yet with a status line
under the H1 or with `(planned)`. Delete what a change makes obsolete, in
the same change.
