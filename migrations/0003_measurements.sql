-- migrations/0003_measurements.sql
-- One row per (zone, slot): the 34 columns the existing pipeline produces, promoted so the
-- index is recomputable by SQL, plus the raw row in `payload` so a future change to the colour
-- segmentation can be re-scored without the pixels (spec:125-129).
--
-- This migration REPLACES the placeholder `measurements` table shipped by 0001_init.sql. That
-- table's promoted columns (observed_m / gridlock_eq / ci_raw / freeflow / ci and the four
-- *_pct columns) belong to the v2 derivation — `derive` computes them from the clean frame, they
-- are never stored here. Because 0001 is already applied, `create table if not exists` would be a
-- silent no-op and leave the superseded schema in place, so the table is dropped and recreated:
-- this file is the single source of truth for the `measurements` shape.
--
-- `{c}_px` and `{c}_pct` are deliberately NOT promoted. They are pure functions of the promoted
-- columns — `px` scales with `m`/`mean_w` and `pct` is a share of the row's own total — so they
-- stay recoverable from `payload` without costing a column each.
drop table if exists measurements;

create table measurements (
    zone_id            text        not null references zones (zone_id),
    slot_ts            timestamptz not null references slots (slot_ts),
    processed_at       timestamptz not null default now(),
    congestion_index   double precision,
    total_meters       double precision,
    total_px_length    double precision,
    total_skel_px      integer,
    low_coverage       boolean,
    total_highway_m    double precision,
    total_normal_m     double precision,
    green_m            double precision, green_mean_w   double precision,
    green_highway_m    double precision, green_normal_m double precision,
    yellow_m           double precision, yellow_mean_w  double precision,
    yellow_highway_m   double precision, yellow_normal_m double precision,
    red_m              double precision, red_mean_w     double precision,
    red_highway_m      double precision, red_normal_m   double precision,
    dark_red_m         double precision, dark_red_mean_w double precision,
    dark_red_highway_m double precision, dark_red_normal_m double precision,
    error              text,
    payload            jsonb       not null,
    primary key (zone_id, slot_ts)
);

create index if not exists measurements_slot_idx on measurements (slot_ts);
