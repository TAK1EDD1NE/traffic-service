from datetime import date, datetime
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


def test_the_scheduler_has_the_12_capture_jobs_plus_the_night_and_maintenance_jobs():
    """12 capture slots plus the 00:30 night pass and the 03:30 maintenance pass — 14, exactly.

    Asserted as an exact set (not "at least 12") so a lost or renamed job fails loudly: the two
    ops jobs are plan 4's contract (the night exit code is its alerting hook).
    """
    from apscheduler.triggers.cron import CronTrigger

    hour_index = CronTrigger.FIELD_NAMES.index("hour")
    minute_index = CronTrigger.FIELD_NAMES.index("minute")
    scheduler = build(raw_root="unused")
    try:
        jobs = scheduler.get_jobs()
        triggers = [job.trigger for job in jobs]
        assert len(triggers) == 14
        times = {(int(str(t.fields[hour_index])), int(str(t.fields[minute_index])))
                 for t in triggers}
        capture = {(h, 0) for h in (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)}
        assert times == capture | {(0, 30), (3, 30)}
        assert {str(t.timezone) for t in triggers} == {"Africa/Algiers"}
        assert {"night", "maintenance"} <= {job.id for job in jobs}
    finally:
        if scheduler.running:  # build() returns a stopped scheduler; shutdown() raises otherwise
            scheduler.shutdown(wait=False)


def test_the_night_job_closes_the_local_day_before_now(db, tmp_path, monkeypatch):
    """00:30 closes **yesterday** on the Algiers clock and hands the run to `night.run_day`."""
    from traffic_service import night, scheduler

    captured = {}

    def fake_run_day(conn, day, raw_root, out_dir, **kwargs):
        captured.update(day=day, raw_root=raw_root, out_dir=out_dir, now=kwargs.get("now"))
        return {"ok": True}

    monkeypatch.setattr(night, "run_day", fake_run_day)
    result = scheduler.run_night_job(tmp_path, out_dir=tmp_path / "out",
                                     now=datetime(2026, 10, 5, 0, 30, tzinfo=ALGIERS))

    assert result == {"ok": True}
    assert captured["day"] == date(2026, 10, 4), "00:30 on the 5th closes the 4th"
    assert captured["out_dir"] == tmp_path / "out"


def test_the_maintenance_job_records_a_run_and_exits(db, tmp_path):
    from traffic_service import scheduler
    result = scheduler.run_maintenance_job(tmp_path, now=datetime(2026, 10, 5, 3, 30, tzinfo=ALGIERS))
    assert result["vacuum"] == "measurements"
    with db.cursor() as cur:
        cur.execute("select job, ok from job_runs where job = 'maintenance'")
        assert cur.fetchone() == ("maintenance", True)


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
