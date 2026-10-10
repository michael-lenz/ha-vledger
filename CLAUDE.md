# Working in this repository

ha-vledger is a vehicle ledger for Home Assistant: one repository, two
packages — the `vledger` library and CLI under `src/`, the integration under
`custom_components/` — one version number. Data is JSON Lines; the raw log
is never rewritten.

**The reasoning behind every design choice is in the decision records,
which live in the PM project — not here.** Read the relevant ADRs before
changing behaviour; this file only covers how to work here. What cannot be
read without the register, because it is private, is specified in `docs/`:
the L0 and L1 formats, the glossary, the user guide.

## The register lives in PMTK

Decisions, requirements, findings and the queue are records in a separate,
private PM project, `ha-vledger-pm`, kept with the PMTK toolkit. Nothing
is mirrored back here. Clone both, and every register command runs from
inside that clone:

```bash
git clone git@github.com:michael-lenz/pmtk.git && pip install -e ./pmtk
git clone git@github.com:michael-lenz/ha-vledger-pm.git
cd ha-vledger-pm && pmtk orient
```

`pmtk orient` is the cold start: the queue worst first, what is settled,
what to check. The full queues are `pmtk-task list --open`, `pmtk-issue
list --open`, `pmtk-adr list`, `pmtk-req list`. PMTK's own `CLAUDE.md`
(in the `pmtk` repository) explains the toolkit's rules in full; the ones
that bind here:

- **Identity.** A Claude session acts under a handle that names its model,
  `claude-fable-5-1` for Fable 5.1; a session that is not told its model,
  or may not write the identifier, acts as plain `claude`. Set it in both
  clones, per session:

  ```bash
  cd ha-vledger-pm && pmtk identity set --handle claude-fable-5-1
  cd ha-vledger    && git config user.name claude-fable-5-1 \
                   && git config user.email claude-fable-5-1@pmtk.local
  ```

  GitHub shows these commits as unverified; that is the price of the
  pseudonymous identity, not a problem to fix with a different author.
- **Claim first.** The first action of any work is `pmtk-task claim
  TASK-9999 --branch <branch>` in the register clone, pushed to its
  mainline at once (`pmtk-issue` alike), so a parallel session sees it. A
  record already claimed names where that work lives: look before
  duplicating. `release` it with a note if you stop without finishing.
  `pmtk-task done <id> -m '<outcome>'` then `pmtk sync` when finished.
  `add` pushes on its own; `claim`, `release` and `done` only write the
  record, and `pmtk sync` commits and pushes it — a claim left unsynced is
  invisible to the sessions it is for.
- **Development waits for decisions.** Work that needs a decision, or
  rests on one, starts only after `pmtk-adr accept` by the owner. Claude
  proposes (`pmtk-adr add`, on the register's mainline) and never accepts.
  An accepted decision is not edited: supersede it (`--supersedes`), or
  the acceptor amends it under a recorded cause.
- **Found something? Register it.** `pmtk-issue add "..." --severity
  high --affects <path> -d '...'`, even when fixing it now. An issue where
  both sides have a case is deferred (`needs-decision`), not closed by
  editing.
- **Say what you are taking on, first.** Asked to take on, implement,
  fix or otherwise act on a particular item — a task, an issue, a
  requirement, a finding — open the reply with a very short synopsis of
  it: the id, one line of what it asks, and what the first step will be
  (the claim, a proposal, the fix). Two or three lines at most, before
  any work; it is how the owner sees that the right record was picked up.
- **Say when you are done — and only then.** A finished piece of work ends
  with "done here, nothing left to do"; one that stops at a proposal or a
  blocker ends with "stopped here, waiting on ADR-9999" — the id of what
  it waits on.

## Branches

The owner currently has Claude working on `main` in both repositories; a
session told otherwise names its branch after the driving record,
`TASK-9999_short-slug`, and the claim carries the branch either way. Ids
like `TASK-9999` are illustrative (the 9990–9999 band is never handed out).

## Getting set up

`docs/developing.md` is the manual: a venv, `pip install -e ".[dev,ha]"`,
`python -m pytest` (never a bare `pytest`), `ruff check src tests
custom_components`. The library's tests run without Home Assistant; the
integration's need the `ha` extra, which does not install cleanly into a
Debian system Python — use the venv.

## Rules the code keeps

- **Every operation on the data is a verb of `vledger`** (ADR-0005). The
  integration calls only functions a verb exposes; a derivation is not
  finished until its verb exists and its scenario runs from the shell. New
  derivations register themselves in `l1.DERIVATIONS`, as `trips` does,
  and join the import in `vledger/derivations.py`, which is how the live
  writer and `derive all` come to run the same set.
- **L0 is never rewritten.** The format is `docs/l0-format.md`; adding a
  key or an attribute to the whitelist is a schema version, and every
  earlier version stays readable. Values stay the strings Home Assistant
  reported; parsing is the derivation's job.
- **The library knows nothing of Home Assistant and depends on nothing
  outside the standard library.** The first tempting third-party import
  is a decision to supersede ADR-0002, not a convenience.
- **Every derived value carries a quality flag** — `measured`, `receipt`,
  `estimated`, `incomplete` — and nothing is read across a capture gap.
- **Determinism is tested.** The same stream yields the same L1 whether
  derived at once or line by line; completion is judged by the stream's
  last line, never by the clock.
- **The version is one number in three places**, and `tests/test_version.py`
  fails when they disagree. A release is a tag (`docs/releasing.md`).
- **Releasing is `scripts/release.sh`, and the owner cuts every release
  with it** (REQ-0128). So a change to how a release is made or to where
  and how the version is carried — a further place holding the version,
  the tag's spelling, the release workflow's trigger or checks, a step
  `docs/releasing.md` adds — updates the script and the manual in the
  same change, and is tested with `scripts/release.sh X.Y.Z --dry-run`.
  Claude does not cut releases: the session may not push tags.
- **Tests build their streams through the verbs**, never by writing lines
  by hand, so a fixture can hold nothing the library could not have
  written. Real streams become fixtures with positions shifted by a fixed
  offset.

## Writing

Code, comments, documentation, CLI output and register records are
English (ADR-0001); design discussion may be German. Documentation comes
in three genres and a change maintains every genre it touches: a design
document says why, an operating manual says what its operator does,
`docs/user-guide.md` says what a participant types — each opens by
declaring which it is, and README's table indexes all of `docs/`. Mark
what is not built yet: a status line under the H1, `(planned)` on a
section. Delete what a change makes obsolete, in the same change.
