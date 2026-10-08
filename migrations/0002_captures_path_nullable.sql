-- 0002_captures_path_nullable.sql
-- A capture that failed still gets a row (a coordinate must never be a silent
-- hole) but has no file, so NULL is the honest value for `path`. `error` carries
-- the reason.
alter table captures alter column path drop not null;
