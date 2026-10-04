from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.scheduler import build, run_slot, slot_hours

ALGIERS = ZoneInfo("Africa/Algiers")


@pytest.fixture()
def db():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        with conn.cursor() as cur:
            cur.execute(
                """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale, publish)
                   values ('Alger_1','Alger','Alger',1,'Alger',36.7,3.1,15.25,true)""")
        conn.commit()
        yield conn


def _ok(zone, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"png")


def test_slot_hours_are_the_twelve_even_local_hours():
    assert slot_hours() == [(h, 0) for h in (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)]


def test_the_scheduler_has_one_job_per_slot_hour_in_algiers():
    from apscheduler.triggers.cron import CronTrigger

    hour_index = CronTrigger.FIELD_NAMES.index("hour")
    scheduler = build(raw_root="unused")
    try:
        triggers = [job.trigger for job in scheduler.get_jobs()]
        assert len(triggers) == 12
        assert sorted({int(str(t.fields[hour_index])) for t in triggers}) == [
            0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]
        assert {str(t.timezone) for t in triggers} == {"Africa/Algiers"}
    finally:
        if scheduler.running:  # build() returns a stopped scheduler; shutdown() raises otherwise
            scheduler.shutdown(wait=False)


def test_run_slot_captures_the_slot_containing_now(db, tmp_path):
    result = run_slot(tmp_path, capturer=_ok, now=datetime(2026, 10, 5, 3, 15, tzinfo=ALGIERS))
    assert result["status"] == "ok"
    assert (tmp_path / "2026-10-05T02-00" / "Alger_1_2026-10-05_02-00.png").is_file()
    with db.cursor() as cur:
        cur.execute("select slot_ts at time zone 'UTC' from slots")
        assert cur.fetchone()[0] == datetime(2026, 10, 5, 1, 0), "02:00 Algiers is 01:00 UTC"


def test_run_slot_returns_none_when_the_slot_is_locked(db, tmp_path):
    with db.cursor() as cur:
        cur.execute("select pg_advisory_lock(hashtext('capture:2026-10-05T04-00'))")
    try:
        assert run_slot(tmp_path, capturer=_ok, now=datetime(2026, 10, 5, 4, 0, tzinfo=ALGIERS)) is None
    finally:
        with db.cursor() as cur:
            cur.execute("select pg_advisory_unlock(hashtext('capture:2026-10-05T04-00'))")
        db.commit()


def test_run_slot_records_a_job_run_row(db, tmp_path):
    run_slot(tmp_path, capturer=_ok, now=datetime(2026, 10, 5, 6, 30, tzinfo=ALGIERS))
    with db.cursor() as cur:
        cur.execute("select job, target, ok from job_runs")
        assert cur.fetchone() == ("capture", "2026-10-05T06-00", True)
