# tests/test_validate.py
"""The day's gate: what a publishable day looks like, and where the numbers come from.

`check_day` is pure — it consumes the dict `day_summary` builds and never raises. The four tests
from the task brief are verbatim; the extra cases pin the rulings the brief does not cover: a
missing/non-finite national index (R12/R16), a zone whose whole day came back as zero metres
(R16a), and the constant's own arithmetic (R11). The `day_summary` cases run against real
Postgres, because two of its three readings are SQL questions (the local date of a UTC slot, and
`captured/expected` over only that day's twelve slots).
"""
import datetime as dt
from datetime import datetime, timedelta, timezone

import pytest

from traffic_service import validate
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ

DAY = dt.date(2026, 10, 4)
RULES = dict(slots_required=12, min_capture_ratio=0.95)


@pytest.fixture()
def db():
    """A migrated database seeded with Alger_1..Alger_3 (the night suite's fixture)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        with conn.cursor() as cur:
            for area in (1, 2, 3):
                cur.execute(
                    """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale,
                                          publish, capacity_k, floor_p20)
                       values (%s,'Alger','Alger',%s,'Alger',36.7,3.1,15.25,true,100.0,0.2)""",
                    (f"Alger_{area}", area))
        conn.commit()
        yield conn


def _slot_times(day=DAY, count=12):
    """The day's expected local slots, as UTC timestamps (the 00:00 slot is 23:00 UTC before)."""
    hours = list(range(0, 24, 2))[:count]
    return [datetime(day.year, day.month, day.day, hour, tzinfo=SLOT_TZ).astimezone(timezone.utc)
            for hour in hours]


def _slots(conn, count=12, expected=3, captured=3):
    with conn.cursor() as cur:
        for slot_ts in _slot_times(count=count):
            cur.execute(
                """insert into slots (slot_ts, expected, captured, status)
                   values (%s, %s, %s, 'ok')""",
                (slot_ts, expected, captured))
    conn.commit()


def _measure(conn, zone_id, slot_ts, metres=6.0):
    with conn.cursor() as cur:
        cur.execute(
            """insert into measurements (zone_id, slot_ts, green_m, yellow_m, red_m, dark_red_m,
                                         payload)
               values (%s, %s, %s, %s, %s, %s, '{}')""",
            (zone_id, slot_ts, metres, metres, metres, metres))
    conn.commit()


# ---------- check_day: the brief's four tests, verbatim ----------

def test_a_thin_day_is_rejected():
    day = {"slots_present": 11, "capture_ratio": 1.0, "zero_coverage_zones": [], "national_ci": 8.0}
    ok, reasons = validate.check_day(day, historical=(0.5, 40.0), **RULES)
    assert not ok and any("slots" in r for r in reasons)


def test_a_day_below_the_capture_floor_is_rejected():
    day = {"slots_present": 12, "capture_ratio": 0.90, "zero_coverage_zones": [], "national_ci": 8.0}
    ok, reasons = validate.check_day(day, historical=(0.5, 40.0), **RULES)
    assert not ok and any("capture" in r for r in reasons)


def test_an_implausible_national_index_is_rejected():
    day = {"slots_present": 12, "capture_ratio": 1.0, "zero_coverage_zones": [], "national_ci": 999.0}
    ok, reasons = validate.check_day(day, historical=(0.5, 40.0), **RULES)
    assert not ok and any("national" in r for r in reasons)


def test_a_good_day_passes_and_says_why():
    day = {"slots_present": 12, "capture_ratio": 0.98, "zero_coverage_zones": [], "national_ci": 8.0}
    ok, reasons = validate.check_day(day, historical=(0.5, 40.0), **RULES)
    assert ok and reasons == []


# ---------- the rulings the brief's four tests do not cover ----------

def test_a_day_with_no_aggregates_is_rejected():
    """R12: `day_summary` reports `national_ci = None` when the day has no `index_daily` row —
    a day the night job never aggregated must not publish."""
    day = {"slots_present": 12, "capture_ratio": 1.0, "zero_coverage_zones": [], "national_ci": None}
    ok, reasons = validate.check_day(day, historical=(0.5, 40.0), **RULES)
    assert not ok and any("national" in r for r in reasons)


def test_a_non_finite_national_index_is_rejected():
    """R16(b): NaN silently fails every range comparison, so it must be caught explicitly."""
    day = {"slots_present": 12, "capture_ratio": 1.0, "zero_coverage_zones": [],
           "national_ci": float("nan")}
    ok, reasons = validate.check_day(day, historical=(0.5, 40.0), **RULES)
    assert not ok and any("national" in r for r in reasons)


def test_a_zone_with_zero_coverage_all_day_is_rejected():
    """R16(a): an all-crashed zone's rows read as a calm index — the zone must block the day and
    be named in the reason."""
    day = {"slots_present": 12, "capture_ratio": 1.0, "zero_coverage_zones": ["Alger_3"],
           "national_ci": 8.0}
    ok, reasons = validate.check_day(day, historical=(0.5, 40.0), **RULES)
    assert not ok and any("zero" in r for r in reasons)
    assert any("Alger_3" in r for r in reasons)


def test_the_constants_carry_the_measured_band():
    assert validate.HISTORICAL_NATIONAL_RANGE == (0.5, 40.0)
    assert validate.SLOTS_REQUIRED == 12
    assert validate.MIN_CAPTURE_RATIO == 0.95


# ---------- day_summary: against real Postgres ----------

def test_day_summary_reads_the_days_slots_coverage_and_rollup(db):
    _slots(db)
    with db.cursor() as cur:
        cur.execute(
            """insert into index_daily (day, national_ci, n_cities, n_zones, coverage)
               values (%s, 7.5, 1, 2, 0.24)""", (DAY,))
    db.commit()

    summary = validate.day_summary(db, DAY)

    assert summary["slots_present"] == 12
    assert summary["capture_ratio"] == pytest.approx(1.0)
    assert summary["zero_coverage_zones"] == []
    assert summary["national_ci"] == pytest.approx(7.5)


def test_day_summary_measures_capture_against_the_days_own_expected(db):
    """Never an absolute 95: only 89 of 95 zones are published and a `--limit` run writes a
    smaller `expected`, so 3 captured of 3 expected is a complete day."""
    _slots(db, expected=3, captured=3)

    assert validate.day_summary(db, DAY)["capture_ratio"] == pytest.approx(1.0)


def test_day_summary_sums_captured_and_expected_over_the_days_slots(db):
    """The numbers are chosen so the sum-ratio and the mean-of-ratios disagree: three slots at
    10/10 and nine at 90/100 is 840/930 = 0.9032, not 0.925."""
    with db.cursor() as cur:
        for index, slot_ts in enumerate(_slot_times()):
            cur.execute(
                """insert into slots (slot_ts, expected, captured, status)
                   values (%s, %s, %s, 'ok')""",
                (slot_ts, 10 if index < 3 else 100, 10 if index < 3 else 90))
    db.commit()

    assert validate.day_summary(db, DAY)["capture_ratio"] == pytest.approx(840 / 930)


def test_day_summary_with_no_slots_rows_reports_zero_and_no_index(db):
    summary = validate.day_summary(db, DAY)

    assert summary == {"slots_present": 0, "capture_ratio": 0.0,
                       "zero_coverage_zones": [], "error_rows": 0, "national_ci": None}


def test_day_summary_names_zones_whose_day_total_observed_is_zero(db):
    """An all-crashed zone is measured all day and carried zero metres; a zone measured only on
    another local day is not this day's problem. The 00:00 slot lives on the previous UTC date,
    so a UTC filter would also drop the first slot of every day."""
    _slots(db)
    for slot_ts in _slot_times():
        _measure(db, "Alger_1", slot_ts, metres=6.0)
        _measure(db, "Alger_2", slot_ts, metres=0.0)
    yesterday = _slot_times(dt.date(2026, 10, 3), count=1)[0]
    with db.cursor() as cur:
        cur.execute("""insert into slots (slot_ts, expected, captured, status)
                       values (%s, 3, 3, 'ok')""", (yesterday,))
    db.commit()
    _measure(db, "Alger_3", yesterday, metres=0.0)

    summary = validate.day_summary(db, DAY)

    assert summary["zero_coverage_zones"] == ["Alger_2"]


# ---------- error_rows: reported, and never a gate of its own ----------

def test_day_summary_counts_the_days_errored_measurement_rows(db):
    """The pipeline turns an image it cannot decode into a row (not an exception), with every
    colour metre 0; those rows are dropped by the derived frames, so the count is how many the
    day lost. It is day-wide, whatever the row's other columns say."""
    _slots(db)
    for slot_ts in _slot_times():
        _measure(db, "Alger_1", slot_ts, metres=6.0)
    with db.cursor() as cur:
        cur.execute("update measurements set error = 'cannot decode image' "
                    "where zone_id = 'Alger_1' and slot_ts = %s", (_slot_times()[3],))
    db.commit()

    assert validate.day_summary(db, DAY)["error_rows"] == 1


def test_an_all_errored_zone_still_trips_zero_coverage(db):
    """The frame filter must not weaken the zero-coverage rule: that rule reads `measurements`
    directly, so a zone whose whole day came back with zero metres — every row errored — is
    still named and still fails the day (R16a)."""
    _slots(db)
    for slot_ts in _slot_times():
        _measure(db, "Alger_1", slot_ts, metres=6.0)
        _measure(db, "Alger_2", slot_ts, metres=0.0)
    with db.cursor() as cur:
        cur.execute("update measurements set error = 'cannot decode image' "
                    "where zone_id = 'Alger_2'")
    db.commit()

    summary = validate.day_summary(db, DAY)

    assert summary["zero_coverage_zones"] == ["Alger_2"]
    assert summary["error_rows"] == 12
    ok, reasons = validate.check_day(summary, historical=(0.5, 40.0), **RULES)
    assert not ok and any("zero" in r for r in reasons)


def test_check_day_ignores_the_error_rows_count():
    """`error_rows` rides the summary and the night report but is NOT a rule: an errored slot is
    excluded from the derivation, so one unreadable PNG must not cost the day's index — the
    day-level gate stays the capture ratio (R16's ruling)."""
    day = {"slots_present": 12, "capture_ratio": 0.99, "zero_coverage_zones": [],
           "error_rows": 3, "national_ci": 8.0}
    assert validate.check_day(day, historical=(0.5, 40.0), **RULES) == (True, [])
