# SPDX-License-Identifier: BSD-3-Clause
"""Names the integration uses; the vocabulary itself is the library's."""

DOMAIN = "vledger"

#: Config entry ``data``: what never changes (ADR-0008, point 3).
DATA_KIND = "kind"
DATA_SUBJECT = "subject"

#: Config entry ``options``: the configuration object, plus where it is stored.
OPT_BASE_PATH = "base_path"

KIND_VEHICLE = "vehicle"
KIND_CHARGEPOINT = "chargepoint"

STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_RECOMPUTING = "recomputing"

#: The Repairs issues of HAI-08: an assigned entity gone from the registry,
#: and one unavailable for longer than ``outage_s``.
ISSUE_ENTITY_REMOVED = "entity_removed"
ISSUE_ENTITY_UNAVAILABLE = "entity_unavailable"

SERVICE_RECOMPUTE = "recompute"

#: The receipt actions (HAI-03, ADR-0015).
SERVICE_ADD_REFUELLING = "add_refuelling_receipt"
SERVICE_ADD_CHARGING = "add_charging_receipt"
SERVICE_CANCEL = "cancel_receipt"

#: The form's event select: enter the time by hand rather than pick a candidate.
EVENT_MANUAL = "manual"

#: At most this many candidates in the form's event select, newest first.
FORM_CANDIDATES = 10
