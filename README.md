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
