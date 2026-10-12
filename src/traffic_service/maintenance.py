# traffic-service/src/traffic_service/maintenance.py
"""The daily maintenance pass: prune the raw PNGs, VACUUM the measurements table, report health.

This module owns the *data*, the CLI owns the *presentation*: `run_maintenance` returns its
summary as plain structures and prints nothing, so plan 4's ops work (dump, disk checks, alerts)
can extend it — and consume its result — without re-implementing or duplicating the formatting
that lives in `cli.cmd_maintenance`. That split is the seam plan 4 Task 1 grows into.

Three jobs, in order:

- **prune** (`prune.prune_raw`) — the same retention rule the night job applies, run on its own so
  pixels are still cleaned up on a day whose publish aborted (the night job prunes only after a
  successful publish, which is correct for the publish path but leaves a failed day's pixels for
  the maintenance pass to age out).
- **vacuum** — `VACUUM (ANALYZE) measurements`, the table that grows every night. See the comment
  in `vacuum()`: VACUUM cannot run inside a transaction block, so it is issued on an autocommit
  connection.
- **health** (`recent_slots`) — the last `HEALTH_WINDOW_DAYS` of slots with their status and capture
  counts. This is the read plan 4's stale-`running`-slot alert will build on; it is computed here,
  not alerted on.
"""
from __future__ import annotations

from datetime import datetime

from traffic_service import prune
from traffic_service.jobs import advisory_lock, job_run
from traffic_service.slots import SLOT_TZ

__all__ = ["HEALTH_WINDOW_DAYS", "VACUUM_TABLE", "recent_slots", "run_maintenance", "vacuum"]

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
                    window_days: int = HEALTH_WINDOW_DAYS) -> dict:
    """Prune, VACUUM and report health; returns the whole summary and records a `job_runs` row.

    The advisory lock (`maintenance`) is taken outside the `job_runs` row, as the night job's is:
    a manual run and the 03:30 scheduled run must not both walk the raw root, or the second's
    `rmtree` races the first's and raises `FileNotFoundError` mid-walk.

    The summary is `{"prune": {slots, bytes}, "vacuum": <table>, "slots": [...]}` — plain data with
    no printing, so a second front end (plan 4) reuses it as-is. `now` and `grace_hours` are
    injectable so tests never depend on the wall clock.
    """
    with advisory_lock(conn, "maintenance"):
        with job_run(conn, "maintenance") as record:
            pruned = prune.prune_raw(raw_root, now=now, grace_hours=grace_hours)
            vacuumed = vacuum(conn)
            slots = recent_slots(conn, window_days)
            summary = {"prune": pruned, "vacuum": vacuumed, "slots": slots}
            record["metrics"] = summary
            return summary
