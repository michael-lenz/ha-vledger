# SPDX-License-Identifier: BSD-3-Clause
"""Every derivation, in one import.

A derivation registers itself when its module is imported — ``trips``,
``refuellings`` and ``charging`` in :data:`vledger.l1.DERIVATIONS`,
``periods`` as :data:`vledger.l1.PERIODS` — and ``l1`` knows none of
them by name, so that it never imports what imports it. Whatever runs the
derivations by kind rather than by name — ``l1.rebuild``,
``l1.incremental``, the integration's live writer — imports this module
first and gets exactly the set ``vledger derive all`` gets. A new
derivation joins the import below, and nothing else has to learn of it.
"""

from vledger import charging, periods, refuellings, trips

__all__ = ["charging", "periods", "refuellings", "trips"]
