# traffic-service/src/traffic_service/night.py
"""The night pass: measure every image of one closed day of capture slots.

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

from psycopg.types.json import Jsonb

from traffic_service import metrics
from traffic_service.slots import SLOT_HOURS, SLOT_TZ

__all__ = ["images_for", "json_safe", "measure_day", "slot_dirs", "zone_id_for"]

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
