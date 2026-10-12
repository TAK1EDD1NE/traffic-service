# traffic-service/src/traffic_service/publish.py
"""Publish the site's nine CSVs: validate the day, rebuild the whole series, hash the result.

The publisher is the last step of the night job and the only writer of the site's data directory.
It runs in four moves:

1. **Validate.** `validate.day_summary` reads the day back from the DB and `validate.check_day`
   judges it; a day that fails raises `PublishAborted` and touches nothing. The caller (the
   `night` command, Task 6) writes the `job_runs` row and raises the alarm — this module has no
   channel of its own.
2. **Materialise the five v2 CSVs.** `regenerate_lovable_csv.py` — the site's own generator, in
   the unversioned `data engineering/` directory — reads `traffic_clean|rows|daily|areas|cities`
   CSVs and knows nothing about Postgres. The publisher derives those five from the **whole**
   `measurements` history (R10: the site's tables are a function of the DB's series, so a nightly
   publish refreshes the entire series, not one day) into a scratch directory. The clean frame is
   the measurements joined to `zones` on the slot's local wall clock — the same shape
   `night.clean_frame` reads — and the frozen floors come from the registry (`zones where
   publish`, R8: K and the floor are never re-derived here).
3. **Run the generator into a staging directory, check its work, then swap.** The generator is
   invoked with `sys.executable` and three environment variables: `TRAFFIC_CSV_BASE` (the scratch
   dir), `TRAFFIC_CSV_OUT` (a `.staging-<day>-*` directory inside `out_dir`, so the swap below is
   a same-filesystem move) and `TRAFFIC_POPULATIONS_CSV` (the committed populations seed). The
   nine CSVs are checked **there** — any of the nine missing (or written empty) aborts and the
   staging directory is removed, leaving `out_dir` exactly as it was. R13: only once the set is
   complete are the nine CSVs moved into `out_dir` (`os.replace`, atomic per file) and
   `manifest.json` written **last** — the manifest is what makes the site re-fetch, so a
   half-written set must never look fresh, and a generator that dies mid-run must never splice
   new and previous CSVs under the previous manifest.
4. **Write the manifest.** `data_version` is a **content hash** over the nine CSVs' bytes in
   sorted-name order (`<day>-<sha256[:12]>`), never a timestamp: the site re-fetches when the
   hash changes, and a timestamp would make every publish look like new data. `generated_at` is
   informational and never feeds the hash; `counts` is the per-CSV row count, header excluded.

Two failure classes are asserted up front because their defaults are silent:

- The generator must be the **parameterised** copy (`TRAFFIC_CSV_BASE`/`TRAFFIC_CSV_OUT`/
  `TRAFFIC_POPULATIONS_CSV` read with `os.environ.get`). `data engineering/` is tracked by no
  repository, so an image built against the original hardcoded file would publish with the
  fallback population without erroring — `_assert_regenerator_supports_env` refuses instead.
- The floors frame must be non-empty and every published area's `capacity_m`/`floor_p20` finite
  (R16): a NULL floor turns a whole area into NaN, and NaN passes the `not null` under psycopg.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import pandas as pd

from traffic_service import derive, night, pipeline, validate
from traffic_service.slots import SLOT_TZ

__all__ = ["CSV_NAMES", "DEFAULT_POPULATIONS", "PublishAborted", "REGENERATOR", "publish_day"]

# The site's nine data files — the exact set `regenerate_lovable_csv.py` writes, in the order
# their content hash is taken (sorted by name). The manifest is deliberately NOT in this tuple:
# it is written last, from this set's bytes.
CSV_NAMES = ("areas.csv", "cities.csv", "color_composition.csv", "daily.csv", "hourly.csv",
             "hourly_delta.csv", "monthly.csv", "road_classes.csv", "road_classes_hourly.csv")

# The generator this publisher drives, and the populations seed it must be handed. The
# generator's own default populations source is `OUT/cities.csv`, which does not exist on a first
# publish on a fresh host (pre-flight finding 3) — without the seed every city would silently
# publish at the fallback 200000.
REGENERATOR = pipeline.DATA_ENGINEERING / "regenerate_lovable_csv.py"
DEFAULT_POPULATIONS = (pathlib.Path(__file__).resolve().parents[2] / "data"
                       / "cities_population.csv")

# The five v2 frames the generator reads, in the corpus CSV names it expects under BASE.
_V2_FILES = (("clean", "traffic_clean.csv"), ("rows", "traffic_rows.csv"),
             ("daily", "traffic_daily.csv"), ("areas", "traffic_areas.csv"),
             ("cities", "traffic_cities.csv"))

# The clean frame's metric columns are the measurements table's own names: `derive.CLEAN_COLUMNS`
# minus the four leading keys, which come from the zone and the slot's local clock.
_HISTORY_METRICS = derive.CLEAN_COLUMNS[4:]

# `night.clean_frame`'s read without the day filter: the whole history, on the same local clock
# and in the corpus's text formats (`YYYY-MM-DD`, `HH:00`). Errored rows are excluded here too:
# a row whose pixels could not be read (all colour metres 0, so a meaningless index) must not
# contribute a false calm to the series the site is rebuilt from.
SELECT_HISTORY = f"""
select z.city,
       z.area_no as area,
       to_char(m.slot_ts at time zone %s, 'YYYY-MM-DD') as date,
       to_char(m.slot_ts at time zone %s, 'HH24') || ':00' as time,
       {", ".join(f"m.{column}" for column in _HISTORY_METRICS)}
from measurements m
join zones z using (zone_id)
where coalesce(m.error, '') = ''
order by z.city, z.area_no, m.slot_ts
"""


class PublishAborted(RuntimeError):
    """The day was not published: validation failed, the shipping contract is not met, or the
    generator left the set incomplete. No manifest is written, so nothing half-written is ever
    served as fresh, and `out_dir` keeps the previous manifest. What is *not* guaranteed is that
    no CSV moved: the nine are moved only once the set is verified complete in the staging
    directory, but that swap is one `os.replace` per file, so a failure part-way through it can
    leave some of the nine already replaced (the next publish rewrites the whole set)."""


def _assert_regenerator_supports_env(script_path: pathlib.Path) -> None:
    """The publisher is parameterised by env vars. If the copy deployed alongside the service is
    the original hardcoded one, every city would silently publish at the fallback population, so
    refuse to publish instead."""
    src = pathlib.Path(script_path).read_text(encoding="utf-8")
    missing = [v for v in ("TRAFFIC_CSV_BASE", "TRAFFIC_CSV_OUT", "TRAFFIC_POPULATIONS_CSV")
               if v not in src]
    if missing:
        raise PublishAborted(
            f"{script_path} does not honour {missing} — it is the unparameterised original, "
            "so publishing would fall back to default populations. Ship the parameterised copy.")


def _floors_frame(conn) -> pd.DataFrame:
    """The frozen per-area floors, read back from the registry — `night`'s own read (R8)."""
    with conn.cursor() as cur:
        cur.execute(night.SELECT_FLOORS)
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=list(derive.FLOOR_COLUMNS))


def _assert_floors_are_usable(floors) -> None:
    """R16: no floors at all, or a missing/non-finite K or floor, means NaN indices — silently."""
    if floors.empty:
        raise PublishAborted("the registry has no published areas, so the floors frame is empty "
                             "and every index would be NaN — seed the zones before publishing")
    for row in floors.itertuples():
        for column in ("capacity_m", "freeflow"):
            value = getattr(row, column)
            if value is None or not math.isfinite(float(value)):
                raise PublishAborted(
                    f"{row.city} area {row.area}: {column} is {value!r} — a missing or "
                    "non-finite floor turns the area into NaN, and NaN passes the `not null` "
                    "under psycopg")


def _history_frame(conn) -> pd.DataFrame:
    """The whole `measurements` history as the derivation's clean frame (R10)."""
    tz = str(SLOT_TZ)
    with conn.cursor() as cur:
        cur.execute(SELECT_HISTORY, (tz, tz))
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=list(derive.CLEAN_COLUMNS))


def _write_v2_csvs(conn, floors, base_dir: pathlib.Path) -> None:
    """Derive the five v2 frames and write them where the generator will read them."""
    base_dir.mkdir(parents=True, exist_ok=True)
    clean = _history_frame(conn)
    if clean.empty:
        raise PublishAborted("measurements is empty — the five v2 CSVs would have no rows, so "
                             "there is no series to publish")
    rows = derive.rows_from_clean(clean, floors)
    areas = derive.areas_from_rows(rows, floors)
    frames = {
        "clean": clean,
        "rows": rows,
        "daily": derive.daily_from_rows(rows, floors),
        "areas": areas,
        "cities": derive.cities_from_areas(areas),
    }
    for name, filename in _V2_FILES:
        frames[name].to_csv(base_dir / filename, index=False)


def _run_regenerator(script_path, base_dir, out_dir, populations) -> str:
    """Run the generator with BASE/OUT/populations pointed at this publish; return its stdout."""
    env = {**os.environ,
           "TRAFFIC_CSV_BASE": str(base_dir),
           "TRAFFIC_CSV_OUT": str(out_dir),
           "TRAFFIC_POPULATIONS_CSV": str(populations)}
    done = subprocess.run([sys.executable, str(script_path)], env=env,
                          capture_output=True, text=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise PublishAborted(f"{script_path} exited {done.returncode}:\n{detail[-2000:]}")
    return done.stdout or ""


def _row_count(path: pathlib.Path) -> int:
    """Data rows in a CSV: every line but the header."""
    with pathlib.Path(path).open(encoding="utf-8", newline="") as handle:
        return sum(1 for _ in handle) - 1


def _data_version(day, out_dir) -> str:
    """R14: sha256 over the nine CSVs' bytes in sorted-name order, first 12 hex, day-prefixed."""
    digest = hashlib.sha256()
    for name in sorted(CSV_NAMES):
        digest.update((pathlib.Path(out_dir) / name).read_bytes())
    return f"{day}-{digest.hexdigest()[:12]}"


def publish_day(db, day, out_dir, populations=None) -> dict:
    """Validate `day`, rebuild the whole series, and write the nine CSVs plus `manifest.json`.

    `populations` is a `city,population` CSV, defaulting to the committed
    `data/cities_population.csv`. Returns `{data_version, day, counts, csvs, error_rows}` — the
    last is the day's errored-measurement count from the validation summary, carried so the
    `night` report can show how many rows the clean frames dropped. Raises
    `PublishAborted` (writing no manifest) for a day that fails validation, a missing populations
    seed, unusable floors, an empty `measurements` history, a generator failure, or a set the
    generator left incomplete.
    """
    _assert_regenerator_supports_env(REGENERATOR)

    summary = validate.day_summary(db, day)
    ok, reasons = validate.check_day(summary, historical=validate.HISTORICAL_NATIONAL_RANGE,
                                     slots_required=validate.SLOTS_REQUIRED,
                                     min_capture_ratio=validate.MIN_CAPTURE_RATIO)
    if not ok:
        raise PublishAborted(f"{day} failed validation: " + "; ".join(reasons))

    populations = pathlib.Path(populations if populations is not None else DEFAULT_POPULATIONS)
    if not populations.is_file():
        raise PublishAborted(
            f"{populations} is missing — the generator reads populations from it (its own read is "
            "existence-guarded), so without it every city would silently publish as 200000")

    floors = _floors_frame(db)
    _assert_floors_are_usable(floors)

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="traffic-publish-") as scratch:
        scratch_dir = pathlib.Path(scratch)
        _write_v2_csvs(db, floors, scratch_dir)
        # The generator writes into a staging directory on `out_dir`'s own filesystem, and the
        # nine CSVs move in only once the set is complete: a generator that dies mid-run (disk
        # full, pandas error, killed process) then leaves `out_dir` exactly as it was — never a
        # splice of new CSVs under the previous manifest. `os.replace` below is same-filesystem,
        # so each move is atomic per file.
        staging = pathlib.Path(tempfile.mkdtemp(dir=out, prefix=f".staging-{day}-"))
        try:
            _run_regenerator(REGENERATOR, scratch_dir, staging, populations)

            missing = [name for name in CSV_NAMES
                       if not (staging / name).is_file()
                       or (staging / name).stat().st_size == 0]
            if missing:
                raise PublishAborted(
                    f"the generator left {missing} missing or empty in its staging directory "
                    f"{staging} — the set is incomplete, so nothing is moved into {out} and no "
                    "manifest is written; the site cannot see it")

            # The nine CSVs first (R13): each an atomic same-filesystem replace, so the swap is
            # a rename, not a rewrite.
            for name in CSV_NAMES:
                os.replace(staging / name, out / name)
        finally:
            # Best-effort: an abort must leave nothing behind and a success leaves it empty.
            shutil.rmtree(staging, ignore_errors=True)

    # The manifest LAST (R13): it is the only file that makes the site re-fetch.
    counts = {name: _row_count(out / name) for name in CSV_NAMES}
    data_version = _data_version(day, out)
    manifest = {
        "data_version": data_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day": str(day),
        "counts": counts,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"data_version": data_version, "day": day, "counts": counts,
            "csvs": sorted(CSV_NAMES), "error_rows": summary["error_rows"]}
