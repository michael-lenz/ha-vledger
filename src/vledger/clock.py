# SPDX-License-Identifier: BSD-3-Clause
"""Time as L0 spells it: UTC, ISO 8601, milliseconds, ``Z`` (ADR-0004).

Everything that touches a timestamp goes through here, so there is one
spelling and one parser. The library never asks the clock on its own —
Home Assistant gives the time of every event — except in the CLI, where
``now()`` is the default a human did not type.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC)


def to_text(t: datetime) -> str:
    """``2026-10-09T07:12:03.412Z`` — always UTC, always milliseconds."""
    if t.tzinfo is None:
        raise ValueError("a naive datetime has no place in L0")
    t = t.astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse(text: str) -> datetime:
    """The inverse of :func:`to_text`; tolerant of what ISO 8601 allows."""
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        raise ValueError(f"timestamp without a zone: {text!r}")
    return t.astimezone(UTC)


def month_of(text: str) -> str:
    """``2026-10`` — the month file a line belongs to, by its own time."""
    return to_text(parse(text))[:7]
