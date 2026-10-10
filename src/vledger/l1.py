# SPDX-License-Identifier: BSD-3-Clause
"""L1 on disk (ADR-0009): one JSON Lines file per kind of event, a
manifest saying what it was derived from, the cursor at the last completed
event, and rebuilds that swap the directory whole.

Everything here is regenerable from L0, receipts and configuration; nothing
here is a source.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

from vledger import __version__, clock, l0, layout, receipts
from vledger import config as vconfig
from vledger.layout import Subject

#: Event kind -> the file that holds it.
FILES = {
    "trip": "trips.jsonl",
    "charging": "charging-sessions.jsonl",
    "refuelling": "refuellings.jsonl",
    "period": "periods.jsonl",
}
MANIFEST = "manifest.json"

#: The derivations that exist, by kind: a function of (base, subject,
#: since) returning completed events as dicts, in order of start. Filled
#: in by the modules that derive, so this one knows no derivation itself.
DERIVATIONS: dict[str, Callable[[Path, Subject, str | None], list[dict]]] = {}

#: The periods (ADR-0014, point 6): a function of (base, subject, events by
#: kind) returning every line of periods.jsonl. Not an event kind — no
#: cursor, no append — so it is called last, by both writers, rather than
#: registered among the derivations. Set by vledger.periods.
PERIODS: Callable[[Path, Subject, dict[str, list[dict]]], list[dict]] | None = None


def l1_dir(base: Path, subject: Subject) -> Path:
    return layout.subject_dir(base, subject) / "l1"


def path_of(base: Path, subject: Subject, kind: str) -> Path:
    return l1_dir(base, subject) / FILES[kind]


# --- what L1 was derived from ------------------------------------------

def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def config_hash(base: Path, subject: Subject) -> str | None:
    """The hash of the latest config line's object, or None without one."""
    latest = None
    for r in l0.read(base, subject, kind="config"):
        latest = r.line["config"]
    if latest is None:
        return None
    return _sha(json.dumps(latest, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def receipts_hash(base: Path, subject: Subject) -> str:
    p = layout.receipts_file(base, subject)
    return _sha(p.read_bytes() if p.is_file() else b"")


def l0_through(base: Path, subject: Subject) -> str | None:
    last = None
    for r in l0.read(base, subject):
        last = r.line["t"]
    return last


# --- the manifest ------------------------------------------------------

def read_manifest(base: Path, subject: Subject) -> dict | None:
    p = l1_dir(base, subject) / MANIFEST
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_manifest(base: Path, subject: Subject, through: dict[str, str]) -> dict:
    manifest = {
        "vledger": __version__,
        "derived_at": clock.to_text(clock.now()),
        "config": config_hash(base, subject),
        "receipts": receipts_hash(base, subject),
        "through": dict(sorted(through.items())),
        "l0_through": l0_through(base, subject),
    }
    d = l1_dir(base, subject)
    d.mkdir(parents=True, exist_ok=True)
    _write_atomic(d / MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def rebuild_due(base: Path, subject: Subject) -> str | None:
    """Why L1 has to be rebuilt from scratch (ABL-08), or None if it is current."""
    m = read_manifest(base, subject)
    if m is None:
        return "no manifest"
    if m.get("vledger") != __version__:
        return f"derived by vledger {m.get('vledger')}, this is {__version__}"
    if m.get("config") != config_hash(base, subject):
        return "the configuration changed"
    if m.get("receipts") != receipts_hash(base, subject):
        return "the receipts changed"
    return None


# --- reading and writing events ----------------------------------------

def read(base: Path, subject: Subject, kind: str) -> Iterator[dict]:
    p = path_of(base, subject, kind)
    if not p.is_file():
        return
    for number, text, torn in l0._raw_lines(p):
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            if torn:
                return
            raise ValueError(f"{p}:{number}: not JSON and not the last line") from None


#: How far back :func:`last` reads at a time.
_TAIL_CHUNK = 64 * 1024


def last(base: Path, subject: Subject, kind: str, n: int = 1) -> list[dict]:
    """The last ``n`` events of a kind, in order of start, read from the
    end of the file rather than the whole of it (ADR-0016, point 8). A
    torn last line is skipped, as :func:`read` skips it."""
    p = path_of(base, subject, kind)
    if n < 1 or not p.is_file():
        return []
    with open(p, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        data, pos = b"", size
        # One line more than asked for: the first one in the buffer may be
        # cut off by the chunk's start, and a torn tail does not count.
        while pos > 0 and data.count(b"\n") <= n:
            step = min(_TAIL_CHUNK, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    parts = data.decode("utf-8", errors="replace").split("\n")
    if pos > 0:
        parts = parts[1:]
    torn_tail = parts[-1] != ""
    if not torn_tail:
        parts.pop()
    out: list[dict] = []
    for i, text in enumerate(parts):
        try:
            out.append(json.loads(text))
        except json.JSONDecodeError:
            if torn_tail and i == len(parts) - 1:
                break
            raise ValueError(f"{p}: a line before the last is not JSON") from None
    return out[-n:]


#: The lines of periods.jsonl that are current (ADR-0018, point 1).
CURRENT = ("month", "year", "rolling", "lifetime")


def current_periods(base: Path, subject: Subject) -> dict[str, dict | None]:
    """The current month, year, rolling and lifetime lines of
    ``periods.jsonl``: per period, its last line — the one holding the
    stream's last line, judged by the stream, never the clock — or None
    without one (ADR-0018, point 1). The file is a few hundred lines a
    decade, so it is read whole."""
    out: dict[str, dict | None] = dict.fromkeys(CURRENT)
    for line in read(base, subject, "period"):
        if line.get("period") in out:
            out[line["period"]] = line
    return out


#: The confirmations that wait for a person (ADR-0016, point 4).
WAITING = ("unconfirmed", "ambiguous")


def waiting(base: Path, subject: Subject) -> dict[str, dict[str, int]]:
    """Per event kind that takes a receipt, how many events L1 holds that
    wait for one: ``{"refuelling": {"unconfirmed": 2, "ambiguous": 0}, …}``."""
    out = {}
    for kind in receipts.EVENT_KINDS:
        counts = dict.fromkeys(WAITING, 0)
        for e in read(base, subject, kind):
            if e.get("confirmation") in counts:
                counts[e["confirmation"]] += 1
        out[kind] = counts
    return out


def encode(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


def append(base: Path, subject: Subject, event: dict) -> Path:
    """One completed event, whole, flushed and synced — like an L0 line."""
    p = path_of(base, subject, event["kind"])
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(encode(event) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return p


def write_kind(base: Path, subject: Subject, kind: str, events: list[dict]) -> Path:
    """Replace one kind's file whole, atomically."""
    p = path_of(base, subject, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(p, "".join(encode(e) + "\n" for e in events))
    return p


def through_of(events: list[dict], previous: str | None = None) -> str | None:
    """The cursor after these events: the latest end of a detected one."""
    best = previous
    for e in events:
        if receipts.is_own(e):
            continue    # the cursor follows the stream, not what a person typed
        if best is None or clock.parse(e["end"]) > clock.parse(best):
            best = e["end"]
    return best


# --- derivation with receipts (ADR-0013) ---------------------------------

def thresholds(base: Path, subject: Subject) -> dict:
    """The thresholds of the latest config line, over the defaults."""
    thr = dict(vconfig.DEFAULT_THRESHOLDS)
    latest = None
    for r in l0.read(base, subject, kind="config"):
        latest = r.line["config"]
    thr.update((latest or {}).get("thresholds") or {})
    return thr


def kinds(base: Path, subject: Subject) -> list[str]:
    """The kinds L1 holds: every derivation, and a receipt kind whose
    receipts exist even before its derivation does — a receipt that meets
    nothing is an event of its own (BEL-06). A charge point's L1 is a
    manifest and nothing else in v1 (ADR-0009, 1): its meter and cost are
    derived on the vehicle's side (ISSUE-0010)."""
    if subject.kind != "vehicle":
        return []
    out = list(DERIVATIONS)
    led = None
    for kind in receipts.EVENT_KINDS:
        if kind not in out:
            led = led or receipts.ledger(base, subject)
            if led.current(kind):
                out.append(kind)
    return out


def detected(base: Path, subject: Subject, kind: str, since: str | None = None) -> list[dict]:
    """One kind's events as its derivation detects them, before any receipt."""
    d = DERIVATIONS.get(kind)
    return d(base, subject, since) if d else []


def derive(base: Path, subject: Subject, kind: str) -> list[dict]:
    """One kind's events as L1 holds them: for refuellings and charging
    sessions, matched against the current receipts (ADR-0013, 4)."""
    events = detected(base, subject, kind)
    if kind not in receipts.EVENT_KINDS:
        return events
    thr = thresholds(base, subject)
    return receipts.apply(kind, events, receipts.ledger(base, subject).current(kind),
                          tolerance_s=thr["matching_tolerance_s"],
                          plausibility_pct=thr["plausibility_pct"])


def _disturbs(base: Path, subject: Subject, kind: str, new: list[dict]) -> bool:
    """Whether a new event could change a match already on disk: a current
    receipt anchored no earlier than the tolerance before it. Matching it
    alone would then differ from a batch run, so the caller rebuilds."""
    if kind not in receipts.EVENT_KINDS or not new:
        return False
    tol = thresholds(base, subject)["matching_tolerance_s"]
    first = min(clock.parse(e["start"]) for e in new)
    return any((first - clock.parse(r["anchor"])).total_seconds() <= tol
               for r in receipts.ledger(base, subject).current(kind))


# --- rebuild and incremental -------------------------------------------

def rebuild(base: Path, subject: Subject) -> dict:
    """Derive everything into l1.tmp/, then swap it into place (ADR-0009, 2).

    A reader never sees a half-built L1; a crash mid-way leaves the old one.
    """
    final = l1_dir(base, subject)
    tmp = final.with_name("l1.tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    through: dict[str, str] = {}
    derived: dict[str, list[dict]] = {}
    for kind in kinds(base, subject):
        events = derived[kind] = derive(base, subject, kind)
        (tmp / FILES[kind]).write_text("".join(encode(e) + "\n" for e in events), encoding="utf-8")
        t = through_of(events)
        if t:
            through[kind] = t
    lines = _periods(base, subject, derived)
    if lines is not None:
        (tmp / FILES["period"]).write_text("".join(encode(e) + "\n" for e in lines), encoding="utf-8")
    # The manifest goes into the temporary directory too, so the swap is one act.
    manifest = {
        "vledger": __version__,
        "derived_at": clock.to_text(clock.now()),
        "config": config_hash(base, subject),
        "receipts": receipts_hash(base, subject),
        "through": dict(sorted(through.items())),
        "l0_through": l0_through(base, subject),
    }
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    old = final.with_name("l1.old")
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        os.rename(final, old)
    os.rename(tmp, final)
    if old.exists():
        shutil.rmtree(old)
    return manifest


def incremental(base: Path, subject: Subject) -> dict[str, list[dict]]:
    """What the live path does: rebuild if due, else derive from each
    kind's cursor and append only the events that start after it
    (ADR-0009, 3). Returns the events appended, per kind."""
    if rebuild_due(base, subject):
        return _rebuild_and_diff(base, subject)
    manifest = read_manifest(base, subject) or {}
    through = dict(manifest.get("through") or {})
    found: dict[str, list[dict]] = {}
    for kind in (k for k in kinds(base, subject) if k in DERIVATIONS):
        cursor = through.get(kind)
        found[kind] = [e for e in detected(base, subject, kind, cursor)
                       if cursor is None or clock.parse(e["start"]) > clock.parse(cursor)]
        if _disturbs(base, subject, kind, found[kind]):
            return _rebuild_and_diff(base, subject)
    added: dict[str, list[dict]] = {}
    for kind, new in found.items():
        cursor = through.get(kind)
        if kind in receipts.EVENT_KINDS:
            # No receipt is near enough to meet these (_disturbs said so).
            new = [receipts.apply(kind, [e], [], tolerance_s=0, plausibility_pct=0)[0]
                   for e in new]
        for e in new:
            append(base, subject, e)
        added[kind] = new
        t = through_of(new, cursor)
        if t:
            through[kind] = t
    # Every run, appended or not: the rolling period moves with L0.
    write_periods(base, subject)
    write_manifest(base, subject, through)
    return added


def _periods(base: Path, subject: Subject, events: dict[str, list[dict]]) -> list[dict] | None:
    """The lines of periods.jsonl, or None where there is no such file — a
    charge point's L1 holds none (ADR-0009, 1)."""
    if PERIODS is None or subject.kind != "vehicle":
        return None
    return PERIODS(base, subject, events)


def write_periods(base: Path, subject: Subject) -> Path | None:
    """Rewrite periods.jsonl whole, atomically, from the event files as
    they are on disk (ADR-0009, 2; ADR-0014, 6)."""
    lines = _periods(base, subject, {k: list(read(base, subject, k)) for k in kinds(base, subject)})
    return None if lines is None else write_kind(base, subject, "period", lines)


def _rebuild_and_diff(base: Path, subject: Subject) -> dict[str, list[dict]]:
    before = {k: list(read(base, subject, k)) for k in FILES}
    rebuild(base, subject)
    return {k: [e for e in read(base, subject, k) if e not in before.get(k, [])]
            for k in kinds(base, subject)}


def clean(base: Path, subject: Subject) -> bool:
    """Delete L1 — it is regenerable (ABL-05)."""
    d = l1_dir(base, subject)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True
