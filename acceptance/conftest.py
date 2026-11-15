# SPDX-License-Identifier: BSD-3-Clause
"""Acceptance tests: each shows one requirement met as a user meets the
product, derived from the requirement and the user documentation, never from
the code. They import nothing of ``vledger`` or of the integration; Home
Assistant loads the integration from ``custom_components/`` as it would on an
instance, and the tests talk to it as the frontend does.

``@pytest.mark.req("REQ-nnnn")`` names the requirement a test accepts;
``@pytest.mark.docker`` one that needs a Docker daemon. A module that needs Home
Assistant skips itself where its test stack is not installed (the ``ha``
extra, docs/developing.md)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Home Assistant finds custom integrations through the ``custom_components``
# package; the repository's root is where an instance's configuration
# directory would hold it.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    # Home Assistant's test plugin brings a ``custom_components`` package of
    # its own, which may be imported first; the repository's joins it.
    import custom_components
except ImportError:
    pass
else:
    if str(ROOT / "custom_components") not in list(custom_components.__path__):
        custom_components.__path__.append(str(ROOT / "custom_components"))

def pytest_configure(config):
    config.addinivalue_line("markers", "req(id): the requirement a test accepts")
    config.addinivalue_line("markers", "docker: the test needs a Docker daemon")
