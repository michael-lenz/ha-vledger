# Developing

*Operating manual — what a developer does to get from a fresh clone to
green tests, in order, and the conditions behind each step. Why the
repository is shaped this way is ADR-0002 in the project's register.*

## Conditions

- **Python 3.13 or newer.** The library's floor (`requires-python` in
  `pyproject.toml`) and what Home Assistant has required since 2025.2.
- **A virtual environment**, as soon as the Home Assistant test stack is
  involved. The library alone runs anywhere; the `ha` extra pulls in a
  full Home Assistant core, and on a Debian or Ubuntu *system* Python one
  of its transitive dependencies (`PyRIC`) fails to build against the
  distribution's patched setuptools, and Debian-managed packages such as
  PyYAML refuse to be replaced. A venv has neither problem.
- **Nothing else.** No database, no services, no pandoc. The library has
  no runtime dependencies (CLI-06), and the tests write only to pytest's
  temporary directories.

## Two environments, on purpose

The repository holds two packages (ADR-0002), and they are developed in
two different loops:

| | the library and CLI | the integration |
|---|---|---|
| needs | Python, `.[dev]` | plus Home Assistant and its test plugin, `.[dev,ha]` |
| tests | `tests/test_*.py` | `tests/ha/` |
| runs in | any Python, the system one included | a venv |

Without Home Assistant installed, `python -m pytest` leaves `tests/ha`
out (`tests/conftest.py`) rather than failing on it.

## Setting up

```bash
git clone https://github.com/michael-lenz/ha-vledger.git
cd ha-vledger
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev,ha]"       # library, CLI, ruff, pytest, Home Assistant, the test plugin
```

Installing `.[dev,ha]` takes a few minutes: Home Assistant is large. For
work on the library alone, `.[dev]` is enough and installs in seconds.

If the `ha` extra fails to build `PyRIC` even inside the venv, the
environment's pip is not isolating builds (a `PIP_NO_BUILD_ISOLATION` in
the environment, or a pip configuration that sets it). Unset it, or
upgrade setuptools inside the venv first; the package itself is only a
transitive dependency of Home Assistant's Bluetooth support and is never
imported by anything here.

## Running the checks

```bash
.venv/bin/python -m pytest -q                     # everything: library and integration
.venv/bin/python -m pytest -q tests/ha            # the integration only
python -m pytest -q                               # the library only, if the system Python has .[dev]
.venv/bin/ruff check src tests custom_components  # the default rules, nothing more
```

**`python -m pytest`, never a bare `pytest`.** A `pytest` found on `PATH`
may belong to another interpreter — a `uv tool` install, a system
package — which never sees what was just installed into the venv, and the
failure then reads as a broken tree. `python -m pytest` runs the
interpreter that is actually active.

Both pytest and ruff have to be green before a push. The release workflow
runs them again and refuses a tag over a red tree
([releasing.md](releasing.md)).

## Trying the CLI by hand

The CLI is the library's test harness (ADR-0005): any stream can be built
and inspected from a shell, which is also how a bug is reproduced without
Home Assistant.

```bash
export VLEDGER_BASE=$(mktemp -d)
vledger l0 start --vehicle demo --homeassistant 2026.9.4 \
    --snapshot '[{"role":"odometer","entity":"sensor.o","state":"100","unit":"km","since":"2026-10-09T09:00:00Z"}]'
vledger l0 state --vehicle demo --role odometer --entity sensor.o --state 112 --unit km
vledger derive trips --vehicle demo
```

The [user guide](user-guide.md) walks every verb. `vledger` is on `PATH`
once the venv is activated, or as `.venv/bin/vledger`.

## Real streams as fixtures

A real stream catches what a scenario built by hand does not think of
(QUA-01). It enters `tests/fixtures/` anonymised, with the L1 it yields
beside it, and `tests/test_fixtures.py` runs every directory there: a
fresh rebuild has to yield that L1 (the `version` key aside, which a
release changes), every vehicle's stream replayed line by line has to
yield the same files as one rebuild, and every stream has to validate.

1. Copy the data directory from the Home Assistant configuration
   directory (`vledger/`), or the subjects of it that belong together: a
   vehicle, the charge points it charges at, the other vehicles that
   charge there.
2. Anonymise it into a new directory named for what it shows:
   `vledger l0 anonymise --base copy --shift LAT,LON --to tests/fixtures/first-week`
   ([user guide](user-guide.md#anonymising)). Pick an offset of your own
   and do not record it.
3. Derive and read it: `vledger derive all --write --base tests/fixtures/first-week --vehicle ID`,
   then check `l1/` against what happened — the drives, charges and
   refuellings you remember, their distances, energies and litres. The
   expected L1 is a statement about the vehicle, not a snapshot of the
   code: what is wrong in it is a finding, registered before the fixture
   is committed.
4. Delete the `manifest.json` files and commit L0, receipts and `l1/`.

When a change of the derivation changes a fixture's L1 on purpose,
step 3 rewrites it; the diff of `l1/` is then part of the change and is
read like code.

## Running the integration in a Home Assistant

A development instance needs the library importable before it loads the
integration, because the manifest pins a version that may not be on PyPI
yet; Home Assistant skips the pip install when the package already
imports. In a Home Assistant Core venv: `pip install -e /path/to/ha-vledger`,
then symlink or copy `custom_components/vledger` into the configuration
directory and restart. On Home Assistant OS, only a published version can
be loaded ([releasing.md](releasing.md)).

## The brand assets

`custom_components/vledger/brand/` holds the icon and logo Home Assistant
shows for the integration since 2026.3 — an instance older than that shows
none, since the domain is not in the `home-assistant/brands` repository —
and what HACS's validation looks for (`brand/icon.png`; ISSUE-0002). The
drawing is the owner's: the front of a black estate car, generated as an
image and kept outside the repository. The files are cuts of it, the
background made transparent, reduced to 256 colours, nothing else:

| file | what | size |
|---|---|---|
| `icon.png`, `icon@2x.png` | the front from the headlight to the cut, padded to a square | 256 × 256, 512 × 512 |
| `logo.png`, `logo@2x.png` | the car beside the wordmark, `vledger` in dark grey for light backgrounds | 702 × 256, 1402 × 512 |
| `dark_logo.png`, `dark_logo@2x.png` | the same with the wordmark in light grey, served on dark backgrounds | 702 × 256, 1402 × 512 |

The icon needs no dark variant: its silver grille and running light read on
both. The Home Assistant logo is not part of any of them and may not be —
the Home Assistant brand guidelines keep it out of other projects' logos.
A new drawing replaces all six.

## Where things are

```
src/vledger/               the library and CLI — all derivation logic, no Home Assistant
custom_components/vledger/ the integration: flows, capture, entities, translations;
                           brand/ the icon and logo it is shown with
tests/                     the library's tests; tests/ha/ the integration's;
                           tests/fixtures/ real streams, anonymised
docs/                      design documents, operating manuals, the user guide
.github/workflows/         tests.yml on every push, release.yml on a tag
```

The version is one number in three places — `pyproject.toml`,
`src/vledger/__init__.py`, `custom_components/vledger/manifest.json` —
and `tests/test_version.py` fails when they disagree.
