# SPDX-License-Identifier: BSD-3-Clause
"""L0: written whole, read back in order, checked, and its gaps found.

Scenarios drive the verbs, as ADR-0005 wants: a stream is built the way the
integration would build it, through ``vledger l0 …``, and read the same way.
"""

import json

import pytest

from vledger import clock, l0
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")


def run(capsys, *argv):
    code = main(["l0", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def build_stream(base, capsys):
    """Two months of a vehicle: start, config, states, a crash, a stop."""
    b = ["--base", str(base), "--vehicle", "a7c1"]
    snapshot = json.dumps([{"role": "odometer", "entity": "sensor.o", "state": "100",
                            "unit": "km", "since": "2026-09-30T20:00:00.000Z"}])
    assert run(capsys, "start", *b, "--t", "2026-09-30T22:00:00.000Z",
               "--homeassistant", "2026.10.1", "--snapshot", snapshot)[0] == 0
    assert run(capsys, "config", *b, "--t", "2026-09-30T22:00:00.050Z", "--config",
               '{"name":"Volvo","thresholds":{"heartbeat_s":3600}}')[0] == 0
    assert run(capsys, "state", *b, "--t", "2026-09-30T23:30:00.000Z", "--role", "odometer",
               "--entity", "sensor.o", "--state", "110", "--unit", "km")[0] == 0
    # The month turns between these two lines.
    assert run(capsys, "state", *b, "--t", "2026-10-01T00:10:00.000Z", "--role", "position",
               "--entity", "device_tracker.v", "--state", "not_home",
               "--attr", "latitude=48.1", "--attr", "longitude=11.5",
               "--attr", "battery=80")[0] == 0
    assert run(capsys, "heartbeat", *b, "--t", "2026-10-01T00:30:00.000Z", "--lines", "2")[0] == 0
    # Silence: nothing for three hours while running.
    assert run(capsys, "state", *b, "--t", "2026-10-01T03:40:00.000Z", "--role", "odometer",
               "--entity", "sensor.o", "--state", "120", "--unit", "km")[0] == 0
    # A crash: a second start with no stop before it.
    assert run(capsys, "start", *b, "--t", "2026-10-01T06:00:00.000Z",
               "--homeassistant", "2026.10.1")[0] == 0
    assert run(capsys, "stop", *b, "--t", "2026-10-01T06:05:00.000Z", "--reason", "reload")[0] == 0
    assert run(capsys, "start", *b, "--t", "2026-10-01T06:05:30.000Z",
               "--homeassistant", "2026.10.1")[0] == 0
    assert run(capsys, "stop", *b, "--t", "2026-10-01T07:00:00.000Z", "--reason", "shutdown")[0] == 0
    return b


def test_lines_land_in_the_month_file_their_own_time_names(tmp_path, capsys):
    build_stream(tmp_path, capsys)
    files = sorted(p.name for p in (tmp_path / "vehicle-a7c1" / "l0").iterdir())
    assert files == ["2026-09.jsonl", "2026-10.jsonl"]
    sept = (tmp_path / "vehicle-a7c1/l0/2026-09.jsonl").read_text().splitlines()
    assert [json.loads(x)["kind"] for x in sept] == ["start", "config", "state"]


def test_a_state_line_carries_only_the_role_relevant_attributes(tmp_path, capsys):
    build_stream(tmp_path, capsys)
    pos = next(r.line for r in l0.read(tmp_path, V, role="position"))
    assert pos["attrs"] == {"latitude": 48.1, "longitude": 11.5}
    odo = next(r.line for r in l0.read(tmp_path, V, role="odometer"))
    assert "attrs" not in odo and odo["state"] == "110" and odo["unit"] == "km"


def test_read_is_in_order_across_months_and_narrowable(tmp_path, capsys):
    b = build_stream(tmp_path, capsys)
    ts = [r.line["t"] for r in l0.read(tmp_path, V)]
    assert ts == sorted(ts) and len(ts) == 10
    code, out, err = run(capsys, "read", *b, "--kind", "state",
                         "--since", "2026-10-01T00:00:00Z")
    assert code == 0 and err.strip() == "2 line(s)"
    assert [json.loads(x)["state"] for x in out.splitlines()] == ["not_home", "120"]


def test_a_torn_last_line_is_skipped_and_reported(tmp_path, capsys):
    b = build_stream(tmp_path, capsys)
    path = tmp_path / "vehicle-a7c1/l0/2026-10.jsonl"
    with open(path, "a") as f:
        f.write('{"v":1,"t":"2026-10-01T08:00:00.000Z","kind":"sta')
    assert len(list(l0.read(tmp_path, V))) == 10
    code, out, _ = run(capsys, "validate", *b)
    assert code == 0 and "torn last line" in out and "0 error(s), 1 warning(s)" in out


def test_a_torn_line_in_the_middle_is_an_error(tmp_path, capsys):
    b = build_stream(tmp_path, capsys)
    path = tmp_path / "vehicle-a7c1/l0/2026-10.jsonl"
    lines = path.read_text().splitlines()
    lines.insert(2, '{"v":1,"t":"2026-10-01T0')
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(l0.TornLine):
        list(l0.read(tmp_path, V))
    code, out, _ = run(capsys, "validate", *b)
    assert code == 1 and "[error] 2026-10.jsonl:3: not JSON" in out


def test_validate_passes_a_clean_stream_and_names_its_versions(tmp_path, capsys):
    b = build_stream(tmp_path, capsys)
    code, out, _ = run(capsys, "validate", *b, "--json")
    report = json.loads(out)
    assert code == 0 and report["problems"] == [] and report["versions"] == [2]
    assert report["by_kind"] == {"start": 3, "config": 1, "state": 3, "heartbeat": 1, "stop": 2}


def test_validate_catches_what_the_writer_refuses(tmp_path, capsys):
    b = build_stream(tmp_path, capsys)
    path = tmp_path / "vehicle-a7c1/l0/2026-10.jsonl"
    with open(path, "a") as f:
        f.write('{"v":1,"t":"2026-11-01T00:00:00.000Z","kind":"state","subject":"a7c1",'
                '"role":"speed","entity":"x","state":5}\n')
        f.write('{"v":1,"t":"2026-10-02T00:00:00.000Z","kind":"wibble","subject":"other"}\n')
    code, out, _ = run(capsys, "validate", *b)
    assert code == 1
    assert "not in month file 2026-10" in out
    assert "unknown role 'speed'" in out and "state is not a string" in out
    assert "unknown kind 'wibble'" in out and "subject 'other'" in out
    assert "t runs backwards" in out


def test_gaps_from_the_markers(tmp_path, capsys):
    b = build_stream(tmp_path, capsys)
    code, out, _ = run(capsys, "gaps", *b, "--json", "--now", "2026-10-01T07:00:00Z")
    found = json.loads(out)
    assert code == 0
    assert [(g["reason"], round(g["seconds"])) for g in found] == [
        ("silence", 5400),    # 22:00 to 23:30 while running, no heartbeat
        ("silence", 11400),   # 00:30 to 03:40, the same
        ("crash", 8400),      # 03:40 to the 06:00 start, no stop before it
        ("stopped", 30),      # reload: 06:05:00 to 06:05:30
    ]
    _, out, _ = run(capsys, "gaps", *b, "--min", "60")
    assert out.count("\n") == 3 and "stopped" not in out


def test_an_open_gap_when_the_stream_ends_without_a_stop(tmp_path, capsys):
    b = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    run(capsys, "start", *b, "--t", "2026-10-01T06:00:00Z", "--homeassistant", "2026.10.1")
    found = l0.gaps(tmp_path, V, now="2026-10-01T09:00:00Z")
    assert [g.reason for g in found] == ["open"]
    assert l0.gaps(tmp_path, V, now="2026-10-01T06:30:00Z") == []


def test_the_writer_refuses_what_l0_cannot_hold():
    with pytest.raises(TypeError):
        l0.state("2026-10-01T00:00:00Z", V, "odometer", "sensor.o", 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        l0.state("2026-10-01T00:00:00Z", V, "speed", "sensor.o", "5")
    with pytest.raises(ValueError):
        l0.state("2026-10-01 00:00", V, "odometer", "sensor.o", "5")
    with pytest.raises(ValueError):
        l0.stop("2026-10-01T00:00:00Z", V, reason="power-cut")
    with pytest.raises(ValueError):
        l0.start("2026-10-01T00:00:00Z", V, vledger="0", homeassistant="0",
                 snapshot=[{"role": "odometer", "entity": "x", "state": "1"}])


def test_a_newer_schema_version_is_refused_on_reading(tmp_path, capsys):
    b = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    run(capsys, "start", *b, "--t", "2026-10-01T06:00:00Z", "--homeassistant", "2026.10.1")
    path = tmp_path / "vehicle-a7c1/l0/2026-10.jsonl"
    with open(path, "a") as f:
        f.write('{"v":3,"t":"2026-10-01T06:01:00.000Z","kind":"stop","subject":"a7c1","reason":"shutdown"}\n')
    with pytest.raises(ValueError, match="newer than this reader"):
        list(l0.read(tmp_path, V))
    code, out, _ = run(capsys, "validate", *b)
    assert code == 1 and "schema version 3 is newer" in out


def test_a_state_line_carries_when_the_old_value_was_last_reported(tmp_path, capsys):
    b = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    code, _, _ = run(capsys, "state", *b, "--t", "2026-10-01T06:02:00Z", "--role", "odometer",
                       "--entity", "sensor.o", "--state", "101",
                       "--reported-before", "2026-10-01T06:00:00.000Z")
    assert code == 0
    line = next(l0.read(tmp_path, V)).line
    assert line["v"] == 2 and line["reported_before"] == "2026-10-01T06:00:00.000Z"
    with pytest.raises(ValueError, match="later than the line"):
        l0.state("2026-10-01T06:00:00.000Z", V, "odometer", "sensor.o", "102",
                  reported_before="2026-10-01T06:00:00.001Z")


def test_version_1_stays_readable_and_never_carries_reported_before(tmp_path, capsys):
    """Version 1 lines exist on disk and cannot be written any more, so they
    are spelled out here."""
    b = ["--base", str(tmp_path), "--vehicle", "a7c1"]
    path = tmp_path / "vehicle-a7c1/l0/2026-10.jsonl"
    path.parent.mkdir(parents=True)
    with open(path, "w") as f:
        f.write('{"v":1,"t":"2026-10-01T06:00:00.000Z","kind":"state","subject":"a7c1",'
                '"role":"odometer","entity":"sensor.o","state":"100"}\n')
    assert [r.line["state"] for r in l0.read(tmp_path, V)] == ["100"]
    code, out, _ = run(capsys, "validate", *b, "--json")
    assert code == 0 and json.loads(out)["versions"] == [1]
    with open(path, "a") as f:
        f.write('{"v":1,"t":"2026-10-01T06:01:00.000Z","kind":"state","subject":"a7c1",'
                '"role":"odometer","entity":"sensor.o","state":"101",'
                '"reported_before":"2026-10-01T06:00:30.000Z"}\n')
        f.write('{"v":2,"t":"2026-10-01T06:02:00.000Z","kind":"state","subject":"a7c1",'
                '"role":"odometer","entity":"sensor.o","state":"102",'
                '"reported_before":"2026-10-01T06:03:00.000Z"}\n')
    code, out, _ = run(capsys, "validate", *b)
    assert code == 1
    assert "reported_before in a version 1 line" in out and "reported_before is later than t" in out


def test_time_is_spelled_one_way():
    assert clock.to_text(clock.parse("2026-10-09T09:12:03.412+02:00")) == "2026-10-09T07:12:03.412Z"
    assert clock.month_of("2026-10-01T00:00:00+02:00") == "2026-09"
    with pytest.raises(ValueError):
        clock.parse("2026-10-09T07:12:03")


def test_the_cli_complains_without_a_traceback(tmp_path, capsys):
    code, _, err = run(capsys, "read", "--base", str(tmp_path))
    assert code == 2 and "name the stream" in err
    code, _, err = run(capsys, "state", "--base", str(tmp_path), "--vehicle", "x",
                       "--role", "odometer", "--entity", "e", "--state", "1", "--t", "yesterday")
    assert code == 2 and "yesterday" in err
