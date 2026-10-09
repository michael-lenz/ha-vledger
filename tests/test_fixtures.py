# SPDX-License-Identifier: BSD-3-Clause
"""Real streams as fixtures (QUA-01): every directory under
``tests/fixtures/`` is a data directory made by ``vledger l0 anonymise``,
with the L1 it is expected to yield beside its L0 — the event files and
``periods.jsonl``, no manifest. Each is derived afresh and compared, and
replayed line by line to show the live path writes the same files.

How a stream becomes one is docs/developing.md, *Real streams as fixtures*.
"""

import shutil
from pathlib import Path

import pytest

from vledger import l0, l1, layout
from vledger.layout import Subject

FIXTURES = Path(__file__).parent / "fixtures"


def fixtures() -> list[Path]:
    if not FIXTURES.is_dir():
        return []
    return sorted(p for p in FIXTURES.iterdir() if p.is_dir() and layout.subjects(p))


def vehicles(base: Path) -> list[Subject]:
    return [s for s in layout.subjects(base) if s.kind == "vehicle"]


def comparable(lines) -> list[dict]:
    """The events without the library version that wrote them, which a
    release changes and nothing else does."""
    return [{k: v for k, v in e.items() if k != "version"} for e in lines]


def expected(base: Path, subject: Subject) -> dict[str, list[dict]]:
    return {kind: comparable(l1.read(base, subject, kind)) for kind in l1.FILES
            if l1.path_of(base, subject, kind).is_file()}


def without_l1(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("l1", "l1.*"))
    return dst


def check(fixture: Path, work: Path) -> None:
    """A fresh rebuild of the fixture's L0 yields the L1 beside it."""
    base = without_l1(fixture, work)
    for v in vehicles(base):
        l1.rebuild(base, v)
        assert expected(base, v) == expected(fixture, v), v.dirname


def replay(fixture: Path, work: Path) -> None:
    """The determinism test of ADR-0009, point 6: each vehicle's stream
    replayed line by line with the incremental derivation after every line
    yields the same files as one rebuild; the other subjects and the
    receipts are there throughout."""
    for v in vehicles(fixture):
        batch = without_l1(fixture, work / v.id / "batch")
        l1.rebuild(batch, v)
        live = without_l1(fixture, work / v.id / "live")
        shutil.rmtree(layout.l0_dir(live, v))
        for path in layout.l0_files(fixture, v):
            dst = layout.l0_dir(live, v) / path.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "a", encoding="utf-8") as f:
                for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
                    f.write(line)
                    f.flush()
                    l1.incremental(live, v)
        for kind in l1.FILES:
            a, b = l1.path_of(batch, v, kind), l1.path_of(live, v, kind)
            assert a.is_file() == b.is_file(), kind
            if a.is_file():
                assert a.read_bytes() == b.read_bytes(), f"{v.dirname} {kind}"


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda p: p.name)
def test_a_fixture_yields_the_l1_beside_it(fixture, tmp_path):
    assert vehicles(fixture), "a fixture holds at least one vehicle"
    check(fixture, tmp_path / "work")


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda p: p.name)
def test_a_fixture_line_by_line_equals_one_rebuild(fixture, tmp_path):
    replay(fixture, tmp_path)


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda p: p.name)
def test_a_fixture_is_valid_and_anonymised(fixture):
    """Every stream validates, and no entity id still carries a name."""
    for subject in layout.subjects(fixture):
        assert l0.validate(fixture, subject).errors == 0, subject.dirname
        for r in l0.read(fixture, subject, kind="state"):
            assert r.line["entity"].split(".", 1)[1].rstrip("_0123456789") in l0.ROLES
