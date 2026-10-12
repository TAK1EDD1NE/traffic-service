# tests/test_prune.py
"""The retention rule: pixels are transient, the measurement columns are forever (spec:125-129).

The clock is the slot the directory names, never the file's mtime — the first test proves it (both
directories are created at test time, so they share an mtime, yet only the old-named one goes), and
`test_the_slot_name_is_the_clock_not_the_mtime` states it directly.
"""
import os
from datetime import datetime, timezone

import pytest

from traffic_service import prune
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ


@pytest.fixture()
def db():
    """A migrated database with one zone, so a measurement can be inserted (FK to `zones`)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        with conn.cursor() as cur:
            cur.execute(
                """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale, publish)
                   values ('Alger_1','Alger','Alger',1,'Alger',36.7,3.1,15.25,true)""")
        conn.commit()
        yield conn


def test_prune_deletes_only_pngs_older_than_the_grace_window(tmp_path):
    old = tmp_path / "2026-10-01T00-00"; new = tmp_path / "2026-10-04T00-00"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "Alger_1_2026-10-04_00-00.png").write_bytes(b"png")

    freed = prune.prune_raw(tmp_path, now=datetime(2026, 10, 4, 12, tzinfo=SLOT_TZ), grace_hours=48)

    assert freed["slots"] == 1 and freed["bytes"] == 3
    assert not old.exists() and new.exists()


def test_prune_never_touches_measurements(db, tmp_path):
    """Retention rule (spec:125-129): pixels are transient, the 34 columns are forever."""
    slot_ts = datetime(2026, 10, 1, 0, 0, tzinfo=SLOT_TZ).astimezone(timezone.utc)
    old = tmp_path / "2026-10-01T00-00"
    old.mkdir(parents=True)
    (old / "Alger_1_2026-10-01_00-00.png").write_bytes(b"png")
    with db.cursor() as cur:
        cur.execute("insert into slots (slot_ts, expected, status) values (%s, 1, 'ok')", (slot_ts,))
        cur.execute("insert into measurements (zone_id, slot_ts, payload) "
                    "values ('Alger_1', %s, '{}')", (slot_ts,))
    db.commit()

    freed = prune.prune_raw(tmp_path, now=datetime(2026, 10, 4, 12, tzinfo=SLOT_TZ), grace_hours=48)

    assert freed["slots"] == 1 and not old.exists()
    with db.cursor() as cur:
        cur.execute("select count(*) from measurements")
        assert cur.fetchone()[0] == 1, "the pixel never carried anything the DB does not keep"
        cur.execute("select count(*) from slots")
        assert cur.fetchone()[0] == 1, "prune is a filesystem rule and issues no DELETE"


def test_the_slot_name_is_the_clock_not_the_mtime(tmp_path):
    """A copy or a backup restore resets every mtime, so mtime must never gate a prune: a fresh
    slot whose mtime was copied from an old backup survives, and an old slot copied today goes."""
    fresh = tmp_path / "2026-10-04T00-00"
    old = tmp_path / "2026-10-01T00-00"
    for d in (fresh, old):
        d.mkdir()
        (d / "Alger_1.png").write_bytes(b"png")
    # Both directories get an mtime far in the past: a restore-from-backup shape.
    stale = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(fresh, (stale, stale))
    os.utime(old, (stale, stale))

    prune.prune_raw(tmp_path, now=datetime(2026, 10, 4, 12, tzinfo=SLOT_TZ), grace_hours=48)

    assert fresh.exists(), "an old mtime must not delete a fresh slot"
    assert not old.exists(), "the clock is the slot in the name"


def test_prune_leaves_a_directory_that_is_not_a_slot(tmp_path):
    """Only real `<day>T<even hour>-00` capture slots are candidates — a stray name is not one."""
    stray = tmp_path / "not-a-slot"
    odd = tmp_path / "2026-10-01T01-00"   # an odd local hour is not a slot
    for d in (stray, odd):
        d.mkdir()
        (d / "x.png").write_bytes(b"png")

    freed = prune.prune_raw(tmp_path, now=datetime(2026, 10, 4, 12, tzinfo=SLOT_TZ), grace_hours=48)

    assert freed == {"slots": 0, "bytes": 0}
    assert stray.exists() and odd.exists()


def test_pruning_a_missing_raw_root_is_not_an_error(tmp_path):
    assert prune.prune_raw(tmp_path / "absent",
                           now=datetime(2026, 10, 4, 12, tzinfo=SLOT_TZ)) == {"slots": 0, "bytes": 0}


def test_slot_start_reads_only_even_local_hours():
    assert prune.slot_start("2026-10-04T00-00") is not None
    assert prune.slot_start("2026-10-04T22-00").hour == 22
    assert prune.slot_start("2026-10-04T01-00") is None
    assert prune.slot_start("2026-13-04T00-00") is None      # impossible month
    assert prune.slot_start("nonsense") is None


def test_the_slot_parser_is_the_shared_slots_implementation():
    """One implementation, two delegations: the day walk and the prune must agree on what a slot
    directory is, or a drift makes one of them silently stop recognising the other's directories.

    `night` is imported here (not at module level) so this file stays runnable without the pixel
    extra; the point is the *values*, not the import graph.
    """
    from traffic_service import night
    from traffic_service.slots import slot_start_from_name

    for name in ("2026-10-04T00-00", "2026-10-04T22-00", "2026-10-04T01-00",
                 "2026-10-04T00-30", "2026-13-04T00-00", "nonsense"):
        assert prune.slot_start(name) == slot_start_from_name(name), name
        assert night._parse_slot(name) == slot_start_from_name(name), name
    assert prune.slot_start("2026-10-04T04-00") == night._parse_slot("2026-10-04T04-00")
