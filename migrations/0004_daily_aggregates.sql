-- migrations/0004_daily_aggregates.sql
-- The daily rollups the site's CSVs are generated from. All three are keyed on the day so a
-- re-run of a day overwrites its own rows and can never splice a series (spec:120-122).
--
-- 0001 shipped placeholder versions of these tables (ci_area_daily keyed by zone_id, with an FK
-- to zones). `create table if not exists` would leave that shape in place and Task 4's inserts
-- would meet the wrong columns, so the tables are dropped and recreated — the same ruling that
-- produced 0003_measurements.sql. They are empty until the first night run.
drop table if exists ci_area_daily;
drop table if exists ci_city_daily;
drop table if exists index_daily;

create table ci_area_daily (
    city text not null, area integer not null, day date not null,
    ci double precision not null, coverage double precision, n_rows integer not null,
    primary key (city, area, day)
);
create table ci_city_daily (
    city text not null, day date not null,
    ci double precision not null, coverage double precision, n_areas integer not null,
    primary key (city, day)
);
create table index_daily (
    day date primary key,
    national_ci double precision not null, n_cities integer not null,
    n_zones integer not null, coverage double precision
);
