# SPDX-License-Identifier: BSD-3-Clause
"""REQ-0030: there is one stream per vehicle and per charge point, rotated by
UTC month.

Derived from the requirement, README.md ("a raw log (L0): JSON Lines,
append-only, one stream per vehicle and per charge point, rotated monthly")
and the user guide: *Where the data is* (every verb works on a data directory
given as ``--base``; "a stream is named by what it belongs to: ``--vehicle
ID`` or ``--chargepoint ID``", and that id is the name of the subject's
directory under the data directory without its prefix, ``vehicle-<id>`` and
``chargepoint-<id>``; "Times are UTC ISO 8601; a verb that writes takes
``--t``") and *The raw log: vledger l0* — the verbs the integration writes a
stream with, "usable by hand to build one"; ``l0 read`` prints "the whole
stream, in order"; ``l0 stats`` counts "month files and their sizes", and
Home Assistant shows "the size of the current month file" and "the number of
month files" from it; ``l0 gaps`` lists every span in which nothing was
captured.

A stream's month files are read from ``vledger l0 stats --json``, its
``files`` (a size per month) and ``current_month``, which the user guide names
but does not spell out. That the months are UTC's and not the local ones is
shown by writing the same lines under time zones on either side of UTC: the
months a stream holds do not move with the zone the command runs in. Every
test drives the ``vledger`` command as a program; nothing of ``vledger`` is
imported."""

import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.req("REQ-0030")]

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# Time zones far from UTC on either side, and one with daylight saving time
# that ends in the last week of October.
ZONES = ["UTC", "Pacific/Kiritimati", "Pacific/Honolulu", "Europe/Berlin"]


def run(base: Path, *argv: str, tz: str = "UTC") -> subprocess.CompletedProcess:
    """``vledger <argv> --base <base>`` in a process of its own, with the time
    zone ``tz``."""
    environ = {k: v for k, v in os.environ.items() if k != "VLEDGER_BASE"}
    environ["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), *(e for e in environ.get("PYTHONPATH", "").split(os.pathsep) if e)])
    environ["TZ"] = tz
    return subprocess.run([sys.executable, "-m", "vledger.cli", *argv, "--base", str(base)],
                          check=False, capture_output=True, text=True, env=environ, cwd=base)


def vledger(base: Path, *argv: str, tz: str = "UTC") -> str:
    p = run(base, *argv, tz=tz)
    assert p.returncode == 0, (argv, p.returncode, p.stdout, p.stderr)
    return p.stdout


class Subject:
    """A vehicle or a charge point, written and read by its subject id."""

    def __init__(self, base: Path, noun: str, sid: str, tz: str = "UTC"):
        self.base, self.flag, self.sid, self.tz = base, f"--{noun}", sid, tz
        self.directory = base / f"{noun}-{sid}"

    def __call__(self, *argv: str) -> str:
        return vledger(self.base, *argv, self.flag, self.sid, tz=self.tz)

    def start(self, t: str) -> None:
        self("l0", "start", "--homeassistant", "2026.10.1", "--snapshot", "[]", "--t", t)

    def odometer(self, km: str, t: str) -> None:
        self("l0", "state", "--role", "odometer", "--entity", f"sensor.{self.sid}_odometer",
             "--state", km, "--unit", "km", "--t", t)

    def heartbeat(self, t: str) -> None:
        self("l0", "heartbeat", "--lines", "0", "--t", t)

    def stop(self, t: str) -> None:
        self("l0", "stop", "--reason", "shutdown", "--t", t)

    def read(self) -> list[dict]:
        return [json.loads(x) for x in self("l0", "read").splitlines() if x.strip()]

    def stats(self, now: str) -> dict:
        return json.loads(self("l0", "stats", "--json", "--now", now))

    def months(self, now: str = "2027-06-01T00:00:00Z") -> list[str]:
        """The stream's month files, by month, as ``l0 stats`` counts them."""
        return sorted(self.stats(now)["files"])

    def gaps(self, now: str) -> list[dict]:
        return json.loads(self("l0", "gaps", "--json", "--now", now))


def vehicle(base: Path, sid: str, tz: str = "UTC") -> Subject:
    return Subject(base, "vehicle", sid, tz)


def chargepoint(base: Path, sid: str, tz: str = "UTC") -> Subject:
    return Subject(base, "chargepoint", sid, tz)


def times(lines: list[dict]) -> list[str]:
    return [x["t"] for x in lines]


# --- one stream per vehicle and per charge point ------------------------------

def test_each_vehicle_and_each_charge_point_has_a_stream_of_its_own(tmp_path):
    golf, zoe, home = vehicle(tmp_path, "a7c1"), vehicle(tmp_path, "b2d4"), chargepoint(tmp_path, "c9e0")
    golf.start("2026-10-10T08:00:00Z")
    zoe.start("2026-10-10T08:01:00Z")
    home.start("2026-10-10T08:02:00Z")
    golf.odometer("1000", "2026-10-10T09:00:00Z")
    zoe.odometer("5000", "2026-10-10T09:05:00Z")
    home.heartbeat("2026-10-10T09:02:00Z")
    golf.odometer("1001", "2026-10-10T09:10:00Z")

    assert sorted(p.name for p in tmp_path.iterdir()) == \
        ["chargepoint-c9e0", "vehicle-a7c1", "vehicle-b2d4"]
    assert times(golf.read()) == ["2026-10-10T08:00:00Z", "2026-10-10T09:00:00Z",
                                  "2026-10-10T09:10:00Z"]
    assert times(zoe.read()) == ["2026-10-10T08:01:00Z", "2026-10-10T09:05:00Z"]
    assert [x["kind"] for x in home.read()] == ["start", "heartbeat"]
    assert [x["state"] for x in golf.read() if x["kind"] == "state"] == ["1000", "1001"]
    assert [x["state"] for x in zoe.read() if x["kind"] == "state"] == ["5000"]


def test_a_vehicle_and_a_charge_point_of_the_same_id_keep_two_streams(tmp_path):
    car, wallbox = vehicle(tmp_path, "x1"), chargepoint(tmp_path, "x1")
    car.start("2026-10-10T08:00:00Z")
    wallbox.start("2026-10-10T08:00:30Z")
    car.odometer("42", "2026-10-10T09:00:00Z")
    wallbox.stop("2026-10-10T10:00:00Z")

    assert car.directory.is_dir() and wallbox.directory.is_dir()
    assert [x["kind"] for x in car.read()] == ["start", "state"]
    assert [x["kind"] for x in wallbox.read()] == ["start", "stop"]


def test_every_line_of_a_vehicle_goes_to_its_one_stream(tmp_path):
    """Every kind of line and every role lands in the vehicle's stream, read
    back whole and in order by the vehicle's id alone."""
    golf = vehicle(tmp_path, "a7c1")
    golf.start("2026-10-10T08:00:00Z")
    golf.odometer("1000", "2026-10-10T08:10:00Z")
    golf("l0", "state", "--role", "position", "--entity", "device_tracker.golf",
         "--state", "not_home", "--t", "2026-10-10T08:20:00Z")
    golf.heartbeat("2026-10-10T09:00:00Z")
    golf.stop("2026-10-10T09:30:00Z")

    assert [p.name for p in tmp_path.iterdir()] == ["vehicle-a7c1"]
    lines = golf.read()
    assert [x["kind"] for x in lines] == ["start", "state", "state", "heartbeat", "stop"]
    assert [x.get("role") for x in lines if x["kind"] == "state"] == ["odometer", "position"]


# --- rotated by UTC month ------------------------------------------------------

@pytest.mark.parametrize("tz", ZONES)
def test_a_stream_rotates_at_midnight_utc_on_the_first_of_the_month(tmp_path, tz):
    golf = vehicle(tmp_path, "a7c1", tz)
    golf.start("2026-10-31T23:00:00Z")
    golf.odometer("1000", "2026-10-31T23:59:59Z")
    golf.odometer("1001", "2026-11-01T00:00:00Z")
    golf.odometer("1002", "2026-11-01T00:30:00Z")

    assert golf.months() == ["2026-10", "2026-11"]


@pytest.mark.parametrize("tz", ZONES)
def test_lines_of_one_utc_month_stay_in_one_month_file_whatever_the_local_month(tmp_path, tz):
    """2026-10-31T23:30Z is already November in Berlin and Kiritimati, and
    2026-11-01T00:30Z still October in Honolulu: neither starts a month."""
    golf, zoe = vehicle(tmp_path, "a7c1", tz), vehicle(tmp_path, "b2d4", tz)
    golf.start("2026-10-31T09:00:00Z")
    golf.odometer("1000", "2026-10-31T22:30:00Z")
    golf.odometer("1001", "2026-10-31T23:30:00Z")
    zoe.start("2026-11-01T00:00:00Z")
    zoe.odometer("5000", "2026-11-01T00:30:00Z")
    zoe.odometer("5001", "2026-11-01T09:00:00Z")

    assert golf.months() == ["2026-10"]
    assert zoe.months() == ["2026-11"]


@pytest.mark.parametrize("tz", ZONES)
def test_a_stream_rotates_at_the_turn_of_the_year_in_utc(tmp_path, tz):
    golf = vehicle(tmp_path, "a7c1", tz)
    golf.start("2026-12-31T23:59:00Z")
    golf.odometer("1000", "2026-12-31T23:59:59Z")
    golf.odometer("1001", "2027-01-01T00:00:00Z")

    assert golf.months() == ["2026-12", "2027-01"]


def test_a_stream_has_one_month_file_for_every_month_it_has_lines_in(tmp_path):
    golf = vehicle(tmp_path, "a7c1")
    golf.start("2026-09-30T12:00:00Z")
    golf.odometer("1000", "2026-10-01T12:00:00Z")
    golf.odometer("1001", "2026-10-15T12:00:00Z")
    golf.odometer("1002", "2026-10-31T12:00:00Z")
    golf.odometer("1003", "2026-11-02T12:00:00Z")

    stats = golf.stats("2026-11-02T12:00:00Z")
    assert sorted(stats["files"]) == ["2026-09", "2026-10", "2026-11"]
    assert stats["current_month"] == "2026-11"
    assert all(size > 0 for size in stats["files"].values())


@pytest.mark.parametrize("tz", ZONES)
def test_a_charge_point_s_stream_rotates_by_utc_month_too(tmp_path, tz):
    home = chargepoint(tmp_path, "c9e0", tz)
    home.start("2026-10-31T23:00:00Z")
    home.heartbeat("2026-10-31T23:59:00Z")
    home.heartbeat("2026-11-01T00:59:00Z")

    assert home.months() == ["2026-10", "2026-11"]
    assert [x["kind"] for x in home.read()] == ["start", "heartbeat", "heartbeat"]


def test_each_stream_rotates_on_its_own(tmp_path):
    """A month begins for a stream with its own first line in it, not with
    another subject's."""
    golf, zoe = vehicle(tmp_path, "a7c1"), vehicle(tmp_path, "b2d4")
    golf.start("2026-10-30T08:00:00Z")
    zoe.start("2026-10-30T08:00:00Z")
    golf.odometer("1000", "2026-11-01T08:00:00Z")
    zoe.odometer("5000", "2026-10-31T08:00:00Z")

    assert golf.months() == ["2026-10", "2026-11"]
    assert zoe.months() == ["2026-10"]


# --- a rotated stream is still one stream ---------------------------------------

@pytest.mark.parametrize("tz", ZONES)
def test_a_stream_reads_back_whole_and_in_order_across_its_month_files(tmp_path, tz):
    golf = vehicle(tmp_path, "a7c1", tz)
    golf.start("2026-10-31T22:00:00Z")
    golf.odometer("1000", "2026-10-31T23:50:00Z")
    golf.heartbeat("2026-10-31T23:59:00Z")
    golf.odometer("1001", "2026-11-01T00:10:00Z")
    golf.stop("2026-11-01T00:20:00Z")

    assert golf.months() == ["2026-10", "2026-11"]
    assert times(golf.read()) == ["2026-10-31T22:00:00Z", "2026-10-31T23:50:00Z",
                                  "2026-10-31T23:59:00Z", "2026-11-01T00:10:00Z",
                                  "2026-11-01T00:20:00Z"]
    assert [x["state"] for x in golf.read() if x["kind"] == "state"] == ["1000", "1001"]


def test_the_turn_of_the_month_is_no_capture_gap(tmp_path):
    """A stream that keeps its hourly heartbeat over the turn of the month
    shows no gap there: the rotation loses nothing."""
    golf = vehicle(tmp_path, "a7c1")
    golf.start("2026-10-31T20:00:00Z")
    for hour in ("21", "22", "23"):
        golf.heartbeat(f"2026-10-31T{hour}:00:00Z")
    for hour in ("00", "01", "02"):
        golf.heartbeat(f"2026-11-01T{hour}:00:00Z")

    assert golf.months() == ["2026-10", "2026-11"]
    assert golf.gaps("2026-11-01T02:30:00Z") == []
