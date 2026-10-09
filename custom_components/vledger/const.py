# SPDX-License-Identifier: BSD-3-Clause
"""Names the integration uses; the vocabulary itself is the library's."""

DOMAIN = "vledger"

#: Config entry ``data``: what never changes (ADR-0007, point 3).
DATA_KIND = "kind"
DATA_SUBJECT = "subject"

#: Config entry ``options``: the configuration object, plus where it is stored.
OPT_BASE_PATH = "base_path"

KIND_VEHICLE = "vehicle"
KIND_CHARGEPOINT = "chargepoint"

STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_RECOMPUTING = "recomputing"

ISSUE_ENTITY_REMOVED = "entity_removed"
