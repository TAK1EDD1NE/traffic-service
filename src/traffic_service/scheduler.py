"""The 12-per-day capture schedule.

One long-lived process owns this scheduler (the spec's `worker` container); the
advisory lock inside `capture_slot` is what makes a restart safe even if a second
process starts, so this module adds no locking of its own.
"""
from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timedelta

from traffic_service import maintenance, night
from traffic_service.capture import capture_slot_guarded
from traffic_service.db import connect
from traffic_service.jobs import JobLocked
from traffic_service.metrics import DEFAULT_WORKERS
from traffic_service.slots import SLOT_HOURS, SLOT_TZ, slot_for

# Where the night job publishes the site's nine CSVs. Mirrors `cli.DEFAULT_OUT_DIR` (R19) — the
# scheduler cannot import `cli` (cli imports this module lazily, and importing it back at module
# level would close the cycle) — and `cmd_scheduler` passes cli's constant explicitly, so this
# fallback is only for a direct `build()` caller.
DEFAULT_OUT_DIR = pathlib.Path("/data/out")


def slot_hours() -> list[tuple[int, int]]:
    return [(hour, 0) for hour in SLOT_HOURS]


def _default_capturer():
    from traffic_service.browser import capturer
    return capturer


def run_slot(raw_root, capturer=None, now: datetime | None = None) -> dict | None:
    """Capture the slot containing `now`. Returns None when another run holds it."""
    moment = now or datetime.now(SLOT_TZ)
    slot_ts = slot_for(moment)
    try:
        with connect() as conn:
            return capture_slot_guarded(conn, slot_ts, raw_root=raw_root,
                                        capturer=capturer or _default_capturer())
    except JobLocked:
        logging.info("slot %s is already being captured by another run; skipping", slot_ts)
        return None


def catch_up(raw_root, capturer=None, now: datetime | None = None) -> dict | None:
    """Re-run the current slot if a restart interrupted it.

    The `slots` row is written up front (the `captures` foreign key needs it) and
    only finished at the end, so an interrupted slot is left `running`. A restart
    that merely computes the *next* fire time would never retry it, making the
    missed slot a total hole. Capture the current slot unless it has already
    finished; the advisory lock makes this safe against a concurrent run.
    """
    moment = now or datetime.now(SLOT_TZ)
    slot_ts = slot_for(moment)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select 1 from slots where slot_ts = %s and status <> 'running'",
                        (slot_ts,))
            if cur.fetchone() is not None:
                return None
    return run_slot(raw_root, capturer=capturer, now=moment)


def run_night_job(raw_root, out_dir=None, now=None, workers=DEFAULT_WORKERS) -> dict | None:
    """The 00:30 job: close the day before `now` — measure, aggregate, publish, prune.

    The day is the **local** day before today in `SLOT_TZ` (`cli.resolve_day("yesterday")`'s rule):
    at 00:30 the day that just closed is the one to publish. Returns None when another run holds
    the day's advisory lock — the work is happening, just not here.
    """
    moment = now or datetime.now(SLOT_TZ)
    day = (moment.astimezone(SLOT_TZ) - timedelta(days=1)).date()
    directory = pathlib.Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    try:
        with connect() as conn:
            return night.run_day(conn, day, raw_root, directory, workers=workers, now=moment)
    except JobLocked:
        logging.info("night %s is already running elsewhere; skipping", day)
        return None


def run_maintenance_job(raw_root, now=None, dump_dir=None) -> dict | None:
    """The 03:30 job: dump, prune raw PNGs, VACUUM, and record the slot health summary.

    Returns None when another run holds the maintenance lock. `now` is injectable so the job is
    testable without a wall-clock dependency (the same rule the capture jobs follow), and
    `dump_dir` mirrors `run_night_job`'s `out_dir` so a caller can point the `pg_dump` at a temp
    directory instead of the `/data/dump` default. The run's alerts go through
    `maintenance.run_maintenance`'s default notifier (`alerts.notify`): the scheduled path is a
    caller of the module, so the channel is injected there, not wired here (R24).
    """
    try:
        with connect() as conn:
            return maintenance.run_maintenance(conn, raw_root, now=now, dump_dir=dump_dir)
    except JobLocked:
        logging.info("maintenance is already running elsewhere; skipping")
        return None


def build(raw_root, *, out_dir=None, capturer=None, now=None):
    """The 14-job scheduler. Everything after `raw_root` is keyword-only: `out_dir` used to sit in
    the second positional slot, so a future `build(root, my_capturer)` would have bound a callable
    as a path and failed much later inside the night job.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(
        timezone=SLOT_TZ,
        # One worker: the spec says the jobs run sequentially in-process and a
        # later job waits on the earlier slot's advisory lock (spec:82, spec:103).
        # The default 10-wide thread pool plus a *per-slot* lock would let a slot
        # that overran its 2 h window start a second concurrent Chromium burst.
        executors={"default": {"type": "threadpool", "max_workers": 1}},
    )
    for hour, minute in slot_hours():
        scheduler.add_job(
            run_slot,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=SLOT_TZ),
            args=[raw_root],
            kwargs={"capturer": capturer, "now": now},
            id=f"capture-{hour:02d}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
    # The night pass, half an hour after the 22:00 slot's two-hour window closes, then maintenance
    # once the night job has had time to finish (it is the only thing reading the raw root). Both
    # get an hour of misfire grace — unlike a capture slot, which is a hard 2 h window, a night run
    # that a restart delayed is still worth running late. coalesce=True means a restart that missed
    # several fires runs the job once, not once per missed fire.
    scheduler.add_job(
        run_night_job,
        trigger=CronTrigger(hour=0, minute=30, timezone=SLOT_TZ),
        args=[raw_root],
        kwargs={"out_dir": out_dir, "now": now},
        id="night",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_maintenance_job,
        trigger=CronTrigger(hour=3, minute=30, timezone=SLOT_TZ),
        args=[raw_root],
        kwargs={"now": now},
        id="maintenance",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler
