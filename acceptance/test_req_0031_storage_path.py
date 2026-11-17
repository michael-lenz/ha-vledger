# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0031: the storage path is configurable; the default lies below the Home
Assistant configuration directory.

This module is the storage path as a user of the ``vledger`` command meets it.
Derived from the requirement and the user guide's *Where the data is*: "Every
verb works on a data directory, given as ``--base DIR`` or by the environment
variable ``VLEDGER_BASE``, and defaults to the current directory"; a stream's
directory under it is named after its subject, ``vehicle-<id>`` or
``chargepoint-<id>``; and *The raw log: vledger l0*, whose ``l0 start`` and
``l0 state`` write a stream and ``l0 read`` prints it back. The default under
Home Assistant, and the data directory chosen in its options, are the other
module, test_req_0031_storage_path_ha.py.

Every test drives the ``vledger`` command as a program, in a working directory
of its own; nothing of ``vledger`` is imported."""

import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0031")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

SNAPSHOT = json.dumps([{"role": "odometer", "entity": "sensor.volvo_odometer",
                        "state": "123456", "unit": "km",
                        "since": "2026-10-08T22:41:10Z"}])


def vledger(*argv: str, cwd: Path, base_env: Path | None = None) -> str:
    """``vledger <argv>`` in a process of its own, run in ``cwd``, with
    ``VLEDGER_BASE`` set to ``base_env`` or not set at all."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    if base_env is not None:
        environ["VLEDGER_BASE"] = str(base_env)
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv],
                       check=False, capture_output=True, text=True, env=environ, cwd=cwd)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def write_stream(*base: str, cwd: Path, base_env: Path | None = None):
    """A vehicle's stream begun by hand, as the guide's *Writing* shows it:
    a start, and one odometer reading."""
    vledger("l0", "start", "--vehicle", "a7c1", "--homeassistant", "2026.10.1",
            "--snapshot", SNAPSHOT, "--t", "2026-10-09T06:00:00Z", *base,
            cwd=cwd, base_env=base_env)
    vledger("l0", "state", "--vehicle", "a7c1", "--role", "odometer",
            "--entity", "sensor.volvo_odometer", "--state", "123457", "--unit", "km",
            "--t", "2026-10-09T06:05:00Z", *base, cwd=cwd, base_env=base_env)


def kinds(*base: str, cwd: Path, base_env: Path | None = None) -> list[str]:
    """The kinds of the lines ``vledger l0 read --vehicle a7c1`` prints."""
    out = vledger("l0", "read", "--vehicle", "a7c1", *base, cwd=cwd, base_env=base_env)
    return [json.loads(x)["kind"] for x in out.splitlines() if x.strip()]


def subjects(directory: Path) -> list[str]:
    """The streams' directories directly under ``directory``."""
    return sorted(p.name for p in directory.iterdir() if p.is_dir()) if directory.exists() else []


@pytest.fixture
def dirs(tmp_path):
    """A working directory and two candidate data directories, all apart."""
    made = {name: tmp_path / name for name in ("cwd", "chosen", "other")}
    for d in made.values():
        d.mkdir()
    return made


def test_base_chooses_the_data_directory(dirs):
    """``--base DIR``: the stream is written into DIR, under the vehicle's own
    directory, and read back from there; nothing lands in the working
    directory."""
    write_stream("--base", str(dirs["chosen"]), cwd=dirs["cwd"])

    assert subjects(dirs["chosen"]) == ["vehicle-a7c1"]
    assert subjects(dirs["cwd"]) == []
    assert kinds("--base", str(dirs["chosen"]), cwd=dirs["cwd"]) == ["start", "state"]


def test_vledger_base_chooses_the_data_directory(dirs):
    """``VLEDGER_BASE`` set and no ``--base``: the stream is written into the
    directory the variable names, and read back from there."""
    write_stream(cwd=dirs["cwd"], base_env=dirs["chosen"])

    assert subjects(dirs["chosen"]) == ["vehicle-a7c1"]
    assert subjects(dirs["cwd"]) == []
    assert kinds(cwd=dirs["cwd"], base_env=dirs["chosen"]) == ["start", "state"]
    assert kinds("--base", str(dirs["chosen"]), cwd=dirs["other"]) == ["start", "state"]


def test_the_data_directory_defaults_to_the_current_directory(dirs):
    """Neither ``--base`` nor ``VLEDGER_BASE``: the stream is written into the
    current directory."""
    write_stream(cwd=dirs["cwd"])

    assert subjects(dirs["cwd"]) == ["vehicle-a7c1"]
    assert kinds(cwd=dirs["cwd"]) == ["start", "state"]
    assert kinds("--base", str(dirs["cwd"]), cwd=dirs["other"]) == ["start", "state"]

