# traffic-service/tests/test_slots.py
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from traffic_service.slots import (SLOT_HOURS, is_slot_start, slot_after,
                                   slot_for, slot_key, slot_label)

ALGIERS = ZoneInfo("Africa/Algiers")


def test_slot_hours_are_every_even_local_hour():
    assert list(SLOT_HOURS) == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]


def test_slot_for_floors_into_the_containing_slot_utc():
    moment = datetime(2026, 10, 5, 1, 59, 30, tzinfo=ALGIERS)
    slot = slot_for(moment)
    assert slot == datetime(2026, 10, 5, 0, 0, tzinfo=ALGIERS).astimezone(timezone.utc)
    assert slot.tzinfo == timezone.utc


def test_slot_for_is_stable_inside_a_slot():
    a = datetime(2026, 10, 5, 15, 0, 1, tzinfo=ALGIERS)
    b = datetime(2026, 10, 5, 15, 59, 59, tzinfo=ALGIERS)
    assert slot_for(a) == slot_for(b)


def test_a_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        slot_for(datetime(2026, 10, 5, 2, 0))


def test_is_slot_start_accepts_exact_even_hours_only():
    assert is_slot_start(datetime(2026, 10, 5, 4, 0, tzinfo=ALGIERS))
    assert not is_slot_start(datetime(2026, 10, 5, 5, 0, tzinfo=ALGIERS))
    assert not is_slot_start(datetime(2026, 10, 5, 4, 0, 1, tzinfo=ALGIERS))


def test_slot_after_steps_two_hours_and_wraps_the_day():
    start = datetime(2026, 10, 5, 22, 0, tzinfo=ALGIERS).astimezone(timezone.utc)
    assert slot_after(start) == datetime(2026, 10, 6, 0, 0, tzinfo=ALGIERS).astimezone(timezone.utc)


def test_twelve_slots_span_exactly_24_hours_all_year():
    """Africa/Algiers has no DST; a wrong zone (e.g. Europe/Paris) fails this in July."""
    for month in (1, 7):
        start = datetime(2026, month, 5, 0, 0, tzinfo=ALGIERS).astimezone(timezone.utc)
        slot = start
        for _ in range(12):
            slot = slot_after(slot)
        assert slot - start == timedelta(hours=24)
        assert datetime(2026, month, 5, 12, 0, tzinfo=ALGIERS).utcoffset() == timedelta(hours=1)


def test_slot_key_and_label_are_local_wall_clock():
    slot = datetime(2026, 10, 5, 14, 0, tzinfo=ALGIERS).astimezone(timezone.utc)
    assert slot_key(slot) == "2026-10-05T14-00"
    assert slot_label(slot) == "2026-10-05_14-00"
