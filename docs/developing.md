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

## Running the integration in a Home Assistant

A development instance needs the library importable before it loads the
integration, because the manifest pins a version that may not be on PyPI
yet; Home Assistant skips the pip install when the package already
imports. In a Home Assistant Core venv: `pip install -e /path/to/ha-vledger`,
then symlink or copy `custom_components/vledger` into the configuration
directory and restart. On Home Assistant OS, only a published version can
be loaded ([releasing.md](releasing.md)).

## Where things are

```
src/vledger/               the library and CLI — all derivation logic, no Home Assistant
custom_components/vledger/ the integration: flows, capture, entities, translations
tests/                     the library's tests; tests/ha/ the integration's
docs/                      design documents, operating manuals, the user guide
.github/workflows/         tests.yml on every push, release.yml on a tag
```

The version is one number in three places — `pyproject.toml`,
`src/vledger/__init__.py`, `custom_components/vledger/manifest.json` —
and `tests/test_version.py` fails when they disagree.
