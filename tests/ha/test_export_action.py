# SPDX-License-Identifier: BSD-3-Clause
"""The export action (ADR-0017): what it writes into the media folder is
what ``vledger export`` writes, read back with the library and the
standard parsers."""

import csv
import io
import json
import shutil
from xml.etree import ElementTree as ET

import pytest
from custom_components.vledger.const import DOMAIN
from homeassistant.exceptions import ServiceValidationError
from test_event_entities import refuelling, trip
from test_receipt_entry import _act, _settle, _setup, at

from vledger import export, l1, receipts
from vledger.layout import Subject

V = Subject("vehicle", "a7c1")


@pytest.fixture
def media(hass, tmp_path):
    """The instance's local media directory, somewhere writable, and
    allowed as Home Assistant allows its media directories by default."""
    where = tmp_path / "media"
    hass.config.media_dirs = {"local": str(where)}
    hass.config.allowlist_external_dirs = {str(where)}
    return where


async def _export(hass, entry, **data):
    return await _act(hass, "export", config_entry_id=entry.entry_id, **data)


async def test_a_csv_lands_in_the_media_folder_and_is_overwritten(hass, vehicle_entry, tmp_path,
                                                                 media, stand_in):
    stand_in["refuelling"] += [refuelling(0), refuelling(60, 30.0)]
    await _setup(hass, vehicle_entry)
    answer = await _export(hass, vehicle_entry, format="csv", kind="refuelling")
    target = media / "vledger" / "volvo" / "refuellings.csv"
    assert answer == {"path": str(target), "count": 2, "current": True}
    rows = list(csv.DictReader(io.StringIO(target.read_text())))
    assert [r["start"] for r in rows] == [at(0), at(60)]
    assert target.read_text() == export.to_csv("refuelling", list(l1.read(tmp_path, V, "refuelling")))
    assert not target.with_name("refuellings.csv.tmp").exists()

    # The same name again is the latest export, and the span is the verb's
    # --since and --until, by start, in the instance's local time as the
    # receipt actions take it.
    answer = await _export(hass, vehicle_entry, format="json", kind="refuelling", since=at(30))
    assert answer["count"] == 1 and answer["path"] == str(target.with_name("refuellings.json"))
    assert json.loads(target.with_name("refuellings.json").read_text())[0]["start"] == at(60)
    stand_in["refuelling"].append(refuelling(120, 20.0))
    await hass.services.async_call(DOMAIN, "recompute", {"config_entry_id": vehicle_entry.entry_id},
                                   blocking=True)
    await _settle(hass, vehicle_entry)
    answer = await _export(hass, vehicle_entry, format="csv", kind="refuelling")
    assert answer["count"] == 3 and answer["path"] == str(target)
    assert len(list(csv.DictReader(io.StringIO(target.read_text())))) == 3


async def test_gpx_is_the_trips_and_a_name_keeps_several(hass, vehicle_entry, media, stand_in):
    stand_in["trip"] += [trip(0, 12.5), trip(60, 3.0)]
    await _setup(hass, vehicle_entry)
    answer = await _export(hass, vehicle_entry, format="gpx")
    target = media / "vledger" / "volvo" / "trips.gpx"
    assert answer["path"] == str(target) and answer["count"] == 2
    root = ET.parse(target).getroot()
    assert len(root.findall("{" + export.GPX_NS + "}trk")) == 2

    answer = await _export(hass, vehicle_entry, format="gpx", filename="october.gpx", until=at(30))
    assert answer["path"] == str(target.with_name("october.gpx")) and answer["count"] == 1
    assert target.exists() and target.with_name("october.gpx").exists()

    with pytest.raises(ServiceValidationError, match="takes no kind"):
        await _export(hass, vehicle_entry, format="gpx", kind="trip")
    with pytest.raises(ServiceValidationError, match="needs a kind"):
        await _export(hass, vehicle_entry, format="csv")


@pytest.mark.parametrize("bad", ["../trips.csv", "sub/trips.csv", "trips.gpx", ".csv", "trips"])
async def test_a_file_name_is_bare_and_carries_the_extension(hass, vehicle_entry, media, stand_in, bad):
    await _setup(hass, vehicle_entry)
    with pytest.raises(ServiceValidationError, match="not a bare file name"):
        await _export(hass, vehicle_entry, format="csv", kind="trip", filename=bad)
    assert not (media / "vledger").exists()


async def test_nowhere_but_the_media_folder(hass, vehicle_entry, tmp_path, stand_in):
    """A media directory Home Assistant does not allow writing to is refused
    before anything is written — the action's one path check."""
    hass.config.media_dirs = {"local": str(tmp_path / "elsewhere")}
    hass.config.allowlist_external_dirs = set()
    await _setup(hass, vehicle_entry)
    with pytest.raises(ServiceValidationError, match="does not allow writing"):
        await _export(hass, vehicle_entry, format="csv", kind="trip")
    assert not (tmp_path / "elsewhere").exists()


async def test_the_action_names_a_loaded_vehicle_with_an_l1(hass, vehicle_entry, chargepoint_entry,
                                                           tmp_path, media, stand_in):
    await _setup(hass, vehicle_entry, chargepoint_entry)
    with pytest.raises(ServiceValidationError, match="charge point"):
        await _export(hass, chargepoint_entry, format="csv", kind="trip")
    with pytest.raises(ServiceValidationError, match="No loaded"):
        await _act(hass, "export", config_entry_id="nope", format="csv", kind="trip")
    shutil.rmtree(l1.l1_dir(tmp_path, V))
    with pytest.raises(ServiceValidationError, match="no L1 to export yet"):
        await _export(hass, vehicle_entry, format="csv", kind="trip")
    assert not (media / "vledger").exists()


async def test_a_stale_l1_is_exported_as_it_is_and_said_so(hass, vehicle_entry, tmp_path, media,
                                                          stand_in):
    """A receipt appended behind the writer's back: the export renders what
    is on disk and the answer says L1 is not current, as the verb does on
    stderr."""
    stand_in["refuelling"] += [refuelling(0)]
    await _setup(hass, vehicle_entry)
    receipts.append(tmp_path, V, receipts.refuelling(
        at(5), V, anchor=at(0), exact=True, quantity_l=41.0, full=True, total_price=70.0))
    answer = await _export(hass, vehicle_entry, format="csv", kind="refuelling")
    assert answer["current"] is False and answer["reason"] == "the receipts changed"
    assert answer["count"] == 1
