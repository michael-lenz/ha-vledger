# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0026 with the ``vledger`` command: start, stop and heartbeat markers,
and capture gaps recognisable from them alone.

Derived from the requirement ("at start, a start marker with a snapshot of all
assigned roles; at orderly shutdown, a stop marker; at a fixed interval, a
heartbeat (default 60 min). Capture gaps are recognisable from these markers
alone"), README.md ("Start, stop and heartbeat markers make any capture gap
visible") and the user guide's *The raw log: ``vledger l0``*: the writing
verbs ``start --snapshot``, ``heartbeat`` and ``stop --reason``, each with
``--t`` for the event's time; ``read --kind`` to read a kind of line back; and
``gaps``, which "lists every span in which nothing was captured, with its
reason: ``crash``, ``stopped``, ``silence`` or ``open``", where "``--tolerance``
is how late a heartbeat may be (default 300 s)" and "``--now`` judges the end
of the stream against a time other than now". The user guide's *In Home
Assistant* adds that stopping Home Assistant ends the stream orderly, and "a
crash does not, and the next start shows as a gap".

The streams here hold markers and nothing else — no state line, no
configuration — so what ``gaps`` finds it finds from the markers alone. The
same requirement in Home Assistant is test_req_0026_markers_ha.py. Nothing of
``vledger`` is imported: the command runs in a process of its own."""

import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.req("REQ-0026")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
VEHICLE = "a7c1"
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
SNAPSHOT = [
    {"role": "odometer", "entity": "sensor.volvo_odometer", "state": "123456",
     "unit": "km", "since": "2026-09-30T22:41:10Z"},
    {"role": "position", "entity": "device_tracker.volvo", "state": "home",
     "since": "2026-09-30T22:41:10Z"},
    {"role": "fuel_level", "entity": "sensor.volvo_fuel", "state": "41.5",
     "unit": "L", "since": "2026-09-30T22:41:10Z"},
]


def vledger(base, *argv, ok=True):
    """``vledger <argv> --base <base>`` in a process of its own."""
    env = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e)])
    p = subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                       check=False, capture_output=True, text=True, env=env)
    if ok:
        assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(text):
    """A time the command wrote, in the spelling of :func:`iso`, so that a
    gap is compared by the instants it spans and not by how they are spelt."""
    return iso(datetime.fromisoformat(text).astimezone(UTC))


def jsonl(text):
    return [json.loads(x) for x in text.splitlines() if x.strip()]


class Stream:
    """One vehicle's raw log, written marker by marker as the integration
    would write it, at the times given."""

    def __init__(self, base):
        self.base = base

    def start(self, t, snapshot=()):
        vledger(self.base, "l0", "start", "--vehicle", VEHICLE, "--t", iso(t),
                "--homeassistant", "2026.10.1", "--snapshot", json.dumps(list(snapshot)))
        return self

    def heartbeat(self, t, lines=0):
        vledger(self.base, "l0", "heartbeat", "--vehicle", VEHICLE, "--t", iso(t),
                "--lines", str(lines))
        return self

    def hourly(self, since, until):
        """A heartbeat every hour after ``since``, up to ``until``."""
        t = since + HOUR
        while t <= until:
            self.heartbeat(t)
            t += HOUR
        return self

    def stop(self, t, reason="shutdown"):
        vledger(self.base, "l0", "stop", "--vehicle", VEHICLE, "--t", iso(t),
                "--reason", reason)
        return self

    def read(self, kind=None):
        argv = ["l0", "read", "--vehicle", VEHICLE] + (["--kind", kind] if kind else [])
        return jsonl(vledger(self.base, *argv).stdout)

    def gaps(self, now, *more):
        out = vledger(self.base, "l0", "gaps", "--vehicle", VEHICLE, "--now", iso(now),
                      "--json", *more).stdout
        return [(instant(g["start"]), instant(g["end"]), g["reason"])
                for g in json.loads(out)]


@pytest.fixture
def stream(tmp_path):
    return Stream(tmp_path)


def test_the_start_marker_carries_the_snapshot_of_every_assigned_role(stream):
    """A start written with three assigned roles reads back as one start line
    at its time whose snapshot holds each of them with its entity, state and
    unit as given."""
    stream.start(T0, SNAPSHOT)
    [start] = stream.read("start")
    assert start["kind"] == "start"
    assert start["t"].startswith(iso(T0)[:-1])
    got = {s["role"]: s for s in start["snapshot"]}
    assert set(got) == {s["role"] for s in SNAPSHOT}
    for want in SNAPSHOT:
        for key in ("entity", "state", "unit"):
            assert got[want["role"]].get(key) == want.get(key), (want["role"], key)


def test_the_three_markers_read_back_in_order_with_their_kinds(stream):
    """start, heartbeats and stop are each a line of their own kind, read
    back in the order written, and each kind can be read alone."""
    stream.start(T0, SNAPSHOT).hourly(T0, T0 + 2 * HOUR).stop(T0 + 2.5 * HOUR)
    assert [ln["kind"] for ln in stream.read()] == ["start", "heartbeat", "heartbeat", "stop"]
    assert len(stream.read("heartbeat")) == 2
    assert len(stream.read("stop")) == 1


def test_a_stream_with_a_heartbeat_every_hour_has_no_gap(stream):
    """Started, a heartbeat every 60 minutes for a day, nothing else: no gap,
    though no state line was written all day — a vehicle that reports nothing
    is not a gap in capture."""
    end = T0 + 24 * HOUR
    stream.start(T0, SNAPSHOT).hourly(T0, end)
    assert stream.gaps(end + timedelta(minutes=10)) == []


def test_an_orderly_stop_and_the_next_start_show_as_a_stopped_gap(stream):
    """Stopped at 02:30 and started again at 05:00: one gap from the stop to
    the start, for the reason ``stopped``."""
    stream.start(T0, SNAPSHOT).hourly(T0, T0 + 2 * HOUR).stop(T0 + 2.5 * HOUR)
    stream.start(T0 + 5 * HOUR, SNAPSHOT).hourly(T0 + 5 * HOUR, T0 + 8 * HOUR)
    assert stream.gaps(T0 + 8 * HOUR + timedelta(minutes=1)) == [
        (iso(T0 + 2.5 * HOUR), iso(T0 + 5 * HOUR), "stopped")]


def test_a_start_without_a_stop_before_it_shows_as_a_crash(stream):
    """The last heartbeat at 03:00, then a start at 05:00 with no stop in
    between — a crash, as the user guide says, shows as a gap from the last
    line before it to the new start."""
    stream.start(T0, SNAPSHOT).hourly(T0, T0 + 3 * HOUR)
    stream.start(T0 + 5 * HOUR, SNAPSHOT).hourly(T0 + 5 * HOUR, T0 + 8 * HOUR)
    assert stream.gaps(T0 + 8 * HOUR + timedelta(minutes=1)) == [
        (iso(T0 + 3 * HOUR), iso(T0 + 5 * HOUR), "crash")]


def test_missing_heartbeats_show_as_silence(stream):
    """Heartbeats every hour to 03:00, then none until 07:00, with no start
    or stop in between: a gap from the last heartbeat to the next line, for
    the reason ``silence``."""
    stream.start(T0, SNAPSHOT).hourly(T0, T0 + 3 * HOUR)
    stream.heartbeat(T0 + 7 * HOUR).hourly(T0 + 7 * HOUR, T0 + 9 * HOUR)
    assert stream.gaps(T0 + 9 * HOUR + timedelta(minutes=1)) == [
        (iso(T0 + 3 * HOUR), iso(T0 + 7 * HOUR), "silence")]


@pytest.mark.parametrize("late_min, gap", [(4, False), (6, True)])
def test_a_heartbeat_may_be_late_by_the_tolerance_and_no_more(stream, late_min, gap):
    """At the default interval of 60 minutes and the default tolerance of
    300 s, a heartbeat 4 minutes late is no gap and one 6 minutes late is a
    silence; with ``--tolerance 600`` neither is."""
    late = T0 + HOUR + timedelta(minutes=late_min)
    stream.start(T0, SNAPSHOT).heartbeat(late).hourly(late, late + 2 * HOUR)
    now = late + 2 * HOUR + timedelta(minutes=1)
    want = [(iso(T0), iso(late), "silence")] if gap else []
    assert stream.gaps(now) == want
    assert stream.gaps(now, "--tolerance", "600") == []


def test_a_stream_that_ends_without_a_stop_is_open_at_its_end(stream):
    """The last heartbeat at 03:00 and nothing since: judged at 06:00 the end
    of the stream is a gap from 03:00 to then, for the reason ``open``;
    judged at 03:30, within the interval, it is no gap yet."""
    stream.start(T0, SNAPSHOT).hourly(T0, T0 + 3 * HOUR)
    assert stream.gaps(T0 + 3.5 * HOUR) == []
    assert stream.gaps(T0 + 6 * HOUR) == [(iso(T0 + 3 * HOUR), iso(T0 + 6 * HOUR), "open")]


def test_the_gaps_of_a_day_are_told_apart_by_their_reasons(stream):
    """One stream with a stop, a crash, a silence and an open end, in that
    order: four gaps, each with its reason, in the order they happened, and
    ``--min`` hides the ones shorter than it."""
    stream.start(T0, SNAPSHOT).hourly(T0, T0 + HOUR).stop(T0 + 1.5 * HOUR)    # stopped
    stream.start(T0 + 2 * HOUR, SNAPSHOT).hourly(T0 + 2 * HOUR, T0 + 3 * HOUR)
    stream.start(T0 + 5 * HOUR, SNAPSHOT)                                      # crash
    stream.heartbeat(T0 + 9 * HOUR)                                            # silence
    now = T0 + 12 * HOUR                                                       # open
    assert stream.gaps(now) == [
        (iso(T0 + 1.5 * HOUR), iso(T0 + 2 * HOUR), "stopped"),
        (iso(T0 + 3 * HOUR), iso(T0 + 5 * HOUR), "crash"),
        (iso(T0 + 5 * HOUR), iso(T0 + 9 * HOUR), "silence"),
        (iso(T0 + 9 * HOUR), iso(now), "open"),
    ]
    assert [g[2] for g in stream.gaps(now, "--min", "7201")] == ["silence", "open"]
