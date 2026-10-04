from traffic_service import cli
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.seed import seed_zones


def test_from_empty_database_to_a_seeded_registry():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()

        assert migrate(conn) == ["0001_init.sql"]
        assert seed_zones(conn, cli.DATA_ENGINEERING / "locations.json",
                          cli.DATA_ENGINEERING / "traffic_areas.csv") == 95

        with conn.cursor() as cur:
            cur.execute("""select count(*),
                                  count(*) filter (where publish),
                                  count(*) filter (where capacity_k is null),
                                  count(distinct city)
                           from zones""")
            total, published, missing_k, cities = cur.fetchone()

    assert (total, published, missing_k, cities) == (95, 89, 6, 27)


def test_doctor_exits_zero_after_seeding(capsys):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        seed_zones(conn, cli.DATA_ENGINEERING / "locations.json",
                   cli.DATA_ENGINEERING / "traffic_areas.csv")

    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "zones: 95" in out and "published: 89" in out


def test_doctor_exits_non_zero_on_an_empty_registry():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
    assert cli.main(["doctor"]) == 1
