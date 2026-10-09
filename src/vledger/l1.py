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
    nothing is an event of its own (BEL-06)."""
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
    for kind in kinds(base, subject):
        events = derive(base, subject, kind)
        (tmp / FILES[kind]).write_text("".join(encode(e) + "\n" for e in events), encoding="utf-8")
        t = through_of(events)
        if t:
            through[kind] = t
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
    for kind in DERIVATIONS:
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
    write_manifest(base, subject, through)
    return added


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
