from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from traffic_service.capture import capture_slot, load_zones
from traffic_service.capture_helpers import slot_dir, zone_filename
from traffic_service.db import connect
from traffic_service.jobs import JobLocked
from traffic_service.migrate import migrate

ALGIERS = ZoneInfo("Africa/Algiers")
SLOT = datetime(2026, 10, 5, 2, 0, tzinfo=ALGIERS).astimezone(timezone.utc)
ONE_ZONE = [{"zone_id": "Alger_1", "city": "Alger", "lat": 36.7, "lng": 3.1, "scale": 15.25}]


@pytest.fixture()
def db():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        with conn.cursor() as cur:
            for area in (1, 2, 3):
                cur.execute(
                    """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale, publish)
                       values (%s,'Alger','Alger',%s,'Alger',36.7,3.1,15.25,true)""",
                    (f"Alger_{area}", area))
        conn.commit()
        yield conn


def _ok(zone, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"png")


def _fails(zone, dest):
    raise RuntimeError("no traffic layer")


def test_zone_filename_is_zone_id_plus_slot_label():
    assert zone_filename("Alger_1", SLOT) == "Alger_1_2026-10-05_02-00.png"


def test_slot_dir_is_the_slot_key_under_the_raw_root(tmp_path):
    assert slot_dir(tmp_path, SLOT) == tmp_path / "2026-10-05T02-00"
    assert (tmp_path / "2026-10-05T02-00").is_dir()


def test_load_zones_returns_published_zones_ordered(db):
    zones = load_zones(db)
    assert [z["zone_id"] for z in zones] == ["Alger_1", "Alger_2", "Alger_3"]
    assert zones[0]["lat"] == 36.7


def test_a_successful_slot_records_files_and_rows(db, tmp_path):
    result = capture_slot(db, SLOT, raw_root=tmp_path, capturer=_ok)
    assert (result["status"], result["captured"], result["failed"]) == ("ok", 3, 0)
    for area in (1, 2, 3):
        assert (tmp_path / "2026-10-05T02-00" / f"Alger_{area}_2026-10-05_02-00.png").is_file()
    with db.cursor() as cur:
        cur.execute("select count(*), count(path), count(error) from captures")
        assert cur.fetchone() == (3, 3, 0)
        cur.execute("select status, captured, expected from slots")
        assert cur.fetchone() == ("ok", 3, 3)


def test_a_failed_zone_is_a_visible_row_with_a_null_path(db, tmp_path):
    result = capture_slot(db, SLOT, raw_root=tmp_path, capturer=_fails, backoff_seconds=0)
    assert (result["status"], result["failed"]) == ("failed", 3)
    with db.cursor() as cur:
        cur.execute("select count(*), count(path), count(error) from captures")
        assert cur.fetchone() == (3, 0, 3)


def test_a_partial_slot_is_neither_ok_nor_failed(db, tmp_path):
    # One zone fails for good (attempt and retry): a transient single failure
    # would be papered over by retries=1 and the slot would finish "ok".
    def flaky(zone, dest):
        if zone["zone_id"] == "Alger_2":
            raise RuntimeError("persistent")
        _ok(zone, dest)

    result = capture_slot(db, SLOT, raw_root=tmp_path, capturer=flaky, backoff_seconds=0)
    assert (result["status"], result["captured"], result["failed"]) == ("partial", 2, 1)


def test_a_transient_failure_is_retried_once(db, tmp_path):
    calls = {"n": 0}

    def once_flaky(zone, dest):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        _ok(zone, dest)

    result = capture_slot(db, SLOT, raw_root=tmp_path, capturer=once_flaky, zones=ONE_ZONE,
                          backoff_seconds=0)
    assert result["captured"] == 1
    assert calls["n"] == 2


def test_rerunning_a_complete_slot_changes_nothing(db, tmp_path):
    first = capture_slot(db, SLOT, raw_root=tmp_path, capturer=_ok)
    second = capture_slot(db, SLOT, raw_root=tmp_path, capturer=_fails, backoff_seconds=0)
    assert first["captured"] == 3
    assert (second["captured"], second["skipped"]) == (0, 3)
    with db.cursor() as cur:
        cur.execute("select count(*) from captures")
        assert cur.fetchone()[0] == 3
        cur.execute("select status, captured from slots")
        assert cur.fetchone() == ("ok", 3)


def test_force_recaptures_without_duplicating(db, tmp_path):
    capture_slot(db, SLOT, raw_root=tmp_path, capturer=_ok)
    again = capture_slot(db, SLOT, raw_root=tmp_path, capturer=_ok, force=True)
    assert again["captured"] == 3
    with db.cursor() as cur:
        cur.execute("select count(*) from captures")
        assert cur.fetchone()[0] == 3


def test_a_failed_zone_is_retried_on_a_later_run(db, tmp_path):
    capture_slot(db, SLOT, raw_root=tmp_path, capturer=_fails, zones=ONE_ZONE, backoff_seconds=0)
    again = capture_slot(db, SLOT, raw_root=tmp_path, capturer=_ok, zones=ONE_ZONE)
    assert again["captured"] == 1
    with db.cursor() as cur:
        cur.execute("select path, error from captures")
        path, error = cur.fetchone()
    assert path is not None and error is None


def test_a_second_concurrent_run_is_refused(db, tmp_path):
    # The lock must be held by a DIFFERENT session: session-level advisory locks
    # are re-entrant within one connection, so holding it on `db` would let
    # capture_slot acquire it again and the test would pass vacuously.
    with connect() as other:
        with other.cursor() as cur:
            cur.execute("select pg_advisory_lock(hashtext('capture:2026-10-05T02-00'))")
        try:
            with pytest.raises(JobLocked):
                capture_slot(db, SLOT, raw_root=tmp_path, capturer=_ok)
        finally:
            with other.cursor() as cur:
                cur.execute("select pg_advisory_unlock(hashtext('capture:2026-10-05T02-00'))")
            other.commit()
    with db.cursor() as cur:
        cur.execute("select count(*) from captures")
        assert cur.fetchone()[0] == 0, "a refused run must change nothing"


def test_an_empty_registry_raises(db, tmp_path):
    with db.cursor() as cur:
        cur.execute("delete from zones")
    db.commit()
    with pytest.raises(RuntimeError, match="no zones"):
        capture_slot(db, SLOT, raw_root=tmp_path, capturer=_ok)
