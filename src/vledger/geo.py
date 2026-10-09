# SPDX-License-Identifier: BSD-3-Clause
"""Positions: the distance between two of them, and what a fix's accuracy
is worth."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, haversine — a lower bound on any road."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def accuracy_m(value) -> float | None:
    """A reported ``gps_accuracy`` as metres, or ``None`` when it is unknown.

    0 means unknown, not exact: the reference vehicle reports 0 for every
    fix (ISSUE-0004), and no receiver is that good.
    """
    try:
        m = float(value)
    except (TypeError, ValueError):
        return None
    return m if m > 0 else None
