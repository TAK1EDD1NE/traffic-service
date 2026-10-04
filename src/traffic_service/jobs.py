"""Job guards: an advisory lock per target, and a job_runs audit row per attempt.

The lock is what makes a restarted container safe: the interrupted job simply
re-runs and can never run twice concurrently. The audit row is what makes a
silent failure visible — a slot that captured 40/95 must leave a trace even
though nothing raised.
"""
from __future__ import annotations

import contextlib
import json

LOCK_KEY_SQL = "select pg_try_advisory_lock(hashtext(%s))"
UNLOCK_SQL = "select pg_advisory_unlock(hashtext(%s))"

INSERT_RUN = """
insert into job_runs (job, target, ok, metrics, error, finished_at)
values (%s, %s, %s, %s, %s, now())
returning id
"""


class JobLocked(RuntimeError):
    """Another session already holds the lock for this job target."""


@contextlib.contextmanager
def advisory_lock(conn, key: str):
    with conn.cursor() as cur:
        cur.execute(LOCK_KEY_SQL, (key,))
        acquired = cur.fetchone()[0]
    if not acquired:
        raise JobLocked(f"another session holds the lock for {key!r}")
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute(UNLOCK_SQL, (key,))


@contextlib.contextmanager
def job_run(conn, job: str, target: str | None = None):
    record: dict = {"metrics": None}
    ok = True
    error: str | None = None
    try:
        yield record
    except BaseException as exc:  # noqa: BLE001 — record, then re-raise unchanged
        ok = False
        error = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        # The audit row must survive the caller's failure, so it is written on a
        # fresh transaction: the caller's transaction may be poisoned.
        with conn.cursor() as cur:
            cur.execute(INSERT_RUN, (job, target, ok,
                                     json.dumps(record.get("metrics")) if ok else None,
                                     error))
        conn.commit()
