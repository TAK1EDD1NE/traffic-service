import pytest

from traffic_service.db import connect
from traffic_service.jobs import JobLocked, advisory_lock, job_run
from traffic_service.migrate import migrate


@pytest.fixture()
def db():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        yield conn


def test_lock_is_exclusive_across_connections(db):
    with advisory_lock(db, "capture:2026-10-05T00:00"):
        with pytest.raises(JobLocked):
            with connect() as other, advisory_lock(other, "capture:2026-10-05T00:00"):
                pytest.fail("second holder must not acquire the lock")


def test_lock_is_released_on_exit(db):
    with advisory_lock(db, "night:2026-10-04"):
        pass
    with connect() as other, advisory_lock(other, "night:2026-10-04"):
        assert True


def test_lock_is_released_even_when_the_body_raises(db):
    with pytest.raises(ValueError):
        with advisory_lock(db, "capture:2026-10-05T02:00"):
            raise ValueError("boom")
    with connect() as other, advisory_lock(other, "capture:2026-10-05T02:00"):
        assert True


def test_different_keys_do_not_block_each_other(db):
    with advisory_lock(db, "capture:2026-10-05T04:00"):
        with connect() as other, advisory_lock(other, "capture:2026-10-05T06:00"):
            assert True


def test_job_run_records_success_and_metrics(db):
    with job_run(db, "capture", target="2026-10-05T00:00") as record:
        record["metrics"] = {"captured": 95, "failed": 0}
    with db.cursor() as cur:
        cur.execute("select job, target, ok, metrics, finished_at is not null from job_runs")
        job, target, ok, metrics, finished = cur.fetchone()
    assert (job, target, ok, metrics) == ("capture", "2026-10-05T00:00", True, {"captured": 95, "failed": 0})
    assert finished is True


def test_job_run_records_failure_and_reraises(db):
    with pytest.raises(RuntimeError):
        with job_run(db, "night", target="2026-10-04") as record:
            raise RuntimeError("validation failed: 11/12 slots")
    with db.cursor() as cur:
        cur.execute("select ok, error from job_runs where job = 'night'")
        ok, error = cur.fetchone()
    assert ok is False
    assert "11/12 slots" in error
