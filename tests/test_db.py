from traffic_service.db import connect, database_url


def test_database_url_defaults_to_the_compose_service(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url().endswith("/traffic")


def test_connect_reaches_the_compose_postgres():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select version(), current_database()")
            version, dbname = cur.fetchone()
    assert dbname == "traffic"
    # The container pins postgres:16; local development may point DATABASE_URL at
    # a newer native server. Assert the floor, not the patch version.
    major = int(version.split()[1].split(".")[0])
    assert major >= 16, version


def test_connect_returns_a_connection_that_needs_an_explicit_commit():
    with connect() as conn:
        assert conn.autocommit is False
        with conn.cursor() as cur:
            cur.execute("create table if not exists _t1 (id int primary key)")
            cur.execute("insert into _t1 values (1) on conflict do nothing")
            conn.rollback()
        with conn.cursor() as cur:
            cur.execute("select to_regclass('_t1')")
            exists = cur.fetchone()[0]
    assert exists is None, "rollback should have dropped the table created in this transaction"
