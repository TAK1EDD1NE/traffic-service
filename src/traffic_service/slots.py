# traffic-service/src/traffic_service/slots.py
"""Slot arithmetic. The index is sampled 12x/day on even local hours.

Algeria has no DST, but the zone is still named explicitly rather than assumed:
a fixed +01:00 would silently survive a rule change, and the historical corpus
this must stay comparable with was sampled on local hours.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SLOT_TZ = ZoneInfo("Africa/Algiers")
SLOT_HOURS = range(0, 24, 2)
SLOT_LENGTH_HOURS = 2


def _require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("slot timestamps must be timezone-aware")
    return moment


def as_local(moment: datetime) -> datetime:
    return _require_aware(moment).astimezone(SLOT_TZ)


def slot_for(moment: datetime) -> datetime:
    """Start of the slot containing `moment`, as tz-aware UTC."""
    local = as_local(moment)
    start_hour = local.hour - (local.hour % SLOT_LENGTH_HOURS)
    floored = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    return floored.astimezone(timezone.utc)


def is_slot_start(moment: datetime) -> bool:
    local = as_local(moment)
    return (
        local.hour % SLOT_LENGTH_HOURS == 0
        and local.minute == 0
        and local.second == 0
        and local.microsecond == 0
    )


def slot_after(slot_ts: datetime) -> datetime:
    return (as_local(slot_ts) + timedelta(hours=SLOT_LENGTH_HOURS)).astimezone(timezone.utc)


def slot_key(slot_ts: datetime) -> str:
    """Filesystem-safe directory name for a slot, in local wall clock."""
    return as_local(slot_ts).strftime("%Y-%m-%dT%H-%M")


def slot_label(slot_ts: datetime) -> str:
    """The timestamp the scraper's filename convention uses: local wall clock."""
    return as_local(slot_ts).strftime("%Y-%m-%d_%H-%M")


# The raw-root directory convention: `<YYYY-MM-DD>T<HH>-<MM>` in local wall clock — exactly the
# text `slot_key` writes. One regex, one function, in the module that owns the convention: the
# night job (which walks the day's slot directories) and the prune (which ages them out) must
# agree on what a slot directory *is*, or one of them silently stops recognising the other's.
_SLOT_NAME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}$")


def slot_start_from_name(name: str) -> datetime | None:
    """`<YYYY-MM-DD>T<HH>-MM` -> the slot start as aware local time, else `None`.

    The inverse of `slot_key`, and the only implementation of the directory-name parse: `night`
    (which decides a day's slot directories) and `prune` (which decides what may be deleted) both
    delegate here, so the convention cannot drift between the two.

    Only real slot starts count: the regex cannot rule out an impossible date (month 13), so the
    parse is guarded too, and the even-hour / zero-minute check rejects a stray `T01-00` or
    `T00-30` name. A name this returns `None` for is not a capture slot and is never measured or
    pruned.
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
