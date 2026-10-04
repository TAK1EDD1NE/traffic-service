import pathlib

import pytest

from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.seed import read_zones, seed_zones

DE = pathlib.Path(__file__).resolve().parents[2] / "data engineering"
LOCATIONS = DE / "locations.json"
AREAS_CSV = DE / "traffic_areas.csv"


@pytest.fixture()
def seeded_db():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        seed_zones(conn, LOCATIONS, AREAS_CSV)
        yield conn


def test_read_zones_covers_every_coordinate_once():
    zones = read_zones(LOCATIONS, AREAS_CSV)
    assert len(zones) == 95
    assert len({z["zone_id"] for z in zones}) == 95


def test_exactly_89_zones_are_published():
    zones = read_zones(LOCATIONS, AREAS_CSV)
    assert sum(1 for z in zones if z["publish"]) == 89


def test_excluded_quiet_areas_are_present_but_unpublished_and_have_no_floor():
    by_id = {z["zone_id"]: z for z in read_zones(LOCATIONS, AREAS_CSV)}
    for excluded in ["Alger_14", "Alger_15", "Alger_16", "Constantine_2",
                     "Constantine_3", "Annaba_3"]:
        assert excluded in by_id, f"{excluded} must still be captured"
        assert by_id[excluded]["publish"] is False
        assert by_id[excluded]["capacity_k"] is None
        assert by_id[excluded]["floor_p20"] is None


def test_published_capacity_and_floor_come_from_the_frozen_areas_csv():
    """Alger_1 is the spot check: capacity_m 38858.038212392, freeflow 0.29480304165089843."""
    by_id = {z["zone_id"]: z for z in read_zones(LOCATIONS, AREAS_CSV)}
    alger_1 = by_id["Alger_1"]
    assert alger_1["capacity_k"] == pytest.approx(38858.038212392)
    assert alger_1["floor_p20"] == pytest.approx(0.29480304165089843)


def test_djelfa_single_coordinate_is_area_1():
    by_id = {z["zone_id"]: z for z in read_zones(LOCATIONS, AREAS_CSV)}
    assert "Djelfa_1" in by_id
    assert by_id["Djelfa_1"]["area_no"] == 1


def test_seed_writes_and_stamps_the_floor_version(seeded_db):
    with seeded_db.cursor() as cur:
        cur.execute("select count(*), count(*) filter (where publish) from zones")
        total, published = cur.fetchone()
        cur.execute("select distinct floor_version from zones where publish")
        versions = [r[0] for r in cur.fetchall()]
    assert (total, published) == (95, 89)
    assert versions == ["p20-v2"]


def test_seed_is_idempotent(seeded_db):
    before = seed_zones(seeded_db, LOCATIONS, AREAS_CSV)
    after = seed_zones(seeded_db, LOCATIONS, AREAS_CSV)
    with seeded_db.cursor() as cur:
        cur.execute("select count(*) from zones")
        assert cur.fetchone()[0] == 95
    assert before == after == 95, "re-seeding must upsert, never duplicate"


def test_seed_leaves_lat_lng_from_locations_json(seeded_db):
    # Alger area 1 is locations.json coordinates[0] = [36.7941463, 3.2611526].
    with seeded_db.cursor() as cur:
        cur.execute("select lat, lng, scale from zones where zone_id = 'Alger_1'")
        lat, lng, scale = cur.fetchone()
    assert (lat, lng, scale) == (36.7941463, 3.2611526, 15.25)
