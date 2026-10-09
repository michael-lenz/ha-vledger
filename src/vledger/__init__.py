# SPDX-License-Identifier: BSD-3-Clause
"""vledger — the vehicle ledger library.

Everything the integration derives is derived here, and only here
(ARC-02). The library knows nothing of Home Assistant (ARC-03) and depends
on nothing outside the standard library (CLI-06).
"""

#: One version number for the library, the integration and the derivation
#: logic recorded in L1 (ADR-0002, ABL-07). pyproject.toml and
#: custom_components/vledger/manifest.json carry the same string; a test
#: keeps the three in step.
__version__ = "0.3.0"
