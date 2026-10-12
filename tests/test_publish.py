# tests/test_publish.py
"""The publish gate: a broken day writes nothing, a good day writes the nine CSVs + a manifest.

The three tests from the task brief are kept verbatim; the extras pin the rulings: the
`data_version` is a content hash over the nine CSVs in sorted-name order (R11/R14), the manifest
is written last and a half-written set aborts (R13), the deployed regenerator must be the
parameterised copy (the brief's own `_assert_regenerator_supports_env`), the floors frame must be
non-empty and finite (R16), and the populations seed must exist — without it every city would
silently publish at the fallback 200000.

The five v2 CSVs are derived from the whole `measurements` history (R10), so these tests seed a
day's slots, measurements and rollups (`night.aggregate_day`) and let the real
`data engineering/regenerate_lovable_csv.py` run as a subprocess over the scratch frames.
"""
import datetime as dt
import hashlib
import json
import pathlib
from datetime import datetime, timezone

import pytest

from traffic_service import night, publish
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ

DAY = dt.date(2026, 10, 4)
POPULATIONS = pathlib.Path(__file__).resolve().parents[1] / "data" / "cities_population.csv"
NINE = ("areas.csv", "cities.csv", "color_composition.csv", "daily.csv", "hourly.csv",
        "hourly_delta.csv", "monthly.csv", "road_classes.csv", "road_classes_hourly.csv")


@pytest.fixture()
def db():
    """A migrated database seeded with Alger_1..Alger_3 (the night suite's fixture)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        with conn.cursor() as cur:
            for area in (1, 2, 3):
                cur.execute(
                    """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale,
                                          publish, capacity_k, floor_p20)
                       values (%s,'Alger','Alger',%s,'Alger',36.7,3.1,15.25,true,100.0,0.2)""",
                    (f"Alger_{area}", area))
        conn.commit()
        yield conn


def _seed_day(conn, slots=12):
    """A closed day: `slots` slot rows, two measured zones per slot, and the three rollups.

    `night.aggregate_day` writes the rollups from the seeded measurements — that is what makes the
    day validate (R10/R12: the rollups are the validation's input, the CSVs are not).
    """
    for hour in list(range(0, 24, 2))[:slots]:
        slot_ts = datetime(DAY.year, DAY.month, DAY.day, hour,
                           tzinfo=SLOT_TZ).astimezone(timezone.utc)
        with conn.cursor() as cur:
            cur.execute("""insert into slots (slot_ts, expected, captured, status)
                           values (%s, 3, 3, 'ok')""", (slot_ts,))
            for zone in ("Alger_1", "Alger_2"):
                cur.execute("""insert into measurements (zone_id, slot_ts, green_m, yellow_m,
                                                         red_m, dark_red_m, payload)
                               values (%s, %s, 6.0, 6.0, 6.0, 6.0, '{}')""", (zone, slot_ts))
    conn.commit()
    night.aggregate_day(conn, DAY)


def test_publish_writes_the_nine_csvs_and_a_manifest(db, tmp_path):
    _seed_day(db)

    result = publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)

    names = {p.name for p in tmp_path.iterdir()}
    assert {"cities.csv", "areas.csv", "color_composition.csv", "hourly.csv", "daily.csv",
            "monthly.csv", "road_classes.csv", "hourly_delta.csv",
            "road_classes_hourly.csv", "manifest.json"} <= names
    assert result["data_version"]


def test_the_data_version_is_the_content_hash_of_the_nine_csvs(db, tmp_path):
    """R11/R14: sha256 over the nine CSVs' bytes in sorted-name order, first 12 hex, day-prefixed;
    the manifest carries it, and `generated_at` never feeds it."""
    _seed_day(db)

    result = publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)

    digest = hashlib.sha256()
    for name in sorted(NINE):
        digest.update((tmp_path / name).read_bytes())
    assert result["data_version"] == f"{DAY}-{digest.hexdigest()[:12]}"

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["data_version"] == result["data_version"]
    assert manifest["day"] == str(DAY)
    assert set(manifest["counts"]) == set(NINE)
    assert manifest["counts"] == result["counts"]
    assert all(isinstance(count, int) for count in manifest["counts"].values())
    # counts exclude the header line; the mini day is one city, two areas, one daily row
    assert manifest["counts"]["cities.csv"] == 1
    assert manifest["counts"]["areas.csv"] == 2
    assert manifest["counts"]["daily.csv"] == 1
    assert sorted(result["csvs"]) == sorted(NINE)
    assert manifest["generated_at"] and result["day"] == DAY


def test_the_data_version_changes_when_the_data_changes(db, tmp_path):
    _seed_day(db)
    v1 = publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)["data_version"]
    # change one measurement, re-aggregate, re-publish
    with db.cursor() as cur:
        cur.execute("update measurements set red_m = red_m + 1 where zone_id = 'Alger_1'")
    db.commit()
    night.aggregate_day(db, DAY)
    v2 = publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)["data_version"]
    assert v1 != v2, "the manifest is the only thing that makes the site re-fetch"


def test_a_failed_validation_publishes_nothing(db, tmp_path):
    _seed_day(db, slots=3)  # a day with 3 slots instead of 12

    with pytest.raises(publish.PublishAborted) as excinfo:
        publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)

    assert "slots" in str(excinfo.value)
    assert not list(tmp_path.glob("*.csv")), "a broken day must not reach the site"
    assert not (tmp_path / "manifest.json").exists()


def test_the_populations_default_is_the_committed_seed():
    assert pathlib.Path(publish.DEFAULT_POPULATIONS) == POPULATIONS
    assert POPULATIONS.is_file()
    assert POPULATIONS.read_text(encoding="utf-8").splitlines()[0] == "city,population"


def test_a_missing_populations_seed_aborts(db, tmp_path):
    """Without the seed the regenerator's existence-guarded read falls back to 200000 for every
    city without erroring — publishing must refuse instead of publishing wrong populations."""
    _seed_day(db)

    with pytest.raises(publish.PublishAborted):
        publish.publish_day(db, DAY, out_dir=tmp_path,
                            populations=tmp_path / "missing_populations.csv")

    assert not list(tmp_path.glob("*.csv"))


def test_a_null_floor_aborts_before_anything_is_written(db, tmp_path):
    """R16: a NULL floor silently turns the whole area into NaN, and NaN passes the `not null`
    under psycopg — so the floors frame is checked before the CSVs are built."""
    _seed_day(db)
    with db.cursor() as cur:
        cur.execute("update zones set floor_p20 = null where zone_id = 'Alger_2'")
    db.commit()

    with pytest.raises(publish.PublishAborted) as excinfo:
        publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)

    assert "Alger area 2" in str(excinfo.value) and "freeflow" in str(excinfo.value)
    assert not list(tmp_path.glob("*.csv")) and not (tmp_path / "manifest.json").exists()


def test_an_empty_floors_frame_aborts(db, tmp_path):
    _seed_day(db)
    with db.cursor() as cur:
        cur.execute("update zones set publish = false")
    db.commit()

    with pytest.raises(publish.PublishAborted):
        publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)


def test_a_regenerator_that_leaves_a_csv_missing_aborts_without_a_manifest(db, tmp_path,
                                                                          monkeypatch):
    """R13: the nine CSVs first, the manifest LAST — a half-written set must never look fresh.
    Both shapes count: a file never written and a file written empty (a truncated write)."""
    _seed_day(db)
    fake = tmp_path / "regenerate_lovable_csv.py"
    fake.write_text(
        "import os, pathlib\n"
        "for name in ('TRAFFIC_CSV_BASE', 'TRAFFIC_POPULATIONS_CSV'):\n"
        "    os.environ.get(name)\n"
        "out = pathlib.Path(os.environ['TRAFFIC_CSV_OUT'])\n"
        "(out / 'cities.csv').write_bytes(b'')\n",
        encoding="utf-8")
    monkeypatch.setattr(publish, "REGENERATOR", fake)

    with pytest.raises(publish.PublishAborted) as excinfo:
        publish.publish_day(db, DAY, out_dir=tmp_path, populations=POPULATIONS)

    assert "areas.csv" in str(excinfo.value)   # never written
    assert "cities.csv" in str(excinfo.value)  # written, but empty
    assert not (tmp_path / "manifest.json").exists()


def test_the_deployed_regenerator_is_the_parameterised_copy():
    """The publisher refuses an unparameterised copy — and this is the copy on this box. R17:
    `data engineering/` is versioned by no repository, so the worker image must ship this exact
    file; this assertion is the loud failure if it does not."""
    publish._assert_regenerator_supports_env(publish.REGENERATOR)


def test_an_unparameterised_regenerator_is_refused(tmp_path):
    original = tmp_path / "regenerate_lovable_csv.py"
    original.write_text("BASE = 'x'\nOUT = 'y'\nimport os\n", encoding="utf-8")

    with pytest.raises(publish.PublishAborted) as excinfo:
        publish._assert_regenerator_supports_env(original)

    assert "TRAFFIC_CSV_BASE" in str(excinfo.value)
    assert "unparameterised" in str(excinfo.value)
