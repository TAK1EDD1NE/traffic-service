# traffic-service/src/traffic_service/alerts.py
"""Operational alerts: the two channels the spec reserved for this plan.

`notify` is the only outbound call in the service and deliberately knows nothing about the
transport — it POSTs the message as a plain-text body, which is what ntfy consumes
(`curl -d "msg" https://ntfy.sh/<topic>`) and what a Telegram relay or a Coolify webhook can be
adapted to. The webhook lives in the environment only (`ALERT_WEBHOOK_URL`, spec:230) — never a
literal and never a default URL in code. Unset webhook = logged and dropped, never an exception:
a failed alert must not fail the job that raised it, and an alert channel that can take the night
batch down is worse than no alert channel.

`health` and `disk_usage` turn the three silent failure modes the monitoring section names
(spec:212-218) into plain sentences — the caller decides the channel. `health` reads the DB;
`disk_usage` reads the filesystem; neither sends anything.
"""
from __future__ import annotations

import logging
import os
import shutil
import urllib.request
from datetime import datetime, timedelta

from traffic_service.slots import SLOT_TZ, slot_for, slot_key

__all__ = ["DISK_ALERT_PERCENT", "HEALTH_WINDOW_HOURS", "disk_usage", "health", "notify"]

# The default fullness at which the raw volume is worth a warning.
DISK_ALERT_PERCENT = 80.0

# The trailing window `health` inspects: the last day of even-hour slots.
HEALTH_WINDOW_HOURS = 24


def notify(message: str, *, url: str | None = None, timeout: float = 10) -> bool:
    """POST `message` to the alert webhook. True when delivered, False when not.

    Never raises: an alert channel that can take the night batch down is worse than no alert
    channel. The webhook is the `url` argument (tests) or `ALERT_WEBHOOK_URL` from the environment
    (production) — there is no baked-in default, so a misconfigured deployment logs and drops
    rather than posting to someone else's topic.
    """
    target = url or os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not target:
        logging.warning("no ALERT_WEBHOOK_URL set — alert not delivered: %s", message)
        return False
    request = urllib.request.Request(
        target, data=message.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8", "Title": "traffic-index"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            delivered = 200 <= response.status < 300
    except Exception as error:  # noqa: BLE001 — see docstring
        logging.warning("alert delivery failed (%s): %s", error, message)
        return False
    if not delivered:
        logging.warning("alert webhook answered non-2xx: %s", message)
    return delivered


def health(conn, now: datetime | None = None, hours: int = HEALTH_WINDOW_HOURS) -> list[str]:
    """Every problem the monitoring section names, as plain sentences.

    The window is the trailing `hours` of even-hour slots, **excluding the slot containing
    `now`** — that one may legitimately still be running. A slot in the window that has no row,
    one stuck `running`, and one that captured short (`partial`/`failed`/`overrun`) are the three
    silent failures; the last night run is reported when it failed. An unhealthy-but-checked
    system is not a failed job — this is a read, the caller raises the channel.
    """
    now = now or datetime.now(SLOT_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SLOT_TZ)
    expected = [slot_for(now - timedelta(hours=h)) for h in range(2, hours + 1, 2)]

    with conn.cursor() as cur:
        cur.execute("select slot_ts, status, captured, expected from slots where slot_ts >= %s",
                    (min(expected),))
        rows = {row[0]: row[1:] for row in cur.fetchall()}
        cur.execute("""select target, ok, error from job_runs
                       where job = 'night' order by id desc limit 1""")
        last_night = cur.fetchone()

    problems: list[str] = []
    for slot_ts in expected:
        key = slot_key(slot_ts)
        row = rows.get(slot_ts)
        if row is None:
            problems.append(f"no slot row for {key} — the capture never ran")
            continue
        status, captured, want = row
        if status == "running":
            problems.append(f"slot {key} still running (stale) — the worker died mid-slot")
        elif status in ("partial", "failed", "overrun"):
            problems.append(f"slot {key} {status}: {captured}/{want} captured")

    if last_night is None:
        problems.append("no night job has ever run")
    else:
        target, ok, error = last_night
        if not ok:
            problems.append(f"night {target} failed: {error}")
    return problems


def disk_usage(path, threshold: float = DISK_ALERT_PERCENT) -> str | None:
    """A message when `path`'s filesystem is fuller than `threshold`, else None.

    A missing `path` returns None rather than raising. An absent volume has no filesystem to
    judge, and on a fresh deploy `/data/raw` does not exist until capture has run its first slot
    — the pass's slot-health lens already reports that symptom (`no slot row for <slot>`), so the
    disk check must not be the one read that turns a fresh deploy's maintenance run into a
    failure (it also contradicts `prune.prune_raw`, which treats the same missing root as "nothing
    to prune", prune.py). `FileNotFoundError` is the *only* error swallowed, so a genuine failure
    on a path that does exist — EACCES, a bad mount — still propagates and stays loud.
    """
    try:
        usage = shutil.disk_usage(str(path))
    except FileNotFoundError:
        return None
    percent = usage.used / usage.total * 100
    if percent <= threshold:
        return None
    return (f"disk {percent:.0f}% used on {path} "
            f"({usage.free / 10**9:.1f} GB free) — prune or extend the volume")
