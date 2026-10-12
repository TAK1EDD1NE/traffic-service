# traffic-service/src/traffic_service/night.py
"""The night pass: measure a closed day's capture slots, then roll the day's index up.

The capture job writes `<raw_root>/<slot_key>/<zone_id>_<slot_label>.png`; this module walks a
day's twelve even-hour slot directories, resolves each PNG back to a registry zone id (which is
why the corpus's one unindexed city, `Djelfa`, still lands on `Djelfa_1`), and writes one
`measurements` row per image — the promoted columns, plus the extractor's full row in `payload`.
A slot with zero images is measured as nothing, not treated as an error: whether a thin day may
publish is Task 5's validation — this job reports counts, it does not judge.

The counts are the job's only voice, so the semantics are exact:

- `images`   — every PNG found in the day's slot directories, whatever happens to it.
- `measured` — rows written by this run, one upsert per image not already measured.
- `skipped`  — images whose `(zone_id, slot_ts)` already has a `measurements` row (error rows
  included), so a re-run measures nothing twice.
- `errors`   — written rows whose promoted `error` column is non-empty; the pipeline turns a
  broken image into a row, not an exception, and this is how many it turned.
- `slots`    — slot directories processed for the day.
- `unknown`  — image names whose zone id is not in the registry. Counted and reported, never
  inserted: the `measurements` FK would reject them, and a crash loop is worse than a counted miss.

`run_night` is the whole flow for a closed day: `measure_day`, then `clean_frame` (the day's
measurements read back as the derivation's clean frame — the slot's **local** clock, so the 00:00
slot, which is 23:00 UTC the day before, belongs to its own day), then `aggregate_day`, which runs
the v2 chain (`derive`) over that frame and upserts the three rollups. The frozen floors come from
the registry (`zones where publish`), never from a re-derivation (R8): K and the floor ship from
`traffic_areas.csv` through the seed, and a floor review is a re-seed, not a code change.

The rollup write sites, in the plan's semantics:

- `ci_area_daily` gets one row per area that has measurements that day; `coverage` is the area's
  mean row weight (`observed_m / capacity_m`) — a diagnostic, not published data.
- `ci_city_daily` gets one row per city: the coverage-weighted daily index `derive` computes, the
  city's mean row weight, and its area count.
- `index_daily.national_ci` is the capacity-weighted mean of the day's city values, each city
  weighted by its own total capacity — the same weighting the city rollup uses; `n_zones` counts
  the area rows the index was computed from.

Every rollup write is `on conflict ... do update`, so a re-run of a day rewrites its own rows and
can never duplicate or splice a series (spec:120-122); `aggregate_day` commits once, so the day's
three tables land together or not at all. A day with no measurements writes nothing and reports
zero counts — whether a thin day may publish is Task 5's validation, not this job's call.

The failure model is the capture job's. A `slots` row is upserted for every processed slot with
`on conflict do nothing`, so a real capture's counts are never clobbered; measurement rows are
committed per slot, so a crash costs one slot rather than the day; an interrupted run is safe to
re-run. The extractor is injected exactly as in `metrics.measure_slot` — production passes the
default, tests pass a fake and skip the ~35 s/image pixel work.
"""
from __future__ import annotations

import math
import pathlib
import re
from datetime import datetime, timezone

import pandas as pd
from psycopg.types.json import Jsonb

from traffic_service import derive, metrics
from traffic_service.slots import SLOT_HOURS, SLOT_TZ

__all__ = ["aggregate_day", "clean_frame", "images_for", "json_safe", "measure_day",
           "run_night", "slot_dirs", "zone_id_for"]

_SLOT_NAME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}$")
_STAMP = re.compile(r"_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}$")

# The SQL lives at the top and the parameters are the only variables — the capture module's shape.
UPSERT_SLOT = """
insert into slots (slot_ts, expected)
values (%s, %s)
on conflict (slot_ts) do nothing
"""

SELECT_MEASURED = """
select zone_id, slot_ts from measurements where slot_ts = any(%s)
"""

_COLUMNS = ", ".join(metrics.MEASUREMENT_COLUMNS)
_PLACEHOLDERS = ", ".join(["%s"] * len(metrics.MEASUREMENT_COLUMNS))
_UPDATES = ", ".join(f"{column} = excluded.{column}" for column in metrics.MEASUREMENT_COLUMNS)

UPSERT_MEASUREMENT = f"""
insert into measurements (zone_id, slot_ts, {_COLUMNS}, payload)
values (%s, %s, {_PLACEHOLDERS}, %s)
on conflict (zone_id, slot_ts) do update set processed_at = now(),
                                            {_UPDATES},
                                            payload = excluded.payload
"""

# The clean frame's metric columns are the measurements table's own names: `derive.CLEAN_COLUMNS`
# minus the four leading keys, which come from the zone and the slot's local clock.
_CLEAN_METRICS = derive.CLEAN_COLUMNS[4:]

SELECT_CLEAN = f"""
select z.city,
       z.area_no as area,
       to_char(m.slot_ts at time zone %s, 'YYYY-MM-DD') as date,
       to_char(m.slot_ts at time zone %s, 'HH24') || ':00' as time,
       {", ".join(f"m.{column}" for column in _CLEAN_METRICS)}
from measurements m
join zones z using (zone_id)
where (m.slot_ts at time zone %s)::date = %s
order by z.city, z.area_no, m.slot_ts
"""

# R8: the frozen floors come from the registry — the numbers `seed.py` loaded from
# `traffic_areas.csv` — and go into `derive` as `capacity_m` / `freeflow`, never re-derived.
SELECT_FLOORS = """
select city, area_no as area, capacity_k as capacity_m, floor_p20 as freeflow
from zones
where publish
"""

UPSERT_AREA = """
insert into ci_area_daily (city, area, day, ci, coverage, n_rows)
values (%s, %s, %s, %s, %s, %s)
on conflict (city, area, day) do update set
    ci = excluded.ci, coverage = excluded.coverage, n_rows = excluded.n_rows
"""

UPSERT_CITY = """
insert into ci_city_daily (city, day, ci, coverage, n_areas)
values (%s, %s, %s, %s, %s)
on conflict (city, day) do update set
    ci = excluded.ci, coverage = excluded.coverage, n_areas = excluded.n_areas
"""

UPSERT_INDEX = """
insert into index_daily (day, national_ci, n_cities, n_zones, coverage)
values (%s, %s, %s, %s, %s)
on conflict (day) do update set
    national_ci = excluded.national_ci, n_cities = excluded.n_cities,
    n_zones = excluded.n_zones, coverage = excluded.coverage
"""


def _parse_slot(name: str) -> datetime | None:
    """`<YYYY-MM-DD>T<HH>-00` -> the slot start as aware local time, else `None`.

    The regex cannot rule out an impossible date (month 13), so the parse is guarded too, and
    the even-hour/minute check is what keeps a stray `T01-00` directory out.
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


def slot_dirs(day, raw_root) -> list[pathlib.Path]:
    """The day's slot directories, in slot order — only real `<day>T<even hour>-00` names."""
    root = pathlib.Path(raw_root)
    if not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        start = _parse_slot(entry.name)
        if start is not None and start.date() == day:
            found.append(entry)
    return found


def images_for(slot_dir) -> list[pathlib.Path]:
    """Every `*.png` in a slot directory, in filename order."""
    return sorted(pathlib.Path(slot_dir).glob("*.png"))


def zone_id_for(png_name, known=None):
    """The registry zone id a captured PNG belongs to, or `None` if there is none.

    The candidate is the filename stem with the trailing `_<YYYY-MM-DD>_<HH-MM>` stripped — a
    regex anchored at the end, never a split on `_`, because a slug legitimately contains `_`.
    A name without that trailing stamp does not follow the capture convention and cannot be
    attributed to a zone, so it is `None` too. With a `known` registry (as `measure_day` passes)
    the candidate must resolve: an exact match wins, then `<candidate>_1` — how the corpus's one
    unindexed city, `Djelfa`, maps to its single registry area — and anything else is unknown.
    """
    stem = pathlib.Path(png_name).stem
    stamp = _STAMP.search(stem)
    if stamp is None:
        return None
    candidate = stem[: stamp.start()]
    if known is None:
        return candidate
    if candidate in known:
        return candidate
    indexed = f"{candidate}_1"
    return indexed if indexed in known else None


def json_safe(value):
    """Make a row jsonb-safe: every non-finite float becomes `None`, recursively.

    `json.dumps` emits `NaN`/`Infinity` for these and Postgres jsonb rejects them ("invalid
    input syntax for type json"), so a row with an absent colour's NaN would crash the insert.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def measure_day(conn, day, raw_root, extractor=metrics.real_extractor,
                workers=metrics.DEFAULT_WORKERS) -> dict:
    """Measure every image of `day` under `raw_root`; idempotent and safe to re-run.

    Returns the counts spelled out in the module docstring.
    """
    directories = slot_dirs(day, raw_root)
    slot_starts = [(directory, _parse_slot(directory.name)) for directory in directories]

    with conn.cursor() as cur:
        cur.execute("select zone_id from zones")
        known = {row[0] for row in cur.fetchall()}
        cur.execute("select count(*) from zones where publish = true")
        expected = cur.fetchone()[0]

    slot_times = [start.astimezone(timezone.utc) for _, start in slot_starts]
    done: set[tuple[str, datetime]] = set()
    if slot_times:
        # One query per day, not one per image: the skip set is every row already measured for
        # the day's slots.
        with conn.cursor() as cur:
            cur.execute(SELECT_MEASURED, (slot_times,))
            done = {(zone_id, slot_ts) for zone_id, slot_ts in cur.fetchall()}

    images = measured = skipped = errors = 0
    unknown: list[str] = []

    for directory, start in slot_starts:
        slot_ts = start.astimezone(timezone.utc)
        with conn.cursor() as cur:
            cur.execute(UPSERT_SLOT, (slot_ts, expected))
        # The `measurements` FK needs the slots row, and an interrupted slot should stay
        # recognisable — the same reasoning as the capture job's up-front commit.
        conn.commit()

        todo = []
        for path in images_for(directory):
            images += 1
            zone_id = zone_id_for(path.name, known)
            if zone_id is None:
                unknown.append(path.name)
            elif (zone_id, slot_ts) in done:
                skipped += 1
            else:
                todo.append((zone_id, path))

        if todo:
            rows = metrics.measure_slot([path for _, path in todo], extractor=extractor,
                                        workers=workers)
            with conn.cursor() as cur:
                for (zone_id, _), row in zip(todo, rows):
                    promoted = metrics.project(row)
                    values = [zone_id, slot_ts]
                    values.extend(promoted[column] for column in metrics.MEASUREMENT_COLUMNS)
                    values.append(Jsonb(json_safe(row)))
                    cur.execute(UPSERT_MEASUREMENT, values)
                    measured += 1
                    if promoted["error"]:
                        errors += 1
        # Commit per slot: a crash costs one slot, not the day.
        conn.commit()

    return {"images": images, "measured": measured, "skipped": skipped,
            "errors": errors, "slots": len(directories), "unknown": unknown}


def clean_frame(conn, day) -> pd.DataFrame:
    """The day's measurements as the derivation's clean frame: `derive.CLEAN_COLUMNS`, in order.

    One SQL read, joined to `zones` for `city` and `area_no` — a name is never re-derived from a
    filename. `date` and `time` are the slot's **local** wall clock in the corpus CSV's own text
    formats (`YYYY-MM-DD`, `HH:00`), and the same local date is the filter: the 00:00 slot is
    23:00 UTC the day before, so a UTC date would drop a slot of every day.
    """
    tz = str(SLOT_TZ)
    with conn.cursor() as cur:
        cur.execute(SELECT_CLEAN, (tz, tz, tz, day))
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=list(derive.CLEAN_COLUMNS))


def _floors_frame(conn) -> pd.DataFrame:
    """The frozen per-area floors, read back from the registry — never re-derived here (R8)."""
    with conn.cursor() as cur:
        cur.execute(SELECT_FLOORS)
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=list(derive.FLOOR_COLUMNS))


def aggregate_day(conn, day) -> dict:
    """Derive and upsert the day's three rollup tables; idempotent, one transaction per day.

    Returns `{"areas", "cities", "days"}` — the rows written to `ci_area_daily`, `ci_city_daily`
    and `index_daily` (`days` is 1). A day with no measurements — or none that survive the frozen
    floors — writes nothing and reports zeros; whether a thin day may publish is Task 5's call.
    """
    floors = _floors_frame(conn)
    clean = clean_frame(conn, day)
    rows = derive.rows_from_clean(clean, floors) if not clean.empty else clean
    if rows.empty:
        return {"areas": 0, "cities": 0, "days": 0}
    daily = derive.daily_from_rows(rows, floors)
    areas = derive.areas_from_rows(rows, floors)
    cities = derive.cities_from_areas(areas)

    # Coverage is the row weight `observed_m / capacity_m`, averaged at each level — a diagnostic,
    # not published data. `rows` carries no K (it stayed in the floors frame), so this merges it
    # back for exactly this one column.
    weighted = rows.merge(floors[["city", "area", "capacity_m"]], on=["city", "area"])
    weighted["weight"] = weighted["observed_m"] / weighted["capacity_m"]
    area_coverage = weighted.groupby(["city", "area"])["weight"].mean()
    city_coverage = weighted.groupby("city")["weight"].mean()
    index_coverage = float(weighted["weight"].mean())

    # The national index: the day's city values weighted by each city's own total capacity — the
    # same weighting the city rollup uses.
    city_capacity = areas.groupby("city")["capacity_m"].sum()
    national_ci = float((cities.set_index("city")["ci"] * city_capacity).sum()
                        / city_capacity.sum())
    n_areas = areas.groupby("city").size()

    # One commit for the day: the three tables land together or not at all.
    with conn.cursor() as cur:
        for row in areas.itertuples():
            cur.execute(UPSERT_AREA, (row.city, int(row.area), day, float(row.ci),
                                      float(area_coverage[(row.city, row.area)]),
                                      int(row.n_rows)))
        for row in daily.itertuples():
            cur.execute(UPSERT_CITY, (row.city, day, float(row.ci),
                                      float(city_coverage[row.city]), int(n_areas[row.city])))
        cur.execute(UPSERT_INDEX, (day, national_ci, len(cities), len(areas), index_coverage))
    conn.commit()
    return {"areas": len(areas), "cities": len(cities), "days": 1}


def run_night(conn, day, raw_root, extractor=metrics.real_extractor,
              workers=metrics.DEFAULT_WORKERS) -> dict:
    """The whole night flow for one closed day: measure -> clean -> derive -> aggregate.

    Returns the merged summary of both halves (`images`/`measured`/`skipped`/`errors`/`slots`/
    `unknown` plus `areas`/`cities`/`days`). It does not validate or publish — Task 5 does, and
    the split is what lets the tests exercise the math without touching the site's data directory.
    """
    counts = measure_day(conn, day, raw_root, extractor=extractor, workers=workers)
    return {**counts, **aggregate_day(conn, day)}
