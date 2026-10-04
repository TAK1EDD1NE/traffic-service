-- 0001_init.sql — the whole schema for the live index.
--
-- `measurements` stores the extraction row as both promoted columns (what the
-- aggregation queries) and a `payload jsonb` (the whole pipeline row, whatever
-- its width). The pipeline's column set changed once already; a jsonb payload
-- means a new colour class or metric never needs a migration, while the
-- promoted set stays small and indexable.

create table schema_migrations (
    filename    text primary key,
    applied_at  timestamptz not null default now()
);

create table zones (
    zone_id       text primary key,              -- '<city_slug>_<area_no>'
    city          text not null,                 -- de-accented name, joins the corpus CSVs
    city_slug     text not null,                 -- slug as it appears in PNG filenames
    area_no       integer not null,              -- 1-based; 1 for single-coordinate cities
    name          text not null,                 -- locations.json display name (accented)
    lat           double precision not null,
    lng           double precision not null,
    scale         double precision not null,
    publish       boolean not null default true, -- false for the excluded quiet areas
    capacity_k    double precision,              -- p99 observed metres = traffic_areas.capacity_m
    floor_p20     double precision,              -- p20 of ci_raw = traffic_areas.freeflow
    floor_version text,
    unique (city_slug, area_no)
);

create table slots (
    slot_ts     timestamptz primary key,         -- top of an even local hour, stored UTC
    expected    integer not null,
    captured    integer not null default 0,
    status      text not null default 'running'
                check (status in ('running', 'ok', 'partial', 'overrun', 'failed')),
    started_at  timestamptz not null default now(),
    finished_at timestamptz
);

create table captures (
    zone_id     text not null references zones (zone_id),
    slot_ts     timestamptz not null references slots (slot_ts),
    path        text not null,
    bytes       bigint,
    error       text,
    captured_at timestamptz not null default now(),
    primary key (zone_id, slot_ts)
);

create table measurements (
    zone_id      text not null,
    slot_ts      timestamptz not null,
    observed_m   double precision,
    gridlock_eq  double precision,
    ci_raw       double precision,
    freeflow     double precision,
    ci           double precision,
    green_pct    double precision,
    yellow_pct   double precision,
    red_pct      double precision,
    dark_red_pct double precision,
    low_coverage boolean,
    error        text,
    payload      jsonb not null,
    primary key (zone_id, slot_ts),
    foreign key (zone_id, slot_ts) references captures (zone_id, slot_ts)
);

create table ci_area_daily (
    zone_id  text not null references zones (zone_id),
    day      date not null,
    ci       double precision not null,
    coverage double precision,
    n_rows   integer not null,
    primary key (zone_id, day)
);

create table ci_city_daily (
    city     text not null,
    day      date not null,
    ci       double precision not null,
    coverage double precision,
    n_areas  integer not null,
    primary key (city, day)
);

create table index_daily (
    day         date primary key,
    national_ci double precision not null,
    n_cities    integer not null,
    n_zones     integer not null,
    coverage    double precision
);

create table job_runs (
    id          bigserial primary key,
    job         text not null,
    target      text,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    ok          boolean,
    metrics     jsonb,
    error       text
);

create index job_runs_job_started_idx on job_runs (job, started_at desc);
create index captures_slot_idx on captures (slot_ts);
create index measurements_zone_slot_idx on measurements (zone_id, slot_ts);
