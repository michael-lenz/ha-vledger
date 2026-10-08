# SPDX-License-Identifier: BSD-3-Clause
"""One version number for everything (ADR-0002, consequence 2)."""

import json
import tomllib
from pathlib import Path

import vledger

ROOT = Path(__file__).resolve().parent.parent


def test_library_integration_and_build_agree_on_the_version():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    manifest = json.loads((ROOT / "custom_components/vledger/manifest.json").read_text())
    assert pyproject["project"]["version"] == vledger.__version__
    assert manifest["version"] == vledger.__version__
    assert manifest["requirements"] == [f"vledger=={vledger.__version__}"]
