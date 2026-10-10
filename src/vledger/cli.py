# SPDX-License-Identifier: BSD-3-Clause
"""The ``vledger`` command line.

Every operation the library offers is a verb here, grouped by noun
(ADR-0005): ``vledger l0 …`` for the raw log, ``vledger receipt …`` for
what a person states about a refuelling or a charge. A verb reads and writes the
directory layout under ``--base`` and prints what it did. The library does
the work; this module parses arguments and spells results.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vledger import (
    __version__,
    anonymise,
    charging,
    clock,
    export,
    geo,
    l0,
    l1,
    periods,
    receipts,
    refuellings,
    report,
    series,
    stats,
    trips,
    units,
)
from vledger.layout import Subject

ENV_BASE = "VLEDGER_BASE"


class Usage(Exception):
    """A complaint about the arguments, printed without a traceback."""


# --- shared pieces ---------------------------------------------------------

def _subject(args) -> Subject:
    if args.vehicle and args.chargepoint:
        raise Usage("--vehicle and --chargepoint exclude each other")
    if args.vehicle:
        return Subject("vehicle", args.vehicle)
    if args.chargepoint:
        return Subject("chargepoint", args.chargepoint)
    raise Usage("name the stream: --vehicle ID or --chargepoint ID")


def _base(args) -> Path:
    return Path(args.base or os.environ.get(ENV_BASE) or ".")


def _t(args) -> str:
    return args.t or clock.to_text(clock.now())


def _json_arg(text: str):
    """JSON from the argument itself, from a file, or from stdin (``-``)."""
    if text == "-":
        return json.load(sys.stdin)
    try:
        is_file = Path(text).is_file()
    except (OSError, ValueError):   # a long JSON string is not a path
        is_file = False
    if is_file:
        return json.loads(Path(text).read_text(encoding="utf-8"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise Usage(f"neither JSON, a file nor '-': {text!r}") from None


def _kv(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise Usage(f"expected key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _add_stream_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--base", help=f"the data directory (default: ${ENV_BASE} or .)")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--vehicle", metavar="ID", help="the vehicle's subject id")
    g.add_argument("--chargepoint", metavar="ID", help="the charge point's subject id")


def _add_t(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--t", help="the event's time, UTC ISO 8601 (default: now)")


# --- l0 verbs --------------------------------------------------------------

def cmd_l0_state(args) -> int:
    subject = _subject(args)
    line = l0.state(_t(args), subject, args.role, args.entity, args.state,
                    unit=args.unit, attrs=_kv(args.attr), measured_at=args.measured_at,
                    reported_before=args.reported_before)
    path = l0.append(_base(args), subject, line)
    print(f"{path.name}: {l0.encode(line)}")
    return 0


def cmd_l0_start(args) -> int:
    subject = _subject(args)
    snapshot = _json_arg(args.snapshot) if args.snapshot else []
    line = l0.start(_t(args), subject, vledger=args.vledger or __version__,
                    homeassistant=args.homeassistant, snapshot=snapshot)
    path = l0.append(_base(args), subject, line)
    print(f"{path.name}: start, {len(snapshot)} role(s) in the snapshot")
    return 0


def cmd_l0_stop(args) -> int:
    subject = _subject(args)
    line = l0.stop(_t(args), subject, reason=args.reason)
    path = l0.append(_base(args), subject, line)
    print(f"{path.name}: stop ({args.reason})")
    return 0


def cmd_l0_heartbeat(args) -> int:
    subject = _subject(args)
    line = l0.heartbeat(_t(args), subject, lines=args.lines)
    path = l0.append(_base(args), subject, line)
    print(f"{path.name}: heartbeat, {args.lines} line(s) since start")
    return 0


def cmd_l0_config(args) -> int:
    subject = _subject(args)
    line = l0.config(_t(args), subject, config=_json_arg(args.config))
    path = l0.append(_base(args), subject, line)
    print(f"{path.name}: config, {len(line['config'])} key(s)")
    return 0


def cmd_l0_read(args) -> int:
    n = 0
    for r in l0.read(_base(args), _subject(args), kind=args.kind, role=args.role,
                     since=args.since, until=args.until):
        print(l0.encode(r.line))
        n += 1
    print(f"{n} line(s)", file=sys.stderr)
    return 0


def cmd_l0_validate(args) -> int:
    report = l0.validate(_base(args), _subject(args))
    if args.json:
        print(json.dumps({
            "files": report.files, "lines": report.lines, "by_kind": report.by_kind,
            "versions": sorted(report.versions),
            "problems": [p.__dict__ for p in report.problems],
        }, indent=2))
    else:
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(report.by_kind.items()))
        versions = ", ".join(str(v) for v in sorted(report.versions)) or "none"
        print(f"{report.files} file(s), {report.lines} line(s): {kinds or 'nothing'}")
        print(f"schema version(s): {versions}")
        for p in report.problems:
            print(f"  [{p.severity}] {p.where}: {p.what}")
        print(f"{report.errors} error(s), {len(report.problems) - report.errors} warning(s)")
    return 1 if report.errors else 0


def cmd_l0_gaps(args) -> int:
    found = l0.gaps(_base(args), _subject(args), tolerance_s=args.tolerance,
                    now=args.now, min_s=args.min)
    if args.json:
        print(json.dumps([g.__dict__ for g in found], indent=2))
    else:
        for g in found:
            print(f"{g.start}  {g.end}  {g.seconds:10.0f} s  {g.reason}")
        print(f"{len(found)} gap(s)", file=sys.stderr)
    return 0


def cmd_l0_stats(args) -> int:
    s = stats.scan(_base(args), _subject(args), since=args.since, tolerance_s=args.tolerance)
    s.finder.close(args.now or clock.to_text(clock.now()))
    d = stats.to_dict(s)
    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(s.by_kind.items())) or "nothing"
    print(f"{len(s.files)} month file(s), {s.bytes} bytes; current {s.current_month or '-'}: "
          f"{s.files.get(s.current_month, 0) if s.current_month else 0} bytes")
    print(f"lines: {kinds}")
    print(f"state lines since the last start: {s.lines_since_start}"
          + (f", since {args.since}: {s.lines_since}" if args.since else ""))
    print(f"last line at {s.last_line_at or '-'}, last heartbeat at {s.last_heartbeat_at or '-'}")
    def summary(name: str, i: stats.Intervals | None) -> str:
        if i is None:
            return f"no {name} interval measured"
        return f"{name} median {i.median_s:.0f} s, p95 {i.p95_s:.0f} s over {i.count}"

    for role in sorted(s.by_role):
        print(f"  {role}: {s.by_role[role]} line(s), last at {s.last_states[role]['t']}; "
              f"{summary('sampling', s.sampling.get(role))}; "
              f"{summary('change', s.changes.get(role))}")
    for role, values in sorted(s.unlisted.items()):
        print(f"  {role}: met and not in the map: {', '.join(sorted(values))}")
    latest = s.gaps[-1] if s.gaps else None
    print(f"{len(s.gaps)} gap(s)" + (f", latest {latest.reason} of {latest.seconds:.0f} s "
                                      f"from {latest.start}" if latest else ""))
    return 0


def cmd_l0_anonymise(args) -> int:
    try:
        dlat, dlon = (float(x) for x in args.shift.split(","))
    except ValueError:
        raise Usage(f"--shift is LAT,LON in degrees, not {args.shift!r}") from None
    only = _subject(args) if args.vehicle or args.chargepoint else None
    counts = anonymise.copy(_base(args), Path(args.to), dlat, dlon, only=only)
    for subject, n in counts.items():
        print(f"{subject.dirname}: {n} line(s)")
    return 0


# --- derive verbs ----------------------------------------------------------

def _write_kind(args, kind: str, noun: str) -> int:
    """``derive … --write``: replace one kind's file with every completed
    event, and move that kind's cursor."""
    if args.since or args.until:
        raise Usage("--write replaces the whole file; it takes no --since or --until")
    base, subject = _base(args), _subject(args)
    events = l1.derive(base, subject, kind)
    path = l1.write_kind(base, subject, kind, events)
    manifest = l1.read_manifest(base, subject) or {}
    through = dict(manifest.get("through") or {})
    t = l1.through_of(events)
    if t:
        through[kind] = t
    l1.write_manifest(base, subject, through)
    print(f"{path.name}: {len(events)} completed {noun}(s)")
    return 0


def cmd_derive_trips(args) -> int:
    if args.write:
        return _write_kind(args, "trip", "trip")
    found = trips.derive_from(_base(args), _subject(args), since=args.since, until=args.until)
    for trip in found:
        print(json.dumps(trips.to_dict(trip), ensure_ascii=False))
    print(f"{len(found)} trip(s)", file=sys.stderr)
    return 0


def cmd_derive_refuellings(args) -> int:
    base, subject = _base(args), _subject(args)
    s = series.load(base, subject, since=args.since, until=args.until)
    reason = refuellings.cannot_detect(s)
    if reason:
        print(f"no refuelling detection: {reason}", file=sys.stderr)
    if args.write:
        return _write_kind(args, "refuelling", "refuelling")
    found = refuellings.derive(s)
    for r in found:
        print(json.dumps(refuellings.to_dict(r), ensure_ascii=False))
    print(f"{len(found)} refuelling candidate(s)", file=sys.stderr)
    return 0


def cmd_derive_charging(args) -> int:
    base, subject = _base(args), _subject(args)
    if subject.kind != "vehicle":
        raise Usage("charging sessions are a vehicle's; a charge point's meter is read from there")
    if args.write:
        return _write_kind(args, "charging", "charging session")
    found = charging.derive_from(base, subject, since=args.since, until=args.until)
    for session in found:
        print(json.dumps(charging.to_dict(session), ensure_ascii=False))
    print(f"{len(found)} charging session(s)", file=sys.stderr)
    return 0


def cmd_derive_periods(args) -> int:
    base, subject = _base(args), _subject(args)
    if subject.kind != "vehicle":
        raise Usage("periods are a vehicle's; a charge point's L1 holds none")
    if args.write:
        if l1.read_manifest(base, subject) is None:
            raise Usage("no L1 to compute periods from: derive all --write first")
        path = l1.write_periods(base, subject)
        print(f"{path.name}: {sum(1 for _ in l1.read(base, subject, 'period'))} period(s), "
              f"from the events in {l1.l1_dir(base, subject)}")
        return 0
    found = periods.derive_from(base, subject)
    for line in found:
        print(json.dumps(line, ensure_ascii=False))
    print(f"{len(found)} period(s)", file=sys.stderr)
    return 0


def cmd_derive_all(args) -> int:
    base, subject = _base(args), _subject(args)
    if not args.write:
        raise Usage("derive all rebuilds L1 on disk; say --write")
    manifest = l1.rebuild(base, subject)
    for kind, t in manifest["through"].items():
        print(f"{kind}: through {t}")
    print(f"rebuilt {l1.l1_dir(base, subject)}")
    return 0


# --- receipt verbs ---------------------------------------------------------

def _vehicle(args) -> Subject:
    subject = _subject(args)
    if subject.kind != "vehicle":
        raise Usage("receipts belong to a vehicle: name it with --vehicle")
    return subject


def _anchor(args, base: Path, subject: Subject, kind: str) -> tuple[str, bool]:
    """The anchor time, and whether it was taken from a candidate (BEL-04)."""
    return receipts.anchor_of(kind, anchor=args.anchor, from_candidate=args.from_candidate,
                              detected=lambda: l1.detected(base, subject, kind))


def cmd_receipt_add_refuelling(args) -> int:
    base, subject = _base(args), _vehicle(args)
    anchor, exact = _anchor(args, base, subject, "refuelling")
    line = receipts.refuelling(
        _t(args), subject, anchor=anchor, exact=exact, quantity_l=args.quantity_l,
        full=args.full, total_price=args.total_price, unit_price=args.unit_price,
        place=args.place, fuel=args.fuel, note=args.note, replaces=args.replaces)
    receipts.append(base, subject, line)
    print(l0.encode(line))
    return 0


def cmd_receipt_add_charging(args) -> int:
    base, subject = _base(args), _vehicle(args)
    anchor, exact = _anchor(args, base, subject, "charging")
    line = receipts.charging(
        _t(args), subject, anchor=anchor, exact=exact, energy_kwh=args.energy_kwh,
        total_price=args.total_price, place=args.place, provider=args.provider,
        note=args.note, replaces=args.replaces)
    receipts.append(base, subject, line)
    print(l0.encode(line))
    return 0


def cmd_receipt_cancel(args) -> int:
    base, subject = _base(args), _vehicle(args)
    line = receipts.cancel(_t(args), subject, cancels=args.id, note=args.note)
    receipts.append(base, subject, line)
    print(l0.encode(line))
    return 0


def cmd_receipt_list(args) -> int:
    base, subject = _base(args), _vehicle(args)
    if args.all:
        lines = [r.line for r in receipts.read(base, subject)]
    else:
        lines = receipts.ledger(base, subject).current(args.kind)
    lines = [ln for ln in lines if args.kind is None or ln["kind"] == args.kind]
    for line in lines:
        print(l0.encode(line))
    print(f"{len(lines)} receipt(s)", file=sys.stderr)
    return 0


def cmd_derive_match(args) -> int:
    base, subject = _base(args), _vehicle(args)
    rows = receipts.pairing(
        receipts.ledger(base, subject).current(),
        {k: l1.detected(base, subject, k) for k in receipts.EVENT_KINDS},
        l1.thresholds(base, subject)["matching_tolerance_s"])
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    counts = {m: sum(r["match"] == m for r in rows) for m in ("event", "own", "ambiguous")}
    print(f"{len(rows)} receipt(s): {counts['event']} met an event, {counts['own']} "
          f"stand alone, {counts['ambiguous']} ambiguous", file=sys.stderr)
    return 0


# --- l1 verbs --------------------------------------------------------------

def cmd_l1_read(args) -> int:
    base, subject = _base(args), _subject(args)
    if args.last is not None:
        if args.last < 1:
            raise Usage("--last takes a number of events, at least 1")
        events = l1.last(base, subject, args.kind, args.last)
    else:
        events = l1.read(base, subject, args.kind)
    n = 0
    for event in events:
        print(l1.encode(event))
        n += 1
    print(f"{n} event(s)", file=sys.stderr)
    return 0


def cmd_l1_status(args) -> int:
    base, subject = _base(args), _subject(args)
    manifest = l1.read_manifest(base, subject)
    if manifest is None:
        print("no L1")
    else:
        print(f"derived by vledger {manifest['vledger']} at {manifest['derived_at']}")
        for kind, t in (manifest.get("through") or {}).items():
            print(f"  {kind}: through {t}")
        print(f"  L0 read through {manifest.get('l0_through')}")
        if subject.kind == "vehicle":
            for kind, counts in l1.waiting(base, subject).items():
                print(f"  {kind}: waiting for a receipt: " + ", ".join(
                    f"{n} {state}" for state, n in counts.items()))
    due = l1.rebuild_due(base, subject)
    print(f"rebuild due: {due}" if due else "current")
    return 1 if due else 0


def cmd_l1_clean(args) -> int:
    gone = l1.clean(_base(args), _subject(args))
    print("deleted" if gone else "nothing to delete")
    return 0


# --- export and report verbs -----------------------------------------------

def _l1_as_it_is(base: Path, subject: Subject, verb: str) -> None:
    """Exports and reports render L1 as it is and derive nothing: refused
    without one, a note on stderr when it is not current."""
    if l1.read_manifest(base, subject) is None:
        raise Usage(f"no L1 to {verb}: derive all --write first")
    due = l1.rebuild_due(base, subject)
    if due:
        print(f"vledger: L1 is not current ({due}); derive all --write brings it up to date",
              file=sys.stderr)


def _exported(args, kind: str) -> list[dict]:
    """One kind's events as L1 holds them, within --since and --until."""
    base, subject = _base(args), _subject(args)
    _l1_as_it_is(base, subject, "export")
    return export.within(l1.read(base, subject, kind), args.since, args.until)


def _emit(args, text: str, n: int, noun: str) -> int:
    if args.out:
        out = Path(args.out)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)
        print(f"{out}: {n} {noun}(s)")
    else:
        sys.stdout.write(text)
        print(f"{n} {noun}(s)", file=sys.stderr)
    return 0


def cmd_export_csv(args) -> int:
    events = _exported(args, args.kind)
    return _emit(args, export.to_csv(args.kind, events), len(events), "event")


def cmd_export_json(args) -> int:
    events = _exported(args, args.kind)
    return _emit(args, export.to_json(events), len(events), "event")


def cmd_export_gpx(args) -> int:
    found = _exported(args, "trip")
    return _emit(args, export.to_gpx(found), len(found), "trip")


def cmd_report_metrics(args) -> int:
    base, subject = _base(args), _subject(args)
    if subject.kind != "vehicle":
        raise Usage("a report is a vehicle's; a charge point's L1 holds no events")
    _l1_as_it_is(base, subject, "report on")
    r = report.from_l1(base, subject, since=args.since, until=args.until)
    text = json.dumps(r, indent=2, ensure_ascii=False) + "\n" if args.json else report.table(r)
    return _emit(args, text, len(r["intervals"]), "tank-to-tank interval")


# --- calc verbs: the atoms ---------------------------------------------------

def cmd_calc_distance(args) -> int:
    print(f"{geo.distance_km(args.lat1, args.lon1, args.lat2, args.lon2):.3f} km")
    return 0


def cmd_calc_convert(args) -> int:
    value = units.convert(args.value, args.unit, args.quantity)
    print(f"{value:g} {units.normal_unit(args.quantity)}")
    return 0


# --- the parser ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vledger", description="The vehicle ledger")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    nouns = parser.add_subparsers(dest="noun", metavar="<noun>", required=True)

    p_l0 = nouns.add_parser("l0", help="the raw log: write, read, validate, gaps, stats")
    verbs = p_l0.add_subparsers(dest="verb", metavar="<verb>", required=True)

    sp = verbs.add_parser("state", help="append one state change")
    _add_stream_args(sp)
    _add_t(sp)
    sp.add_argument("--role", required=True, choices=l0.ROLES)
    sp.add_argument("--entity", required=True, help="the entity id")
    sp.add_argument("--state", required=True, help="the state string, as Home Assistant holds it")
    sp.add_argument("--unit", help="unit_of_measurement, when the entity has one")
    sp.add_argument("--attr", action="append", metavar="KEY=VALUE",
                    help="an attribute; only the role-relevant ones are kept")
    sp.add_argument("--measured-at", help="the source's own measurement time, if it gives one")
    sp.add_argument("--reported-before",
                    help="when Home Assistant last reported the value this one replaces")
    sp.set_defaults(func=cmd_l0_state)

    sp = verbs.add_parser("start", help="capture begins: write the start marker and snapshot")
    _add_stream_args(sp)
    _add_t(sp)
    sp.add_argument("--vledger", help="library version (default: this one)")
    sp.add_argument("--homeassistant", required=True, help="Home Assistant version")
    sp.add_argument("--snapshot", help="JSON list of {role, entity, state, unit?, since}; "
                                       "inline, a file, or - for stdin")
    sp.set_defaults(func=cmd_l0_start)

    sp = verbs.add_parser("stop", help="capture ends orderly: write the stop marker")
    _add_stream_args(sp)
    _add_t(sp)
    sp.add_argument("--reason", required=True, choices=l0.STOP_REASONS)
    sp.set_defaults(func=cmd_l0_stop)

    sp = verbs.add_parser("heartbeat", help="write a heartbeat")
    _add_stream_args(sp)
    _add_t(sp)
    sp.add_argument("--lines", type=int, required=True, help="state lines since start")
    sp.set_defaults(func=cmd_l0_heartbeat)

    sp = verbs.add_parser("config", help="write the subject's complete configuration")
    _add_stream_args(sp)
    _add_t(sp)
    sp.add_argument("--config", required=True, help="JSON object; inline, a file, or - for stdin")
    sp.set_defaults(func=cmd_l0_config)

    sp = verbs.add_parser("read", help="print a stream as JSON Lines, in order")
    _add_stream_args(sp)
    sp.add_argument("--kind", choices=l0.KINDS)
    sp.add_argument("--role", choices=l0.ROLES)
    sp.add_argument("--since", help="first time to print, inclusive")
    sp.add_argument("--until", help="last time to print, inclusive")
    sp.set_defaults(func=cmd_l0_read)

    sp = verbs.add_parser("validate", help="check a stream against the schema and report")
    _add_stream_args(sp)
    sp.add_argument("--json", action="store_true", help="the report as JSON")
    sp.set_defaults(func=cmd_l0_validate)

    sp = verbs.add_parser("gaps", help="list the capture gaps the markers reveal")
    _add_stream_args(sp)
    sp.add_argument("--tolerance", type=float, default=300,
                    help="seconds a heartbeat may be late (default 300)")
    sp.add_argument("--now", help="the time the stream is judged against (default: now)")
    sp.add_argument("--min", type=float, default=0, help="shortest gap to list, seconds")
    sp.add_argument("--json", action="store_true", help="the gaps as JSON")
    sp.set_defaults(func=cmd_l0_gaps)

    sp = verbs.add_parser("stats", help="count a stream: sizes, lines, last lines, "
                                        "sampling and change intervals, unlisted values, gaps")
    _add_stream_args(sp)
    sp.add_argument("--since", help="also count the state lines at or after this time")
    sp.add_argument("--tolerance", type=float, default=300,
                    help="seconds a heartbeat may be late (default 300)")
    sp.add_argument("--now", help="the time the stream is judged against (default: now)")
    sp.add_argument("--json", action="store_true", help="the counts as JSON")
    sp.set_defaults(func=cmd_l0_stats)

    sp = verbs.add_parser("anonymise", help="copy the streams with positions shifted and "
                                            "names dropped, to become a fixture")
    _add_stream_args(sp)
    sp.add_argument("--shift", required=True, metavar="LAT,LON",
                    help="degrees every position moves by, e.g. 0.1,-2.5")
    sp.add_argument("--to", required=True, metavar="DIR", help="the base to copy into")
    sp.set_defaults(func=cmd_l0_anonymise)

    p_derive = nouns.add_parser("derive", help="the derivations, one at a time")
    dverbs = p_derive.add_subparsers(dest="verb", metavar="<verb>", required=True)
    sp = dverbs.add_parser("trips", help="the trips in a stream, as JSON Lines")
    _add_stream_args(sp)
    sp.add_argument("--since", help="first time to read, inclusive")
    sp.add_argument("--until", help="last time to read, inclusive")
    sp.add_argument("--write", action="store_true", help="replace l1/trips.jsonl instead of printing")
    sp.set_defaults(func=cmd_derive_trips)
    sp = dverbs.add_parser("refuellings", help="the refuelling candidates in a stream, as JSON Lines")
    _add_stream_args(sp)
    sp.add_argument("--since", help="first time to read, inclusive")
    sp.add_argument("--until", help="last time to read, inclusive")
    sp.add_argument("--write", action="store_true",
                    help="replace l1/refuellings.jsonl instead of printing")
    sp.set_defaults(func=cmd_derive_refuellings)
    sp = dverbs.add_parser("charging", help="the charging sessions in a vehicle's stream, as JSON Lines")
    _add_stream_args(sp)
    sp.add_argument("--since", help="first time to read, inclusive")
    sp.add_argument("--until", help="last time to read, inclusive")
    sp.add_argument("--write", action="store_true",
                    help="replace l1/charging-sessions.jsonl instead of printing")
    sp.set_defaults(func=cmd_derive_charging)
    sp = dverbs.add_parser("periods", help="the metrics per month, year, rolling period and "
                                           "lifetime, as JSON Lines")
    _add_stream_args(sp)
    sp.add_argument("--write", action="store_true",
                    help="rewrite l1/periods.jsonl from the events on disk instead of printing")
    sp.set_defaults(func=cmd_derive_periods)
    sp = dverbs.add_parser("all", help="rebuild L1 from scratch, atomically")
    _add_stream_args(sp)
    sp.add_argument("--write", action="store_true", help="required: this writes")
    sp.set_defaults(func=cmd_derive_all)

    sp = dverbs.add_parser("match", help="pair the current receipts with the detected "
                                         "refuellings and charging sessions")
    _add_stream_args(sp)
    sp.set_defaults(func=cmd_derive_match)

    p_receipt = nouns.add_parser("receipt", help="what a person states: add, cancel, list")
    rverbs = p_receipt.add_subparsers(dest="verb", metavar="<verb>", required=True)
    sp = rverbs.add_parser("add", help="append a refuelling or charging receipt")
    akinds = sp.add_subparsers(dest="receipt_kind", metavar="<kind>", required=True)

    def receipt_common(sp: argparse.ArgumentParser) -> None:
        _add_stream_args(sp)
        sp.add_argument("--t", help="the time of entry, UTC ISO 8601 (default: now)")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--anchor", help="when the refuelling or charge happened, as typed")
        g.add_argument("--from-candidate", metavar="START",
                       help="the start of a detected event: anchors there exactly")
        sp.add_argument("--total-price", type=float, help="the amount paid")
        sp.add_argument("--place", help="where, as free text")
        sp.add_argument("--note", help="anything else")
        sp.add_argument("--replaces", metavar="UUID", help="correct this receipt: replaces it whole")

    sp = akinds.add_parser("refuelling", help="litres, a price, full tank or not")
    receipt_common(sp)
    sp.add_argument("--quantity-l", type=float, required=True, help="litres, as on the receipt")
    sp.add_argument("--unit-price", type=float, help="the price per litre")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--full", dest="full", action="store_true", help="the tank was filled")
    g.add_argument("--partial", dest="full", action="store_false", help="it was not")
    sp.add_argument("--fuel", help="the fuel type, as on the receipt")
    sp.set_defaults(func=cmd_receipt_add_refuelling)

    sp = akinds.add_parser("charging", help="billed kWh and the price")
    receipt_common(sp)
    sp.add_argument("--energy-kwh", type=float, required=True, help="the billed energy")
    sp.add_argument("--provider", help="who billed it")
    sp.set_defaults(func=cmd_receipt_add_charging)

    sp = rverbs.add_parser("cancel", help="take a receipt out, with a receipt of its own")
    _add_stream_args(sp)
    sp.add_argument("id", metavar="UUID", help="the receipt to cancel")
    sp.add_argument("--t", help="the time of entry, UTC ISO 8601 (default: now)")
    sp.add_argument("--note", help="why")
    sp.set_defaults(func=cmd_receipt_cancel)

    sp = rverbs.add_parser("list", help="the receipts that count, by anchor time")
    _add_stream_args(sp)
    sp.add_argument("--kind", choices=receipts.KINDS)
    sp.add_argument("--all", action="store_true",
                    help="every line in file order, corrected and cancelled ones too")
    sp.set_defaults(func=cmd_receipt_list)

    p_l1 = nouns.add_parser("l1", help="the derivation on disk: read, status, clean")
    lverbs = p_l1.add_subparsers(dest="verb", metavar="<verb>", required=True)
    sp = lverbs.add_parser("read", help="print one kind's events as JSON Lines")
    _add_stream_args(sp)
    sp.add_argument("--kind", required=True, choices=list(l1.FILES))
    sp.add_argument("--last", type=int, metavar="N",
                    help="only the last N events, read from the end of the file")
    sp.set_defaults(func=cmd_l1_read)
    sp = lverbs.add_parser("status", help="the manifest, and whether a rebuild is due")
    _add_stream_args(sp)
    sp.set_defaults(func=cmd_l1_status)
    sp = lverbs.add_parser("clean", help="delete L1; it is regenerable")
    _add_stream_args(sp)
    sp.set_defaults(func=cmd_l1_clean)

    p_export = nouns.add_parser("export", help="renderings of L1: CSV and JSON of one kind, "
                                               "GPX of the trips")
    everbs = p_export.add_subparsers(dest="verb", metavar="<verb>", required=True)

    def export_common(sp: argparse.ArgumentParser) -> None:
        _add_stream_args(sp)
        sp.add_argument("--since", help="first start to export, inclusive")
        sp.add_argument("--until", help="last start to export, inclusive")
        sp.add_argument("--out", metavar="FILE", help="write the file instead of printing it")

    for name, func, what in (("csv", cmd_export_csv, "one kind's events as CSV, a row each"),
                             ("json", cmd_export_json, "one kind's events as one JSON array")):
        sp = everbs.add_parser(name, help=what)
        export_common(sp)
        sp.add_argument("--kind", required=True, choices=list(l1.FILES))
        sp.set_defaults(func=func)
    sp = everbs.add_parser("gpx", help="the trips as GPX 1.1, a track each from its waypoints")
    export_common(sp)
    sp.set_defaults(func=cmd_export_gpx)

    p_report = nouns.add_parser("report", help="the metrics of a freely chosen span, from L1")
    rpverbs = p_report.add_subparsers(dest="verb", metavar="<verb>", required=True)
    sp = rpverbs.add_parser("metrics", help="a period line's metrics for the span, and the "
                                           "tank-to-tank intervals in it with their mean "
                                           "outside temperature")
    _add_stream_args(sp)
    sp.add_argument("--since", help="the span's start, inclusive (default: the stream's first line)")
    sp.add_argument("--until", help="the span's end, inclusive (default: the stream's last line)")
    sp.add_argument("--json", action="store_true", help="the report as JSON instead of a table")
    sp.add_argument("--out", metavar="FILE", help="write the file instead of printing it")
    sp.set_defaults(func=cmd_report_metrics)

    p_calc = nouns.add_parser("calc", help="the atoms the derivations are built from")
    cverbs = p_calc.add_subparsers(dest="verb", metavar="<verb>", required=True)
    sp = cverbs.add_parser("distance", help="great-circle distance between two positions")
    for name in ("lat1", "lon1", "lat2", "lon2"):
        sp.add_argument(name, type=float)
    sp.set_defaults(func=cmd_calc_distance)
    sp = cverbs.add_parser("convert", help="a value in a source unit as the L1 unit")
    sp.add_argument("value", type=float)
    sp.add_argument("unit", help="the source unit, as Home Assistant spells it")
    sp.add_argument("--quantity", required=True,
                    choices=["distance", "volume", "energy", "percent", "temperature"])
    sp.set_defaults(func=cmd_calc_convert)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (Usage, ValueError, TypeError, l0.TornLine, FileNotFoundError) as e:
        print(f"vledger: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
