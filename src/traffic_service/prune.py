# traffic-service/src/traffic_service/prune.py
"""PNG retention: the pixels are transient, the measurement columns are forever.

A slot's PNGs are read exactly twice — once by the capture job that writes them, once by the
night job that measures them into `measurements` — and after that they have no reader. Retention
is therefore keyed on the **slot the directory names** (`<YYYY-MM-DD>T<HH>-MM`, local wall clock)
and never on the file's mtime: a `cp -r`, an rsync, or a restore from a backup resets every
mtime, and an mtime clock would then either hold a month of pixels forever or (worse) delete a
fresh slot whose mtime was copied from an old one. The grace window is measured from the slot's
own start, so a directory is removed only once it is older than `grace_hours`.

Only PNG slot directories are pruned. `measurements` — and every other table — is retained
forever; this module issues no statement against the database at all, which is what makes the
retention rule (`spec:125-129`) trivially true.

The prune runs last in the night job and only after a publish that did not raise: a day that
failed validation still needs its pixels for the next attempt. It runs *outside* the night's
`job_runs` row (see `night.run_day`), so a prune that fails cannot colour the night's verdict —
the pixels are transient, the published day is not. The `maintenance` pass, which prunes on the
same rule, is where a persistent prune failure surfaces.
"""
from __future__ import annotations

import logging
import pathlib
import shutil
from datetime import datetime, timedelta

from traffic_service.slots import SLOT_TZ, slot_start_from_name

__all__ = ["DEFAULT_GRACE_HOURS", "prune_raw", "slot_start"]

# The default grace window the night job passes: two days of pixels, so a late re-run or a
# debugging window still has the images that produced a suspicious measurement.
DEFAULT_GRACE_HOURS = 48


def slot_start(name: str) -> datetime | None:
    """`<YYYY-MM-DD>T<HH>-MM` -> the slot start as aware local time, else `None`.

    A thin delegation to `slots.slot_start_from_name`, which owns the directory convention — the
    night job decides which directories are a day's slots through the same function, so the two
    cannot drift apart (a drift would make this module silently prune nothing). The name is kept
    because it is this module's published surface.
    """
    return slot_start_from_name(name)


def _bytes_in(directory) -> int:
    """Every regular file's size under `directory`, recursively (recursive: nothing is missed)."""
    total = 0
    for path in pathlib.Path(directory).rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:  # a file that vanished mid-walk is simply not counted
            continue
    return total


def prune_raw(raw_root, now: datetime | None = None, grace_hours: int = DEFAULT_GRACE_HOURS) -> dict:
    """Remove every slot directory older than `grace_hours`; return `{slots, bytes}`.

    `now` is injectable so tests never depend on the wall clock — the same rule the rest of the
    service follows. It must be timezone-aware (the slot start is). A missing `raw_root` is not an
    error: an empty raw root prunes nothing.
    """
    moment = now or datetime.now(SLOT_TZ)
    root = pathlib.Path(raw_root)
    if not root.is_dir():
        return {"slots": 0, "bytes": 0}

    removed = freed = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        start = slot_start(entry.name)
        if start is None:
            continue
        age = moment - start
        if age <= timedelta(hours=grace_hours):
            continue
        freed += _bytes_in(entry)
        shutil.rmtree(entry)
        removed += 1
        logging.info("pruned slot %s (%.1f h old)", entry.name, age.total_seconds() / 3600)
    return {"slots": removed, "bytes": freed}
