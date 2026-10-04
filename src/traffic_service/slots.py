# traffic-service/src/traffic_service/slots.py
"""Slot arithmetic. The index is sampled 12x/day on even local hours.

Algeria has no DST, but the zone is still named explicitly rather than assumed:
a fixed +01:00 would silently survive a rule change, and the historical corpus
this must stay comparable with was sampled on local hours.
"""
from __future__ import annotations

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
