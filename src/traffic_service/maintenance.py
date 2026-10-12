# traffic-service/src/traffic_service/maintenance.py
"""The daily maintenance pass: dump, prune, VACUUM, and report health.

This module owns the *data*, the CLI owns the *presentation*: `run_maintenance` returns its
summary as plain structures and prints nothing, so plan 4's ops work (dump, disk checks, alerts)
extends it — and consumes its result — without re-implementing or duplicating the formatting that
lives in `cli.cmd_maintenance`. That split is the seam plan 4 Task 1 grows into (R18/R21): the
03:30 scheduled run imports this module directly and must share one implementation with the
`maintenance` command, so the alert channel is injected here, not wired in the CLI.

Four jobs, in order:

- **prune** (`prune.prune_raw`) — the same retention rule the night job applies, run on its own so
  pixels are still cleaned up on a day whose publish aborted (the night job prunes only after a
  successful publish, which is correct for the publish path but leaves a failed day's pixels for
  the maintenance pass to age out).
- **dump** (`dump.pg_dump`) — a nightly `pg_dump` to `/data/dump`, the file an operator restores
  on a fresh VPS. **On by default**, and run ahead of every read that could raise (a health query,
  the disk check): a maintenance run that silently skipped the backup is the failure this pass
  exists to prevent, so no later read may be able to cost it. Retention is today's dump plus the
  `keep` newest older ones — `keep + 1` files, 15 at the default `keep=14` (see `dump.py`). A
  non-zero `pg_dump` raises `DumpFailed` out of `run_maintenance`, so the `job_runs` row reads
  `ok = false` with the reason.
- **vacuum** — `VACUUM (ANALYZE) measurements`, the table that grows every night. See the comment
  in `vacuum()`: VACUUM cannot run inside a transaction block, so it is issued on an autocommit
  connection.
- **health** (`recent_slots` and `alerts.health`) — the last `HEALTH_WINDOW_DAYS` of slots as a
  per-slot summary, plus the alertable problems (`alerts.health` — a missing/stale/short slot, a
  failed night) and the disk check. An unhealthy-but-checked system is not a failed run: the
  problems are recorded in `metrics` and sent to the notifier; a failed `pg_dump` — or any other
  step that raises — fails the run and is alerted.
"""
from __future__ import annotations

import pathlib
from datetime import date, datetime

from traffic_service import alerts, db, dump, prune
from traffic_service.jobs import JobLocked, advisory_lock, job_run
from traffic_service.slots import SLOT_TZ

__all__ = ["DEFAULT_DUMP_DIR", "HEALTH_WINDOW_DAYS", "VACUUM_TABLE", "recent_slots",
           "run_maintenance", "vacuum"]

# Where the nightly dumps live. Task 4's compose mounts a volume here; the CLI's parser aliases
# this constant (the scheduler cannot import `cli`, so the default has to live in this module).
DEFAULT_DUMP_DIR = pathlib.Path("/data/dump")

# How far back the per-slot health summary looks: a week is enough to see a capture that has been
# silently failing without dumping the whole table into a job_runs row.
HEALTH_WINDOW_DAYS = 7

# The table that grows every night and is worth reclaiming. `measurements` is retained forever
# (only PNGs are pruned), so VACUUM — never TRUNCATE — is the right tool.
VACUUM_TABLE = "measurements"

SELECT_RECENT_SLOTS = """
select to_char(s.slot_ts at time zone %s, 'YYYY-MM-DD"T"HH24:MI') as slot,
       s.status,
       s.expected,
       s.captured,
       count(c.zone_id) filter (where c.error is not null) as failed
from slots s
left join captures c using (slot_ts)
where s.slot_ts >= now() - make_interval(days => %s)
group by s.slot_ts, s.status, s.expected, s.captured
order by s.slot_ts desc
"""


def recent_slots(conn, window_days: int = HEALTH_WINDOW_DAYS) -> list[dict]:
    """The last `window_days` of slots: local slot label, status, capture counts, failures.

    Every slot the capture job wrote is listed, including a `running` one — an interrupted slot
    that has not been caught up is exactly what the health summary exists to surface.
    """
    with conn.cursor() as cur:
        cur.execute(SELECT_RECENT_SLOTS, (str(SLOT_TZ), window_days))
        rows = cur.fetchall()
    return [{"slot": row[0], "status": row[1], "expected": row[2],
             "captured": row[3], "failed": row[4]} for row in rows]


def vacuum(conn, table: str = VACUUM_TABLE) -> str:
    """VACUUM (ANALYZE) `table` and return its name; the caller's connection is left as it was.

    VACUUM cannot run inside a transaction block (Postgres: "VACUUM cannot run inside a
    transaction block"), and psycopg opens one lazily — so the connection is committed first (to
    end whatever transaction the caller had open) and `autocommit` is switched on for the
    VACUUM only, then restored. Issuing it inside the normal transaction would raise and make
    `maintenance` fail on every run. `table` is a fixed constant, never user input.
    """
    conn.commit()  # end any open transaction: autocommit cannot be toggled inside one
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"vacuum (analyze) {table}")
    finally:
        conn.autocommit = previous
    return table


def run_maintenance(conn, raw_root, *, now: datetime | None = None,
                    grace_hours: int = prune.DEFAULT_GRACE_HOURS,
                    window_days: int = HEALTH_WINDOW_DAYS,
                    dump_dir=None, day: date | None = None, keep: int = dump.DEFAULT_KEEP,
                    notifier=alerts.notify) -> dict:
    """Dump, prune, VACUUM and report health; records a `job_runs` row and alerts the problems.

    The advisory lock (`maintenance`) is taken **inside** the `job_runs` row, exactly as
    `capture_slot_guarded` does — a manual run and the 03:30 scheduled run must not both walk the
    raw root, or the second's `rmtree` races the first's and raises `FileNotFoundError` mid-walk.
    A run refused by the lock raises `JobLocked` unchanged, having still left `ok = true` with
    `metrics {"skipped": "locked"}`: a duplicate is not a failure.

    **Accepted cross-job race (not fixed, deliberately).** This pass prunes on the 48 h slot clock
    while holding only its own lock — it does not take, or wait on, any day's `night:<day>` lock.
    So an operator `night --day <d>` backfill of a day older than 48 h can have its PNGs removed
    mid-measure by a maintenance run that happens to be walking the same root: run that backfill
    with the scheduler stopped. The acceptance is conscious — every measurement the backfill needs
    is already in `measurements` (or is rewritten by the day's own slot directories), the raw
    pixels are transient by design, and the day stays recomputable — and a cross-job lock registry
    is deliberately not built for it.

    The summary is `{"prune": {slots, bytes}, "vacuum": <table>, "slots": [...], "dump": <path>,
    "problems": [...]}` — plain data with no printing, so a second front end (plan 4) reuses it
    as-is, and plan 3's keys are unchanged (R23; `dump` and `problems` are added). `now`,
    `grace_hours`, `window_days`, `dump_dir`, `day` and `keep` are injectable so tests never depend
    on the wall clock or a real `pg_dump`; `day` defaults to `now`'s local date and is **both** the
    `job_runs` target and the dump's file name (R26), and the URL comes from `db.database_url()`
    (its documented fallback), never a bare `os.environ` read. The steps run prune → dump → vacuum →
    health, so the backup precedes every read that could raise and `metrics` is assigned once, after
    the dump result is known.

    The alert channel is injected (`notifier`) and called **after** the `job_runs` row is written,
    from the collected lists, so a delivery problem can never corrupt the record — and the
    scheduled path (which calls this module directly, R18/R21) gets the same alerting a CLI-only
    hook would silently miss (R24). A failed `pg_dump` raises `DumpFailed` out of here, so the
    audit row reads `ok = false` with the reason (`metrics` is NULL by `job_run`'s own rule); the
    dump failure's message is alerted too.
    """
    moment = now or datetime.now(SLOT_TZ)
    day = day or moment.date()
    directory = pathlib.Path(dump_dir) if dump_dir is not None else DEFAULT_DUMP_DIR
    problems: list[str] = []
    failure: str | None = None
    summary: dict = {}
    try:
        with job_run(conn, "maintenance", target=day.isoformat()) as record:
            with advisory_lock(conn, "maintenance"):
                pruned = prune.prune_raw(raw_root, now=moment, grace_hours=grace_hours)

                # The backup runs before every read. It is the one step whose failure fails the
                # run, and it must never be reachable-lost to a health query or the disk check
                # raising — a fresh deploy has no `/data/raw` yet (see `alerts.disk_usage`).
                try:
                    dump_path = dump.pg_dump(db.database_url(), directory, day, keep=keep)["path"]
                except dump.DumpFailed as error:
                    failure = str(error)
                    raise

                vacuumed = vacuum(conn)
                slots = recent_slots(conn, window_days)
                problems = alerts.health(conn, now=moment)
                disk = alerts.disk_usage(raw_root)
                if disk:
                    problems.append(disk)

                # Built in one assignment, after the dump result is known: `job_run` serialises
                # `record["metrics"]` in its `finally`, but a future one that snapshotted the dict
                # at assignment would silently drop a key added afterwards.
                summary = {"prune": pruned, "vacuum": vacuumed, "slots": slots,
                           "problems": problems, "dump": dump_path}
                record["metrics"] = summary
    except JobLocked:
        # A duplicate run, not a failure: another session holds the `maintenance` lock, so this
        # pass did no work. `job_run` already left `ok = true` with `metrics {"skipped": "locked"}`
        # (jobs.py:50-52), and the scheduler logs the same overlap as "already running elsewhere;
        # skipping" (cli.py's `JobLocked` handling). `JobLocked` is a `RuntimeError`, so it must be
        # re-raised HERE, ahead of the total handler below — otherwise a lock-refused run would be
        # recorded as a failure and paged, alerting on the entirely benign cron overlap the lock
        # exists for (the alert-fatigue failure this project's monitoring is built to avoid).
        raise
    except Exception as error:
        # TOTAL failure path: any exception that escapes a step (vacuum, the health/disk reads,
        # `db.database_url()`, …) is recorded for the alert and then re-raised unchanged, so
        # `job_run` still writes `ok = false` with the error text. Before this, only a
        # `DumpFailed` reached the notifier and every other failure ended the run silently
        # (spec:216-218). `DumpFailed` already set `failure` above with its own pinned message.
        if failure is None:
            failure = f"{type(error).__name__}: {error}"
        raise
    finally:
        # After `job_run` wrote its row: a delivery problem must never corrupt the audit record.
        for message in [*problems, *([failure] if failure else [])]:
            notifier(message)
    return summary
