# traffic-service/src/traffic_service/validate.py
"""Whether a closed day may reach the site.

Two halves, deliberately split. `day_summary` is the only part that talks to the database — it
reads back the numbers a publish decision needs. `check_day` is pure, so the rules can be
reviewed and tested without a day's worth of fixtures. `check_day` returns `(ok, reasons)` and
never raises: a bad day is data the caller reports (the `night` command writes it to `job_runs`
and raises the alarm — Task 6), not an exception to swallow somewhere up-stack.

The four rules, and why each one exists:

- `slots_present` vs `SLOTS_REQUIRED` — a thin day is a hole in the series: the site's `daily.csv`
  would carry a value computed from a fraction of the day's twelve samples.
- `capture_ratio` vs `MIN_CAPTURE_RATIO` — measured against the day's own `sum(captured) /
  sum(expected)`, never an absolute zone count: only 89 of 95 zones are published and a `--limit`
  run writes a smaller `expected`, so an absolute 95 would reject every real day. This is the
  spec's partial-crash signal.
- `zero_coverage_zones` — an all-crashed zone still has `measurements` rows, all-zero metres, and
  its false `ci = 0` reads as a calm day; the zone is named in the reason so the operator can see
  which capture broke (R16a).
- `national_ci` — the day's own aggregate from `index_daily`, required to exist and to be finite.
  A missing row means the night job never aggregated the day (or crashed before it), and NaN
  silently passes every range comparison, so it is rejected explicitly rather than by the band
  (R12/R16b).

`day_summary`'s readings are all on the day's **local** clock: the 00:00 slot is 23:00 UTC the
day before, so a UTC-dated filter would drop a slot of every day (`night.clean_frame` carries the
same rule).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from traffic_service.slots import SLOT_HOURS, SLOT_TZ

__all__ = ["HISTORICAL_NATIONAL_RANGE", "MIN_CAPTURE_RATIO", "SLOTS_REQUIRED", "check_day",
           "day_summary"]

# R11 — the plausible band for a day's national index. Measured 2026-10-12 from the shipped
# corpus CSVs: the capacity-weighted national index is 11.559, the city values span 3.84-16.81
# and the daily values span 0.1-38.84. The band brackets the corpus's daily span with room on
# either side; a day outside it is a broken derivation or a broken floor, not traffic.
HISTORICAL_NATIONAL_RANGE = (0.5, 40.0)

# The index is sampled 12x/day on even local hours (`slots.SLOT_HOURS`); a day missing any of its
# twelve slots is not a day.
SLOTS_REQUIRED = 12

# Of the day's own expected captures, not of the registry: 89 of 95 zones publish, and a
# `--limit` run's `slots.expected` is smaller still.
MIN_CAPTURE_RATIO = 0.95

SELECT_SLOTS = """
select count(*), coalesce(sum(captured), 0), coalesce(sum(expected), 0)
from slots
where slot_ts = any(%s)
"""

# Zones measured that local day whose total observed metres are zero: every row is a crashed (or
# zero-length) extraction. `observed_m` is not promoted in `measurements` — it is the sum of the
# four colour lengths, the same expression `derive.rows_from_clean` uses.
SELECT_ZERO_COVERAGE = """
select z.zone_id
from measurements m
join zones z using (zone_id)
where (m.slot_ts at time zone %s)::date = %s
group by z.zone_id
having coalesce(sum(coalesce(m.green_m, 0) + coalesce(m.yellow_m, 0)
                    + coalesce(m.red_m, 0) + coalesce(m.dark_red_m, 0)), 0) = 0
order by z.zone_id
"""

SELECT_NATIONAL = "select national_ci from index_daily where day = %s"

# The day's errored measurement rows: the pipeline turns an image it could not decode into a row
# (not an exception) and its colour metres are all 0. The derived frames drop those rows
# (`night.SELECT_CLEAN`, `publish.SELECT_HISTORY`), so this count is how many the day lost. It is
# REPORTED, never a rule: one unreadable PNG must not cost the day's index (the day-level gate
# stays the capture ratio), and the zone-level `zero_coverage_zones` rule still catches a zone
# that was all-errored, because that rule reads `measurements` directly.
SELECT_ERROR_ROWS = """
select count(*)
from measurements m
where (m.slot_ts at time zone %s)::date = %s
  and coalesce(m.error, '') <> ''
"""


def check_day(day, historical, slots_required, min_capture_ratio):
    """Judge a day summary; returns `(ok, reasons)` and never raises.

    Every broken rule appends its reason rather than short-circuiting, so one run's alert carries
    everything that is wrong with the day. A good day returns `(True, [])`.
    """
    reasons: list[str] = []

    if day["slots_present"] < slots_required:
        reasons.append(f"only {day['slots_present']} of {slots_required} slots present")

    ratio = day["capture_ratio"]
    # `not (ratio >= min)` rather than `ratio < min`: a NaN ratio fails every comparison and must
    # read as a rejection, not as a pass.
    if not (ratio >= min_capture_ratio):
        reasons.append(f"capture ratio {ratio:.3f} below {min_capture_ratio}")

    zero = day["zero_coverage_zones"]
    if zero:
        reasons.append(f"zero observed coverage in {len(zero)} zone(s): {', '.join(zero)}")

    national = day["national_ci"]
    if national is None or not math.isfinite(national):
        reasons.append(f"national index is missing or not finite ({national!r}) — no aggregates "
                       "were written for the day")
    elif not (historical[0] <= national <= historical[1]):
        reasons.append(f"national index {national} outside the historical range {historical}")

    return (not reasons), reasons


def day_summary(conn, day) -> dict:
    """The numbers a publish decision needs, read back from the database for one local day.

    `slots_present` counts how many of the day's twelve expected slot timestamps have a `slots`
    row; `capture_ratio` is `sum(captured)/sum(expected)` over exactly those rows (`0.0` when
    there are none); `zero_coverage_zones` are the zones measured that day whose day-total
    observed metres are zero; `error_rows` is the day-wide count of measurement rows whose error
    text is non-empty (the rows the derived frames drop — reported, and deliberately not a rule);
    `national_ci` is the day's `index_daily` value, or `None` when the day has no aggregates.
    """
    moments = [datetime(day.year, day.month, day.day, hour, tzinfo=SLOT_TZ).astimezone(timezone.utc)
               for hour in SLOT_HOURS]
    with conn.cursor() as cur:
        cur.execute(SELECT_SLOTS, (moments,))
        present, captured, expected = cur.fetchone()
        cur.execute(SELECT_ZERO_COVERAGE, (str(SLOT_TZ), day))
        zero = [row[0] for row in cur.fetchall()]
        cur.execute(SELECT_ERROR_ROWS, (str(SLOT_TZ), day))
        error_rows = cur.fetchone()[0]
        cur.execute(SELECT_NATIONAL, (day,))
        national = cur.fetchone()

    return {
        "slots_present": int(present),
        "capture_ratio": (float(captured) / float(expected)) if expected else 0.0,
        "zero_coverage_zones": zero,
        "error_rows": int(error_rows),
        "national_ci": None if national is None else float(national[0]),
    }
