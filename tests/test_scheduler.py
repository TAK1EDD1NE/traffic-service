from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.scheduler import build, catch_up, run_slot, slot_hours

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


def test_the_scheduler_runs_its_jobs_on_a_single_worker():
    """One worker so the 12 jobs run sequentially in-process (spec:82) and a later
    job waits on the earlier slot's lock (spec:103) instead of starting a second
    concurrent Chromium burst on the 2 vCPU box. `max_instances=1` limits each job
    to itself, not the set, so the executor width is the real guard."""
    scheduler = build(raw_root="unused")
    scheduler.start(paused=True)  # materialise the executor without firing a job
    try:
        executor = scheduler._executors["default"]
        assert executor._pool._max_workers == 1
    finally:
        scheduler.shutdown(wait=False)


def test_catch_up_captures_a_slot_that_has_no_row(db, tmp_path):
    result = catch_up(tmp_path, capturer=_ok, now=datetime(2026, 10, 5, 10, 30, tzinfo=ALGIERS))
    assert result is not None and result["status"] == "ok"
    assert (tmp_path / "2026-10-05T10-00" / "Alger_1_2026-10-05_10-00.png").is_file()


def test_catch_up_does_nothing_when_the_slot_has_already_finished(db, tmp_path):
    with db.cursor() as cur:
        cur.execute("insert into slots (slot_ts, expected, status) "
                    "values ('2026-10-05 12:00+01', 1, 'ok')")
    db.commit()
    assert catch_up(tmp_path, capturer=_ok, now=datetime(2026, 10, 5, 12, 30, tzinfo=ALGIERS)) is None
    with db.cursor() as cur:
        cur.execute("select count(*) from captures")
        assert cur.fetchone()[0] == 0, "a finished slot must not be re-captured"


def test_catch_up_resumes_a_slot_whose_zones_were_partly_captured(db, tmp_path):
    """A restart mid-slot leaves the finished zones' rows committed and the slot
    still 'running'; catch-up must resume it rather than redo the whole slot."""
    with db.cursor() as cur:
        for area in (2, 3):
            cur.execute(
                """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale, publish)
                   values (%s,'Alger','Alger',%s,'Alger',36.7,3.1,15.25,true)""",
                (f"Alger_{area}", area))
    db.commit()

    calls = []

    def crashes_after_the_first_zone(zone, dest):
        calls.append(zone["zone_id"])
        if len(calls) > 1:
            raise SystemExit("power cut")
        _ok(zone, dest)

    now = datetime(2026, 10, 5, 14, 30, tzinfo=ALGIERS)
    with pytest.raises(SystemExit):
        catch_up(tmp_path, capturer=crashes_after_the_first_zone, now=now)

    with db.cursor() as cur:
        cur.execute("select count(*), count(path) from captures")
        assert cur.fetchone() == (1, 1), "the first zone's row must be durable"
        cur.execute("select status from slots")
        assert cur.fetchone() == ("running",), "an interrupted slot is left unfinished"

    result = catch_up(tmp_path, capturer=_ok, now=now)
    assert (result["captured"], result["skipped"]) == (2, 1)
