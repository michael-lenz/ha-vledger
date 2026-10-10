# SPDX-License-Identifier: BSD-3-Clause
"""The corrected metrics as external statistics (ADR-0019, HAI-09).

The metric entities' own statistics keep what an entity showed at the
time; a rebuild that corrects a past month leaves them as they were. So
per metric a ``vledger:<subject>_<metric>`` statistic holds a row per
month line of ``periods.jsonl``, and the whole series is written again
whenever the month lines change — rows are upserted by their start, so a
corrected month replaces its row and the running sum after it moves too.

Read through the library (``l1.read``; ADR-0005), once at start and after
every run of the writer. Without a recorder there is nothing to do.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from vledger import l1

from .capture import Capture
from .const import DOMAIN
from .l1view import kinds_of
from .l1writer import L1Writer
from .metrics import METRICS, SUM, Metric, energies, unit_of, value_of

_LOGGER = logging.getLogger(__name__)

#: The unit class under which Home Assistant converts a statistic (point 1).
UNIT_CLASS = {SensorDeviceClass.DISTANCE: "distance", SensorDeviceClass.VOLUME: "volume",
              SensorDeviceClass.ENERGY: "energy"}


def statistic_id(subject_id: str, metric: Metric) -> str:
    return f"{DOMAIN}:{subject_id.lower()}_{metric.key}"


def metadata(subject_id: str, vehicle: str, metric: Metric, currency: str) -> StatisticMetaData:
    summed = metric.what == SUM
    return StatisticMetaData(
        statistic_id=statistic_id(subject_id, metric), source=DOMAIN,
        name=f"{vehicle} {metric.label}",
        has_sum=summed,
        mean_type=StatisticMeanType.NONE if summed else StatisticMeanType.ARITHMETIC,
        unit_class=UNIT_CLASS.get(metric.device_class),
        unit_of_measurement=unit_of(metric.unit, currency))


def rows(metric: Metric, months: list[dict]) -> list[StatisticData]:
    """One row per month line with a value, at the month's start floored to
    the hour (point 2): a sum's state and running sum, a rate's mean."""
    out: list[StatisticData] = []
    running = 0.0
    for line in months:
        value = value_of(metric, line)
        if value is None:
            continue
        start = dt_util.parse_datetime(line["start"]).replace(minute=0, second=0, microsecond=0)
        if metric.what == SUM:
            running += value
            out.append(StatisticData(start=start, state=value, sum=running))
        else:
            out.append(StatisticData(start=start, mean=value, min=value, max=value))
    return out


class MonthlyStatistics:
    def __init__(self, hass: HomeAssistant, capture: Capture, writer: L1Writer) -> None:
        self.hass = hass
        self.subject = capture.subject
        self.base = capture.base
        self.vehicle = capture.config.get("name") or capture.subject.id
        self._writer = writer
        has = energies(kinds_of(capture.config))
        self.metrics = tuple(m for m in METRICS if m.needs in has)
        #: The hash of the month lines last imported (point 3).
        self._imported: str | None = None
        self._lock = asyncio.Lock()
        self._unlisten: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        self._unlisten = self._writer.listen_runs(self._on_run)
        self._on_run()

    @callback
    def async_stop(self) -> None:
        if self._unlisten:
            self._unlisten()
            self._unlisten = None

    @callback
    def _on_run(self) -> None:
        if "recorder" not in self.hass.config.components:
            return
        self.hass.async_create_background_task(
            self.async_import(), name=f"vledger statistics {self.subject.id}")

    def _months(self) -> tuple[list[dict], str]:
        months = [x for x in l1.read(self.base, self.subject, "period") if x.get("period") == "month"]
        digest = hashlib.sha256(json.dumps(months, sort_keys=True).encode()).hexdigest()
        return months, digest

    async def async_import(self) -> None:
        # Serialised, so an older series never lands after a newer one.
        async with self._lock:
            try:
                months, digest = await self.hass.async_add_executor_job(self._months)
            except Exception:
                _LOGGER.exception("vledger: could not read the periods of %s", self.subject.id)
                return
            # No month yet is nothing to write; the same months, nothing new.
            if not months or digest == self._imported:
                return
            currency = self.hass.config.currency
            for metric in self.metrics:
                async_add_external_statistics(
                    self.hass, metadata(self.subject.id, self.vehicle, metric, currency),
                    rows(metric, months))
            self._imported = digest
