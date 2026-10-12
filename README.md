# traffic-service

Capture + nightly pipeline for the live traffic congestion index.

Part of the live-index work described in
`../docs/superpowers/specs/2026-10-03-live-index-architecture-design.md`.

## Local development

    docker compose up -d --wait db        # Postgres 16 on localhost:5433
    uv run --extra dev pytest             # tests need a database up

`DATABASE_URL` overrides the default `postgresql://traffic:traffic@localhost:5433/traffic`.
Port 5433 is deliberate — 5432 is commonly taken on this machine (a native
PostgreSQL may already be listening there; point `DATABASE_URL` at it to run the
tests without Docker).

The interpreter is pinned by `.python-version` to 3.12, matching the pipeline's
own virtualenv.

## Commands

    uv run traffic-service migrate        # apply pending SQL migrations
    uv run traffic-service seed-zones     # upsert the zone registry from the corpus
    uv run traffic-service doctor         # schema + registry health; exits 1 when unhealthy
    uv run traffic-service night          # close one day: measure -> publish -> prune
    uv run traffic-service maintenance    # prune raw PNGs, VACUUM, report slot health

## The night pipeline

    uv run traffic-service night --day yesterday                 # the default
    uv run traffic-service night --day 2026-10-04 --out-dir /data/out --workers 4

`night` closes one day: it measures the day's twelve slot directories into
`measurements`, runs the v2 chain over them (`ci_area_daily`, `ci_city_daily`,
`index_daily`), **validates** the day, and only then publishes. The validation rules
(slot count, capture ratio, zero-coverage zones, a finite national index) live in
`validate.py`; a day that fails publishes nothing, prunes nothing, prints the reasons
to stderr, and exits **1** — that exit code is the alerting hook.

- `--day` is `yesterday` (the local day before today in `Africa/Algiers`) or
  `YYYY-MM-DD`. A day is always local: the 00:00 slot is 23:00 UTC the day before.
- `--raw-root` defaults to `/data/raw`, `--out-dir` to `/data/out`.
- The whole run holds an advisory lock (`night:<day>`); a second run of the same day
  is refused rather than allowed to shadow the first.
- Every attempt writes a `job_runs` row: `ok` is true only when the day published.

### The publish contract

`publish.publish_day` writes the site's **nine CSVs** into `--out-dir` from the whole
`measurements` history (a nightly publish refreshes the entire series, not one day),
then writes `manifest.json` **last** — it is the only file that makes the site
re-fetch.

    {
      "data_version": "2026-10-04-1a2b3c4d5e6f",
      "generated_at": "2026-10-04T00:42:11+00:00",
      "day": "2026-10-04",
      "counts": { "cities.csv": 27, ... }
    }

`data_version` is a **content hash** (`<day>-<sha256(9 CSVs)[:12]>`), never a
timestamp, so an unchanged publish keeps the same version and the site does not
re-fetch. `generated_at` is informational and never feeds the hash. The CSVs are built
in a staging directory on `--out-dir`'s own filesystem and moved in atomically, so a
generator that dies mid-run leaves the previous good set (and its manifest) untouched.

## Retention

**Pixels are transient; `measurements` is forever.** Only the raw PNGs are pruned, and
only after a day has published; every database table is retained indefinitely.

    uv run traffic-service maintenance --raw-root /data/raw

`prune.prune_raw` removes slot directories older than a **48 h** grace window. The
clock is the **slot named by the directory** (`<YYYY-MM-DD>T<HH>-MM`), never the file
mtime: a copy, an rsync, or a restore from backup resets mtimes, and an mtime clock
would then hold pixels forever (or delete a fresh slot whose mtime came from an old
backup). Maintenance also `VACUUM (ANALYZE)`s `measurements` and prints the last seven
days of slot status and capture counts.

## Scheduling

`scheduler` runs all 14 jobs in one process on the Algiers clock: the **12 capture
slots** on even hours, the **night** pass at **00:30** (closing yesterday), and
**maintenance** at **03:30**. Jobs run sequentially on a single worker.

## The zone registry

`zones` holds one row per `locations.json` coordinate: **95 zones across 27 cities**.

- `zone_id` is `<city_slug>_<area_no>` — the same string the scraper puts in PNG
  filenames, so a captured file maps to a zone with no lookup table.
- **89 zones are published.** The other 6 (`Alger_14/15/16`, `Constantine_2/3`,
  `Annaba_3`) are the excluded quiet areas: still captured, `publish = false`,
  `capacity_k`/`floor_p20` NULL because they are absent from `traffic_areas.csv`.
- `capacity_k` (= `traffic_areas.capacity_m`, the p99 observed metres) and
  `floor_p20` (= `traffic_areas.freeflow`) are **read from the corpus, never
  re-derived here**, and stamped with `floor_version`. Re-seeding with a different
  `--floor-version` is how a formula review is shipped.
- Names come in two forms and the join is on the de-accented one:
  `Aïn Témouchent` (locations.json) → `Ain Temouchent` (corpus) → `A_n_T_mouchent` (slug).

## Layout

    migrations/     ordered .sql, applied once each and recorded in schema_migrations
    src/traffic_service/
        db.py       how to reach Postgres
        migrate.py  the migration runner
        zones.py    the two city-name transforms
        seed.py     corpus -> zones
        jobs.py     advisory lock + job_runs audit rows
        cli.py      the only entry point

## Capturing

    uv run traffic-service capture --slot now                 # the current slot
    uv run traffic-service capture --slot 2026-10-05T02:00    # a specific slot (local)
    uv run traffic-service capture --slot now --limit 5       # smoke test
    uv run traffic-service scheduler                          # the 12-per-day loop

- Slot timestamps are **local (Africa/Algiers)**, on even hours; the database
  stores UTC.
- A PNG is `<zone_id>_<local slot time>.png` under `<raw-root>/<slot>`, so a
  re-run overwrites the same file and the slot is resumable.
- A zone that failed gets a row with `path = NULL` and `error` set — visible, not
  a hole — and is retried on the next run of that slot. `--force` re-captures
  everything.
- The slot holds an advisory lock; a second concurrent run is refused and
  changes nothing.
- `capture` exits **1** when a slot is not fully captured, so a scheduler wrapper
  or an operator can see it.
