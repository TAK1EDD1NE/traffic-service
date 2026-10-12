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
failed validation still needs its pixels for the next attempt.
"""
from __future__ import annotations

import logging
import pathlib
import re
import shutil
from datetime import datetime, timedelta

from traffic_service.slots import SLOT_HOURS, SLOT_TZ

__all__ = ["DEFAULT_GRACE_HOURS", "prune_raw", "slot_start"]

# The default grace window the night job passes: two days of pixels, so a late re-run or a
# debugging window still has the images that produced a suspicious measurement.
DEFAULT_GRACE_HOURS = 48

# The same shape `night._parse_slot` accepts. The regex is duplicated rather than imported
# because `night` imports this module (R18) and a shared `night` import here would be circular.
_SLOT_NAME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}$")


def slot_start(name: str) -> datetime | None:
    """`<YYYY-MM-DD>T<HH>-MM` -> the slot start as aware local time, else `None`.

    Only real even-hour slots count, and the regex cannot rule out an impossible date (month 13)
    so the parse is guarded too — the same rule `night.slot_dirs` uses to decide which
    directories are a day's slots. A directory that is not a capture slot is never treated as
    one, and so is never pruned.
    """
    if not _SLOT_NAME.match(name):
        return None
    try:
        start = datetime.strptime(name, "%Y-%m-%dT%H-%M")
    except ValueError:
        return None
    if start.minute != 0 or start.hour not in SLOT_HOURS:
        return None
    return start.replace(tzinfo=SLOT_TZ)


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
