# SPDX-License-Identifier: BSD-3-Clause
"""The ``vledger`` command line.

Every operation the library offers is a verb here, grouped by noun
(ADR-0005): ``vledger l0 …`` for the raw log. A verb reads and writes the
directory layout under ``--base`` and prints what it did. The library does
the work; this module parses arguments and spells results.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vledger import __version__, clock, l0
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
    p = Path(text)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
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
                    unit=args.unit, attrs=_kv(args.attr), measured_at=args.measured_at)
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


# --- the parser ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vledger", description="The vehicle ledger")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    nouns = parser.add_subparsers(dest="noun", metavar="<noun>", required=True)

    p_l0 = nouns.add_parser("l0", help="the raw log: write, read, validate, gaps")
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
