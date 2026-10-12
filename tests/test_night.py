# tests/test_night.py
import datetime as dt
import pathlib
from datetime import datetime, timezone

import pytest

from traffic_service import night
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ

DAY = dt.date(2026, 10, 4)


@pytest.fixture()
def db():
    """A migrated database seeded with Alger_1..Alger_3 (the other suites' fixture)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        with conn.cursor() as cur:
            for area in (1, 2, 3):
                cur.execute(
                    """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale, publish)
                       values (%s,'Alger','Alger',%s,'Alger',36.7,3.1,15.25,true)""",
                    (f"Alger_{area}", area))
        conn.commit()
        yield conn


@pytest.fixture
def raw_root(tmp_path):
    """A day of slots on disk, in the layout capture writes: <raw_root>/<slot_key>/<png>."""
    for hour in (0, 2, 4):
        d = tmp_path / f"2026-10-04T{hour:02d}-00"
        d.mkdir(parents=True)
        for zone in ("Alger_1", "Alger_2"):
            (d / f"{zone}_2026-10-04_{hour:02d}-00.png").write_bytes(b"png")
    return tmp_path


def _row(path, **overrides):
    """A fake extractor row carrying the promoted columns, so no test needs the pixel pipeline."""
    row = {"congestion_index": 42.0, "total_meters": 1.0, "total_px_length": 2.0,
           "total_skel_px": 3, "low_coverage": False, "total_highway_m": 4.0,
           "total_normal_m": 5.0, **{f"{c}_{s}": 6.0 for c in
                                     ("green", "yellow", "red", "dark_red")
                                     for s in ("m", "mean_w", "highway_m", "normal_m")},
           "error": ""}
    row.update(overrides)
    return row


def test_it_finds_only_the_12_slot_directories_of_that_day(raw_root):
    (raw_root / "2026-10-03T22-00").mkdir()          # another day
    (raw_root / "2026-10-04T01-00").mkdir()          # not an even-hour slot
    assert [p.name for p in night.slot_dirs(DAY, raw_root)] == [
        "2026-10-04T00-00", "2026-10-04T02-00", "2026-10-04T04-00"]


def test_it_derives_the_zone_id_from_the_filename(raw_root):
    """The zone id is the filename stem up to the trailing timestamp: `Alger_1_2026-10-04_00-00.png`
    -> `Alger_1`. The slug must never be re-derived from a display name (plan 1: three-way name
    transform, join on the de-accented form)."""
    assert night.zone_id_for("Alger_1_2026-10-04_00-00.png") == "Alger_1"
    assert night.zone_id_for("A_n_T_mouchent_2_2026-10-04_00-00.png") == "A_n_T_mouchent_2"
    assert night.zone_id_for("Ain_Temouchent_2026-10-04_00-00.png") == "Ain_Temouchent"


def test_measure_day_writes_one_row_per_image_and_is_idempotent(db, raw_root):
    calls = []

    def fake(path):
        calls.append(str(path))
        return {"congestion_index": 42.0, "total_meters": 1.0, "total_px_length": 2.0,
                "total_skel_px": 3, "low_coverage": False, "total_highway_m": 4.0,
                "total_normal_m": 5.0, **{f"{c}_{s}": 6.0 for c in
                                          ("green", "yellow", "red", "dark_red")
                                          for s in ("m", "mean_w", "highway_m", "normal_m")},
                "error": ""}

    first = night.measure_day(db, DAY, raw_root, extractor=fake)
    assert first["images"] == 6 and first["measured"] == 6 and first["skipped"] == 0

    second = night.measure_day(db, DAY, raw_root, extractor=fake)
    assert second["measured"] == 0 and second["skipped"] == 6
    assert len(calls) == 6, "a measured image must never be re-measured on a re-run"

    with db.cursor() as cur:
        cur.execute("select count(*) from measurements")
        assert cur.fetchone()[0] == 6
        cur.execute("select count(*) from measurements where payload is null")
        assert cur.fetchone()[0] == 0


def test_zone_id_for_resolves_against_the_registry():
    """The corpus's single unindexed stem is `Djelfa` (a one-coordinate city); the registry keys
    every zone `<slug>_<area_no>`, so the registry form must try `_1` before giving up."""
    known = {"Alger_1", "Djelfa_1"}
    assert night.zone_id_for("Djelfa_2026-10-04_00-00.png", known) == "Djelfa_1"
    assert night.zone_id_for("Alger_1_2026-10-04_00-00.png", known) == "Alger_1"
    assert night.zone_id_for("Nowhere_2026-10-04_00-00.png", known) is None


def test_measure_day_counts_an_image_outside_the_registry_and_never_inserts_it(db, raw_root):
    (raw_root / "2026-10-04T00-00" / "Ghost_9_2026-10-04_00-00.png").write_bytes(b"png")

    result = night.measure_day(db, DAY, raw_root, extractor=_row)

    assert result["images"] == 7 and result["slots"] == 3
    assert result["measured"] == 6
    assert result["unknown"] == ["Ghost_9_2026-10-04_00-00.png"]
    with db.cursor() as cur:
        cur.execute("select count(*) from measurements")
        assert cur.fetchone()[0] == 6, "an unknown zone must never reach the FK"


def test_a_non_finite_metric_becomes_null_in_the_payload_instead_of_crashing(db, raw_root):
    """Postgres jsonb rejects `NaN`/`Infinity` ("invalid input syntax for type json") and the
    pipeline emits them when a colour is absent — the payload must be sanitised, recursively,
    or a real day crashes at the insert."""
    def fake(path):
        return _row(path, congestion_index=float("nan"), total_px_length=float("inf"),
                    green_px=[{"pct": float("-inf")}])

    result = night.measure_day(db, DAY, raw_root, extractor=fake)
    assert result["measured"] == 6

    with db.cursor() as cur:
        cur.execute("""select payload -> 'congestion_index', payload -> 'total_px_length',
                              payload #> '{green_px,0,pct}'
                       from measurements limit 1""")
        assert cur.fetchone() == (None, None, None)


def test_json_safe_nulls_every_non_finite_float_recursively():
    row = {"ok": 1.5, "nan": float("nan"), "inf": float("inf"),
           "nested": [{"deep": float("-inf")}, (2.0, float("nan"))],
           "text": "keep", "flag": True, "nothing": None}

    assert night.json_safe(row) == {"ok": 1.5, "nan": None, "inf": None,
                                    "nested": [{"deep": None}, [2.0, None]],
                                    "text": "keep", "flag": True, "nothing": None}


def test_measuring_a_day_never_clobbers_a_real_captures_slot_row(db, raw_root):
    slot_ts = datetime(2026, 10, 4, 0, 0, tzinfo=SLOT_TZ).astimezone(timezone.utc)
    with db.cursor() as cur:
        cur.execute("""insert into slots (slot_ts, expected, captured, status)
                       values (%s, 95, 95, 'ok')""", (slot_ts,))
    db.commit()

    night.measure_day(db, DAY, raw_root, extractor=_row)

    later = datetime(2026, 10, 4, 2, 0, tzinfo=SLOT_TZ).astimezone(timezone.utc)
    with db.cursor() as cur:
        cur.execute("select expected, captured, status from slots where slot_ts = %s", (slot_ts,))
        assert cur.fetchone() == (95, 95, "ok"), "the capture's counts must survive the night job"
        cur.execute("select expected, status from slots where slot_ts = %s", (later,))
        assert cur.fetchone() == (3, "running"), "every processed slot gets its own row"
        cur.execute("select count(*) from slots")
        assert cur.fetchone()[0] == 3
