# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0029: the raw log's format is JSON Lines (UTF-8), one record per line;
an incomplete last line, e.g. after a crash, is recognised on reading and
skipped without losing subsequent lines.

Derived from the requirement, README.md ("a raw log (L0): JSON Lines,
append-only, one stream per vehicle and per charge point, rotated monthly")
and the user guide: *Where the data is* (every verb works on a data directory
given as ``--base``; a stream is named by ``--vehicle ID``, and the subject's
directory under the data directory is ``vehicle-<id>``) and *The raw log:
vledger l0* — the verbs the integration writes a stream with, "usable by hand
to build one"; ``l0 read`` prints the stream as JSON Lines; ``l0 validate``
lists every problem, "a warning is a line readers skip (a torn last line, an
unknown kind)", and exits 1 only on an error; ``l0 gaps`` lists capture gaps,
``crash`` among their reasons; and the ``derive`` verbs read the stream.

A crash is what a user can leave on disk: a line cut off part way, with no
line break after it, at the end of the stream's file. The file is found as
the user finds it, under ``vehicle-<id>`` in the data directory. Every test
drives the ``vledger`` command as a program; nothing of ``vledger`` is
imported."""

import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0029")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

VEHICLE = ("--vehicle", "a7c1")


def run(base: Path, *argv: str) -> subprocess.CompletedProcess:
    """``vledger <argv> --base <base>`` in a process of its own."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    return subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                          check=False, capture_output=True, text=True, env=environ, cwd=base)


def vledger(base: Path, *argv: str, expect: int = 0) -> str:
    p = run(base, *argv)
    assert p.returncode == expect, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


def read(base: Path, *argv: str) -> list[dict]:
    """The lines ``vledger l0 read`` prints, each one JSON object."""
    return [json.loads(x) for x in vledger(base, "l0", "read", *VEHICLE, *argv).splitlines()
            if x.strip()]


def start(base: Path, t: str) -> None:
    vledger(base, "l0", "start", *VEHICLE, "--homeassistant", "2026.10.1",
            "--snapshot", "[]", "--t", t)


def odometer(base: Path, km: str, t: str) -> None:
    vledger(base, "l0", "state", *VEHICLE, "--role", "odometer",
            "--entity", "sensor.golf_odometer", "--state", km, "--unit", "km", "--t", t)


def position(base: Path, zone: str, t: str) -> None:
    vledger(base, "l0", "state", *VEHICLE, "--role", "position",
            "--entity", "device_tracker.golf", "--state", zone, "--t", t)


def stream_files(base: Path) -> list[Path]:
    """Every file of the vehicle's stream, under its directory in the data
    directory."""
    files = sorted(p for p in (base / "vehicle-a7c1").rglob("*") if p.is_file())
    assert files, "nothing written under vehicle-a7c1"
    return files


def crash(base: Path, fragment: bytes) -> None:
    """What a crash mid-write leaves: the start of a line, and no line break,
    at the end of the stream's one file."""
    (only,) = stream_files(base)
    with only.open("ab") as f:
        f.write(fragment)


# A state line cut off part way, as a crash leaves it.
TORN = b'{"v":5,"t":"2026-10-08T10:09:00.000Z","kind":"state","subject":"a7c1","role":"odo'
# The same, cut inside the two bytes of the ü of a zone's name.
TORN_IN_A_CHARACTER = ('{"v":5,"t":"2026-10-08T10:09:00.000Z","kind":"state","subject":"a7c1",'
                       '"role":"position","entity":"device_tracker.golf","state":"Bü'
                       ).encode()[:-1]


@pytest.fixture
def base(tmp_path):
    """A data directory with one vehicle's stream: a start, a position named
    with a character beyond ASCII, and two odometer readings."""
    base = tmp_path
    start(base, "2026-10-08T10:00:00Z")
    position(base, "Büro", "2026-10-08T10:01:00Z")
    odometer(base, "12345", "2026-10-08T10:02:00Z")
    odometer(base, "12346", "2026-10-08T10:05:00Z")
    return base


WRITTEN = [("start", None), ("state", "Büro"), ("state", "12345"), ("state", "12346")]


def kinds_and_states(lines: list[dict]) -> list[tuple]:
    return [(ln.get("kind"), ln.get("state")) for ln in lines]


# --- the format -----------------------------------------------------------------

def test_the_stream_is_utf_8_with_one_json_object_on_each_line(base):
    """The stream's file decodes as UTF-8, ends with a line break, and holds
    one JSON object per line, one for each record written, in order."""
    lines = []
    for path in stream_files(base):
        text = path.read_bytes().decode("utf-8")      # strict: raises on anything else
        assert text.endswith("\n"), path
        lines += [json.loads(x) for x in text.split("\n")[:-1]]
    assert all(isinstance(ln, dict) for ln in lines)
    assert kinds_and_states(lines) == WRITTEN


def test_a_name_beyond_ascii_reads_back_as_written(base):
    """The zone's ü survives the round trip through the file."""
    assert [ln["state"] for ln in read(base, "--kind", "state", "--role", "position")] == ["Büro"]


def test_l0_read_prints_json_lines(base):
    """What ``l0 read`` prints is JSON Lines too: one object per line, every
    record written."""
    assert kinds_and_states(read(base)) == WRITTEN


# --- a torn last line -------------------------------------------------------------

@pytest.mark.parametrize("fragment", [TORN, TORN_IN_A_CHARACTER],
                         ids=["cut-in-a-key", "cut-in-a-character"])
def test_a_torn_last_line_is_skipped_on_reading(base, fragment):
    """``l0 read`` succeeds on a stream whose last line a crash cut off, and
    prints every complete line and nothing of the torn one."""
    crash(base, fragment)
    assert kinds_and_states(read(base)) == WRITTEN


def test_a_torn_last_line_is_skipped_when_one_role_is_read(base):
    crash(base, TORN)
    assert [ln["state"] for ln in read(base, "--kind", "state", "--role", "odometer")] == [
        "12345", "12346"]


def test_validate_recognises_a_torn_last_line_as_a_warning(base):
    """``l0 validate`` names the torn line as a warning, a line readers skip,
    not as an error, and counts only the complete lines."""
    crash(base, TORN)
    report = json.loads(vledger(base, "l0", "validate", *VEHICLE, "--json", expect=0))
    assert report["lines"] == len(WRITTEN)
    problems = report["problems"]
    assert [p["severity"] for p in problems] == ["warning"], problems
    assert "torn" in problems[0]["what"], problems
    assert "torn last line" in vledger(base, "l0", "validate", *VEHICLE, expect=0)


@pytest.mark.parametrize("verb", [
    ("l0", "gaps", "--now", "2026-10-08T10:30:00Z"),
    ("derive", "trips"),
    ("derive", "refuellings"),
    ("derive", "charging"),
    ("derive", "periods"),
], ids=" ".join)
def test_every_reader_of_the_stream_reads_past_a_torn_last_line(base, verb):
    """Each verb the user guide gives for reading a stream succeeds on one
    whose last line is torn, as it does on the same stream without it, and
    prints the same."""
    whole = vledger(base, *verb, *VEHICLE)
    crash(base, TORN)
    assert vledger(base, *verb, *VEHICLE) == whole


def test_l0_stats_counts_the_complete_lines_past_a_torn_last_line(base):
    """``l0 stats`` succeeds on the torn stream and counts what it counts
    without the torn line: the lines by kind and by role, and the last line.
    The file's size it reports grows by the fragment, and is left out."""
    def counted():
        stats = json.loads(vledger(base, "l0", "stats", *VEHICLE,
                                   "--now", "2026-10-08T10:30:00Z", "--json"))
        return {k: v for k, v in stats.items() if k not in ("files", "bytes")}
    whole = counted()
    crash(base, TORN)
    assert counted() == whole


# --- the lines that follow it ------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="ISSUE-0135")
@pytest.mark.parametrize("fragment", [TORN, TORN_IN_A_CHARACTER],
                         ids=["cut-in-a-key", "cut-in-a-character"])
def test_the_lines_written_after_the_crash_are_not_lost(base, fragment):
    """After the crash the stream goes on: a new start and further readings
    are written. Reading it skips the torn line and loses neither what came
    before it nor anything after it."""
    crash(base, fragment)
    start(base, "2026-10-08T11:00:00Z")
    odometer(base, "12350", "2026-10-08T11:02:00Z")
    position(base, "Zuhause", "2026-10-08T11:03:00Z")
    assert kinds_and_states(read(base)) == [
        *WRITTEN, ("start", None), ("state", "12350"), ("state", "Zuhause")]


@pytest.mark.xfail(strict=True, reason="ISSUE-0135")
def test_the_crash_shows_as_a_gap_and_the_stream_reads_on(base):
    """The crash and the restart show in ``l0 gaps`` as a gap with the reason
    ``crash``, and the stream after it is counted by ``l0 stats``."""
    crash(base, TORN)
    start(base, "2026-10-08T11:00:00Z")
    odometer(base, "12350", "2026-10-08T11:02:00Z")
    gaps = json.loads(vledger(base, "l0", "gaps", *VEHICLE, "--now", "2026-10-08T11:05:00Z",
                              "--json"))
    assert [g["reason"] for g in gaps] == ["crash"], gaps
    vledger(base, "l0", "stats", *VEHICLE, "--now", "2026-10-08T11:05:00Z")
