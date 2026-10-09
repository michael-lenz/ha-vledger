# SPDX-License-Identifier: BSD-3-Clause
"""The library's tests need nothing but the library. The integration's, under
tests/ha/, need a Home Assistant core and its test plugin (the ``ha``
extra); without them that directory is left out of the run, not failed."""

import importlib.util

if importlib.util.find_spec("homeassistant") is None:
    collect_ignore = ["ha"]
