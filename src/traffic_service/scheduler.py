"""The 12-per-day capture schedule.

One long-lived process owns this scheduler (the spec's `worker` container); the
advisory lock inside `capture_slot` is what makes a restart safe even if a second
process starts, so this module adds no locking of its own.
"""
from __future__ import annotations

import logging
from datetime import datetime

from traffic_service.capture import capture_slot_guarded
from traffic_service.db import connect
from traffic_service.jobs import JobLocked
from traffic_service.slots import SLOT_HOURS, SLOT_TZ, slot_for


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


def build(raw_root, capturer=None, now=None):
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
    return scheduler
