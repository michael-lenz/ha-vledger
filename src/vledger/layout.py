# SPDX-License-Identifier: BSD-3-Clause
"""Where things live on disk (ADR-0004, point 3).

::

    <base>/
      vehicle-<subject>/
        l0/2026-10.jsonl      one file per UTC month, by the line's own t
        receipts.jsonl        append-only, apart from L0
        l1/…                  the derivation, regenerable
      chargepoint-<subject>/
        l0/2026-10.jsonl
        l1/…

A subject is a UUID the integration minted; this module only knows how to
spell its directory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

KINDS = ("vehicle", "chargepoint")
_DIR = re.compile(r"^(vehicle|chargepoint)-(.+)$")
_MONTH_FILE = re.compile(r"^(\d{4}-\d{2})\.jsonl$")


@dataclass(frozen=True)
class Subject:
    """A vehicle or a charge point: what a stream belongs to."""

    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"subject kind must be one of {KINDS}, not {self.kind!r}")
        if not self.id or "/" in self.id:
            raise ValueError(f"not a subject id: {self.id!r}")

    @property
    def dirname(self) -> str:
        return f"{self.kind}-{self.id}"

    @classmethod
    def from_dirname(cls, name: str) -> Subject:
        m = _DIR.match(name)
        if not m:
            raise ValueError(f"not a subject directory: {name!r}")
        return cls(m.group(1), m.group(2))


def subject_dir(base: Path, subject: Subject) -> Path:
    return base / subject.dirname


def l0_dir(base: Path, subject: Subject) -> Path:
    return subject_dir(base, subject) / "l0"


def l0_file(base: Path, subject: Subject, month: str) -> Path:
    """The month file a line of time ``month`` (``YYYY-MM``) goes to."""
    return l0_dir(base, subject) / f"{month}.jsonl"


def l0_files(base: Path, subject: Subject) -> list[Path]:
    """Every month file of a stream, oldest first; nothing else in the directory."""
    d = l0_dir(base, subject)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if _MONTH_FILE.match(p.name))


def receipts_file(base: Path, subject: Subject) -> Path:
    return subject_dir(base, subject) / "receipts.jsonl"


def subjects(base: Path) -> list[Subject]:
    """Every subject under ``base``, by directory name."""
    if not base.is_dir():
        return []
    found = []
    for p in sorted(base.iterdir()):
        if p.is_dir() and _DIR.match(p.name):
            found.append(Subject.from_dirname(p.name))
    return found
