# SPDX-License-Identifier: BSD-3-Clause
"""``vledger l0 stats``: a stream counted in one pass, built through the
verbs (ADR-0005)."""

import json

from test_l0 import V, build_stream, run
from vledger import l0, layout, stats


def build_sampled(base, capsys):
    """An odometer every ten minutes, one sample at 25, a charging state
    with a value the map does not list, then a crash and two more lines."""
    b = ["--base", str(base), "--vehicle", "a7c1"]
    run(capsys, "start", *b, "--t", "2026-10-09T06:00:00Z", "--homeassistant", "2026.10.1")
    run(capsys, "config", *b, "--t", "2026-10-09T06:00:00.050Z", "--config", json.dumps(
        {"name": "Volvo", "roles": {"charging_state": {"entity": "sensor.c",
                                                       "map": {"charging": ["Charging"]}}},
         "thresholds": {"heartbeat_s": 3600}}))
    for minute, km in ((10, 101), (20, 102), (45, 103), (55, 104)):
        run(capsys, "state", *b, "--t", f"2026-10-09T06:{minute:02d}:00Z", "--role", "odometer",
            "--entity", "sensor.o", "--state", str(km), "--unit", "km")
    for minute, value in ((12, "Charging"), (30, "Done"), (40, "unavailable"), (50, "Idle")):
        run(capsys, "state", *b, "--t", f"2026-10-09T06:{minute:02d}:00Z",
            "--role", "charging_state", "--entity", "sensor.c", "--state", value)
    run(capsys, "heartbeat", *b, "--t", "2026-10-09T07:00:00Z", "--lines", "8")
    # A crash: no stop, a new start hours later; intervals do not span it.
    run(capsys, "start", *b, "--t", "2026-10-09T10:00:00Z", "--homeassistant", "2026.10.1")
    run(capsys, "state", *b, "--t", "2026-10-09T10:05:00Z", "--role", "odometer",
        "--entity", "sensor.o", "--state", "110", "--unit", "km")
    return b


def test_stats_counts_a_stream(tmp_path, capsys):
    b = build_sampled(tmp_path, capsys)
    code, out, _ = run(capsys, "stats", *b, "--json", "--since", "2026-10-09T06:30:00Z",
                       "--now", "2026-10-09T10:10:00Z")
    assert code == 0
    d = json.loads(out)
    path = layout.l0_file(tmp_path, V, "2026-10")
    assert d["files"] == {"2026-10": path.stat().st_size} and d["bytes"] == path.stat().st_size
    assert d["lines_by_kind"] == {"start": 2, "config": 1, "state": 9, "heartbeat": 1}
    assert d["state_lines_by_role"] == {"odometer": 5, "charging_state": 4}
    assert d["lines_since_start"] == 1           # after the second start
    assert d["lines_since"] == 6                 # 06:30 and later
    assert d["last_line_at"] == "2026-10-09T10:05:00Z"
    assert d["last_heartbeat_at"] == "2026-10-09T07:00:00Z"
    assert d["last_state"] == {"role": "odometer", "t": "2026-10-09T10:05:00Z"}
    assert d["last_states"]["charging_state"]["state"] == "Idle"
    # 10, 25 and 10 minutes — the 3 h 10 min across the crash is not one.
    odo = d["intervals"]["odometer"]
    assert odo["count"] == 3 and odo["median_s"] == 600
    assert 600 < odo["p95_s"] <= 1500
    # Done and Idle are not listed; unavailable says nothing; Charging is.
    assert d["unlisted"] == {"charging_state": ["Done", "Idle"]}
    assert [(g["reason"], round(g["seconds"])) for g in d["gaps"]] == [("crash", 10800)]


def test_stats_judges_an_open_end_against_now(tmp_path, capsys):
    b = build_sampled(tmp_path, capsys)
    _, out, _ = run(capsys, "stats", *b, "--json", "--now", "2026-10-09T13:00:00Z")
    assert [g["reason"] for g in json.loads(out)["gaps"]] == ["crash", "open"]


def test_stats_speaks(tmp_path, capsys):
    b = build_sampled(tmp_path, capsys)
    code, out, _ = run(capsys, "stats", *b, "--now", "2026-10-09T10:10:00Z")
    assert code == 0
    assert "1 month file(s)" in out
    assert "odometer: 5 line(s)" in out and "median 600 s" in out
    assert "charging_state: met and not in the map: Done, Idle" in out
    assert "1 gap(s), latest crash of 10800 s" in out


def test_an_empty_stream_counts_to_nothing(tmp_path, capsys):
    code, out, _ = run(capsys, "stats", "--base", str(tmp_path), "--vehicle", "a7c1", "--json")
    d = json.loads(out)
    assert code == 0 and d["bytes"] == 0 and d["gaps"] == [] and d["last_state"] is None


def test_the_gap_finder_fed_line_by_line_finds_what_gaps_finds(tmp_path, capsys):
    build_stream(tmp_path, capsys)
    finder = l0.GapFinder()
    for r in l0.read(tmp_path, V):
        finder.feed(r.line)
    finder.close("2026-10-01T07:00:00Z")
    assert finder.found == l0.gaps(tmp_path, V, now="2026-10-01T07:00:00Z")
    assert stats.scan(tmp_path, V).gaps == finder.found


def test_intervals_of_one_and_none():
    assert stats.intervals_of([]) is None
    assert stats.intervals_of([30.0]) == stats.Intervals(1, 30.0, 30.0)
