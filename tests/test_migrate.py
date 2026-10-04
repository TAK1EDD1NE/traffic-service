import psycopg
import pytest

from traffic_service.db import connect
from traffic_service.migrate import migrate

EXPECTED_TABLES = {
    "zones", "slots", "captures", "measurements",
    "ci_area_daily", "ci_city_daily", "index_daily", "job_runs",
}


@pytest.fixture()
def clean_db():
    """Drop everything, then hand back a connection. Each test starts empty."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        yield conn


def test_migrate_creates_every_table(clean_db):
    applied = migrate(clean_db)
    assert applied == ["0001_init.sql"]
    with clean_db.cursor() as cur:
        cur.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
        tables = {row[0] for row in cur.fetchall()}
    assert EXPECTED_TABLES <= tables


def test_migrate_is_idempotent(clean_db):
    migrate(clean_db)
    assert migrate(clean_db) == []


def test_measurements_is_keyed_by_zone_and_slot(clean_db):
    migrate(clean_db)
    with clean_db.cursor() as cur:
        cur.execute(
            """select a.attname from pg_index i
               join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
               where i.indrelid = 'measurements'::regclass and i.indisprimary
               order by a.attname"""
        )
        assert [r[0] for r in cur.fetchall()] == ["slot_ts", "zone_id"]


def test_a_capture_cannot_exist_without_its_slot(clean_db):
    migrate(clean_db)
    with clean_db.cursor() as cur:
        cur.execute(
            """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale)
               values ('Alger_1','Alger','Alger',1,'Alger',36.7,3.1,15.25)"""
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """insert into captures (zone_id, slot_ts, path)
                   values ('Alger_1', '2026-10-05 00:00+01', '/data/raw/x.png')"""
            )
    clean_db.rollback()
