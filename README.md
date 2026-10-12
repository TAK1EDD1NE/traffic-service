# traffic-service

Capture + nightly pipeline for the live traffic congestion index.

Part of the live-index work described in
`../docs/superpowers/specs/2026-10-03-live-index-architecture-design.md`.

## Local development

    docker compose up -d --wait db        # Postgres 16 on localhost:5433
    DATABASE_URL=postgresql://traffic@localhost:5432/traffic uv run --extra dev --extra pipeline pytest -q

The suite needs **both** extras: `dev` carries pytest, `pipeline` carries pandas (a runtime
import of `derive`/`night`/`publish`, so the worker image installs it too). Either point
`DATABASE_URL` at the Docker database on 5433 (the default above) or at the native one on 5432.

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
- The whole run holds an advisory lock (`night:<day>`), taken inside the job's
  `job_runs` row; a second run of the same day is refused rather than allowed to
  shadow the first, records `ok = true` with `metrics {"skipped": "locked"}`, and
  the CLI exits **0** — a cron overlap is not a failure.
- Every attempt writes a `job_runs` row: `ok` is false only when the day itself failed
  (an aborted publish is the one such case here).
- The prune runs after a publish that succeeded and outside the `job_runs` row: a
  prune that fails prints a WARNING to stderr and still exits **0**, because the day
  is on the site. The 03:30 `maintenance` pass re-attempts it on its own row.

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
only after a day has published; every database table is retained indefinitely. A prune
that fails never fails the night (see above) — it is `maintenance`'s job to age the
pixels out, and that is where a persistent prune failure shows up.

    uv run traffic-service maintenance --raw-root /data/raw

`prune.prune_raw` removes slot directories older than a **48 h** grace window. The
clock is the **slot named by the directory** (`<YYYY-MM-DD>T<HH>-MM`), never the file
mtime: a copy, an rsync, or a restore from backup resets mtimes, and an mtime clock
would then hold pixels forever (or delete a fresh slot whose mtime came from an old
backup). The directory-name parse lives once, in `slots.slot_start_from_name`, and both
the night walk and the prune delegate to it. Maintenance also `VACUUM (ANALYZE)`s
`measurements` and prints the last seven days of slot status and capture counts. It
prunes on the slot clock while holding only its own lock, so an operator backfill of a
day older than 48 h (`night --day <d>`) must be run with the scheduler stopped.

## Scheduling

`scheduler` runs all 14 jobs in one process on the Algiers clock: the **12 capture
slots** on even hours, the **night** pass at **00:30** (closing yesterday), and
**maintenance** at **03:30**. Jobs run sequentially on a single worker.

## The zone registry

`zones` holds one row per `locations.json` coordinate: **95 zones across 27 cities**.

- `zone_id` is `<city_slug>_<area_no>` — the same string the scraper puts in PNG
  filenames, so a captured file maps to a zone with no lookup table.
- **89 zones are published.** The other 6 (`Alger_14/15/16`, `Constantine_2/3`,
  `Annaba_3`) are the excluded quiet areas: **never captured** — `capture.load_zones` reads
  only `publish = true`, so they have no PNGs and no `measurements` rows at all — and
  `capacity_k`/`floor_p20` are NULL because they are absent from `traffic_areas.csv`. (The
  spec's wording once implied they were captured but unpublished; the code is the registry
  filter above, and the spec-vs-code question is open with the owner.)
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

## Deploy

Production is one Coolify **Docker Compose** resource (`deploy/compose.prod.yml`) named
`traffic-index`: three containers `db` / `worker` / `web`, one shared data volume, no published
ports (Coolify/Traefik terminates TLS and routes the domain to `web`). Both images are built
and pushed from the dev box — Coolify only pulls.

    docker login ghcr.io -u TAK1EDD1NE                    # PAT with write:packages
    bash deploy/build-worker.sh <tag> --push              # image A (~4.64 GB, Chromium baked in)
    bash deploy/build-web.sh    <tag> --push              # image B (~331 MB)
    # then set TRAFFIC_WORKER_IMAGE / TRAFFIC_WEB_IMAGE to <tag> in the Coolify env and redeploy

The full operator procedure — VPS prerequisites, GHCR auth, the Coolify resource and its
dashboard restriction, the first bring-up (including the fresh-volume bootstrap), acceptance,
alerts, backups/restore, upgrades and troubleshooting — is in
[`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).
