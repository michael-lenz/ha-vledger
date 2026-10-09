# SPDX-License-Identifier: BSD-3-Clause
"""L1 on disk (ADR-0009): completed events only, the cursor, rebuilds that
swap whole, and the proof that incremental equals batch."""

import json
import shutil

from test_trips import Builder, at
from vledger import l1, series, trips
from vledger.cli import main
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")


def read_lines(base, kind):
    return list(l1.read(base, V, kind))


def test_only_completed_trips_are_written(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    # The standstill after the drive has not elapsed: the trip is open.
    assert trips.derive_from(tmp_path, V, completed_only=True) == []
    assert len(trips.derive_from(tmp_path, V)) == 1
    b.heartbeat(end + 60)   # 60 min of nothing: completed
    assert len(trips.derive_from(tmp_path, V, completed_only=True)) == 1


def test_rebuild_writes_files_and_manifest_and_swaps_whole(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    b.heartbeat(end + 60)
    manifest = l1.rebuild(tmp_path, V)
    d = l1.l1_dir(tmp_path, V)
    assert sorted(p.name for p in d.iterdir()) == sorted(
        ["manifest.json", *(l1.FILES[k] for k in l1.DERIVATIONS)])
    assert not (d.with_name("l1.tmp")).exists() and not d.with_name("l1.old").exists()
    assert manifest["through"] == {"trip": at(107)}
    assert manifest["config"].startswith("sha256:") and manifest["receipts"].startswith("sha256:")
    assert [e["kind"] for e in read_lines(tmp_path, "trip")] == ["trip"]
    assert l1.rebuild_due(tmp_path, V) is None
    # A second rebuild replaces, never appends.
    l1.rebuild(tmp_path, V)
    assert len(read_lines(tmp_path, "trip")) == 1


def test_a_rebuild_is_due_when_version_config_or_receipts_change(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    b.heartbeat(end + 60)
    assert l1.rebuild_due(tmp_path, V) == "no manifest"
    l1.rebuild(tmp_path, V)
    assert l1.rebuild_due(tmp_path, V) is None
    (tmp_path / "vehicle-a7c1" / "receipts.jsonl").write_text('{"uuid":"x"}\n')
    assert l1.rebuild_due(tmp_path, V) == "the receipts changed"
    l1.rebuild(tmp_path, V)
    b.run("config", *b.b, "--t", at(end + 70), "--config", '{"name":"V","roles":{"odometer":{"entity":"sensor.o"}}}')
    assert l1.rebuild_due(tmp_path, V) == "the configuration changed"
    l1.rebuild(tmp_path, V)
    m = json.loads((l1.l1_dir(tmp_path, V) / "manifest.json").read_text())
    m["vledger"] = "0.0.1"
    (l1.l1_dir(tmp_path, V) / "manifest.json").write_text(json.dumps(m))
    assert "0.0.1" in l1.rebuild_due(tmp_path, V)


def test_incremental_appends_only_what_starts_after_the_cursor(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    b.heartbeat(end + 60)
    added = l1.incremental(tmp_path, V)          # no manifest: a rebuild
    assert [e["start"] for e in added["trip"]] == [at(60)]
    assert l1.incremental(tmp_path, V)["trip"] == []   # nothing new
    end2 = b.drive(end + 90, odo_start=1022)
    assert l1.incremental(tmp_path, V)["trip"] == []   # second trip still open
    b.heartbeat(end2 + 60)
    added = l1.incremental(tmp_path, V)
    assert [e["start"] for e in added["trip"]] == [at(end + 90)]
    events = read_lines(tmp_path, "trip")
    assert [e["distance_km"] for e in events] == [22, 22]     # the second from its cursor, with its start value seeded
    assert l1.read_manifest(tmp_path, V)["through"]["trip"] == events[-1]["end"]


def test_incremental_line_by_line_equals_one_rebuild(tmp_path, capsys):
    """The determinism test of ADR-0009, point 6: a stream replayed line by
    line with the incremental derivation after every line yields the same
    trips.jsonl as one rebuild of the whole stream."""
    live = tmp_path / "live"
    b = Builder(live, capsys)
    end = b.drive(60)
    b.heartbeat(end + 60)
    end2 = b.drive(end + 95, odo_start=1022)
    b.heartbeat(end2 + 45)
    end3 = b.drive(end2 + 50, odo_start=1044)   # 50 min after: a third trip
    b.heartbeat(end3 + 61)

    replay = tmp_path / "replay"
    src = live / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst = replay / "vehicle-a7c1" / "l0" / "2026-10.jsonl"
    dst.parent.mkdir(parents=True)
    with open(dst, "a") as f:
        for line in src.read_text().splitlines(keepends=True):
            f.write(line)
            f.flush()
            l1.incremental(replay, V)

    l1.rebuild(live, V)
    batch = (l1.l1_dir(live, V) / "trips.jsonl").read_bytes()
    incremental = (l1.l1_dir(replay, V) / "trips.jsonl").read_bytes()
    assert batch == incremental
    assert len(read_lines(live, "trip")) == 3


def test_the_verbs(tmp_path, capsys):
    b = Builder(tmp_path, capsys)
    end = b.drive(60)
    b.heartbeat(end + 60)
    assert main(["l1", "status", *b.b]) == 1
    assert "no L1" in capsys.readouterr().out
    assert main(["derive", "trips", *b.b, "--write"]) == 0
    assert "1 completed trip" in capsys.readouterr().out
    assert main(["derive", "trips", *b.b, "--write", "--since", at(0)]) == 2
    assert main(["derive", "all", *b.b]) == 2                   # refuses without --write
    assert main(["derive", "all", *b.b, "--write"]) == 0
    assert main(["l1", "status", *b.b]) == 0
    assert "current" in capsys.readouterr().out
    assert main(["l1", "read", *b.b, "--kind", "trip"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out.splitlines()[0])["distance_km"] == 22 and err.strip() == "1 event(s)"
    assert main(["l1", "clean", *b.b]) == 0
    assert not l1.l1_dir(tmp_path, V).exists()
    shutil.rmtree(tmp_path / "vehicle-a7c1")


def test_a_cursor_is_seeded_from_a_snapshot_too(tmp_path, capsys):
    """ISSUE-0011: a role whose last value before the cursor came from a
    start line's snapshot still seeds the read from there."""
    b = Builder(tmp_path, capsys)
    b.heartbeat(0).state(2, "soc", 79, "%")
    s = series.load(tmp_path, V, since=at(5))
    assert [(f.t, f.latitude) for f in s.fixes] == [(at(-120), 51.0)]   # the snapshot's, never changed
    assert [x.value for x in s.series["soc"]] == [79]                     # a later state line beats it
    assert s.series["odometer"][0].value == 1000


def test_a_charge_points_l1_is_a_manifest_and_nothing_else(tmp_path, capsys):
    """ADR-0009, 1: no event of its own in v1 (ISSUE-0010)."""
    b = ["--base", str(tmp_path), "--chargepoint", "cp1"]
    assert main(["l0", "start", *b, "--t", at(0), "--homeassistant", "2026.9.4", "--snapshot", "[]"]) == 0
    assert main(["derive", "all", *b, "--write"]) == 0
    cp = Subject("chargepoint", "cp1")
    assert [p.name for p in l1.l1_dir(tmp_path, cp).iterdir()] == ["manifest.json"]
    assert l1.incremental(tmp_path, cp) == {}
    assert [p.name for p in l1.l1_dir(tmp_path, cp).iterdir()] == ["manifest.json"]
