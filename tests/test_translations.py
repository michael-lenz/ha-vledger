# SPDX-License-Identifier: BSD-3-Clause
"""The integration's strings in English and German (ADR-0069, consequence 3).

``custom_components/vledger/strings.json`` and ``translations/en.json`` are
the source; ``translations/de.json`` is derived from them and complete
wherever they are. The key sets are compared as files, so the check needs
no Home Assistant. The card's strings are held to the same rule by
tests/frontend/model.test.mjs."""

import json
from pathlib import Path

import pytest

INTEGRATION = Path(__file__).resolve().parents[1] / "custom_components" / "vledger"

pytestmark = [pytest.mark.req("REQ-0011")]


def keys(node, prefix=()):
    """Every leaf of a translation file, as the path of keys that reaches it."""
    if isinstance(node, dict):
        out = set()
        for key, value in node.items():
            out |= keys(value, prefix + (key,))
        return out
    return {".".join(prefix)}


def load(name):
    return json.loads((INTEGRATION / name).read_text(encoding="utf-8"))


def test_keys_finds_every_leaf_and_only_leaves():
    tree = {"config": {"step": {"user": {"title": "T", "data": {"a": "A", "b": "B"}}}},
            "title": "X"}
    assert keys(tree) == {"config.step.user.title", "config.step.user.data.a",
                          "config.step.user.data.b", "title"}


def test_english_translation_has_exactly_the_source_keys():
    source, english = keys(load("strings.json")), keys(load("translations/en.json"))
    assert sorted(source - english) == [], "in strings.json, not in en.json"
    assert sorted(english - source) == [], "in en.json, not in strings.json"


def test_german_translation_has_exactly_the_english_keys():
    english, german = keys(load("translations/en.json")), keys(load("translations/de.json"))
    assert sorted(english - german) == [], "English strings without a German one"
    assert sorted(german - english) == [], "German strings the English source lacks"


def test_no_german_string_is_empty():
    def leaves(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from leaves(value)
        else:
            yield node

    assert all(str(text).strip() for text in leaves(load("translations/de.json")))
