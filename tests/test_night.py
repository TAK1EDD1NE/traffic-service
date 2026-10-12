# tests/test_night.py
import datetime as dt
import pathlib
from datetime import datetime, timezone

import pytest

from traffic_service import derive, night, prune, publish
from traffic_service.db import connect
from traffic_service.jobs import JobLocked
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ

DAY = dt.date(2026, 10, 4)


@pytest.fixture()
def db():
    """A migrated database seeded with Alger_1..Alger_3 (the other suites' fixture).

    The zones carry the frozen floors the aggregation reads back from the registry — capacity_k
    100 m and floor_p20 0.2 — so a day of fake rows has a defined index instead of division by
    NULL (seed.py loads the same two columns from `traffic_areas.csv`).
    """
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


def test_aggregate_day_writes_the_three_rollups_and_is_idempotent(db, raw_root):
    night.measure_day(db, DAY, raw_root, extractor=_row)

    # The brief read 3 areas here; only Alger_1 and Alger_2 have images in the fixture (3 slots x
    # 2 zones = 6 PNGs), so the day's rollups carry the two area rows the derivation yields.
    first = night.aggregate_day(db, DAY)
    assert first["areas"] == 2 and first["cities"] == 1 and first["days"] == 1
    again = night.aggregate_day(db, DAY)
    assert again["areas"] == 2
    with db.cursor() as cur:
        cur.execute("select count(*) from ci_area_daily")
        assert cur.fetchone()[0] == 2, "a re-run must overwrite, not duplicate"


def test_run_night_is_the_whole_flow(db, raw_root):
    """measure -> clean -> derive -> aggregate, in one call, idempotent end to end."""
    first = night.run_night(db, DAY, raw_root, extractor=_row)
    assert first["measured"] == 6 and first["areas"] == 2
    second = night.run_night(db, DAY, raw_root, extractor=_row)
    assert second["measured"] == 0 and second["skipped"] == 6
    assert second["areas"] == 2


def test_clean_frame_is_the_clean_schema_on_the_days_local_clock(db, raw_root):
    """The 00:00 slot is 23:00 UTC the day before, so filtering or emitting on the UTC date would
    silently lose a slot of every day; `date`/`time` come out in the corpus CSV's text formats."""
    night.measure_day(db, DAY, raw_root, extractor=_row)

    frame = night.clean_frame(db, DAY)

    assert list(frame.columns) == list(derive.CLEAN_COLUMNS)
    assert len(frame) == 6, "all three slots, including the one that is the previous day in UTC"
    assert set(frame["date"]) == {"2026-10-04"}
    assert sorted(set(frame["time"])) == ["00:00", "02:00", "04:00"]
    assert sorted(set(frame["area"])) == [1, 2]


def test_aggregate_day_writes_the_days_numbers(db, raw_root):
    """The counts prove the wiring; these values prove the math that crossed the DB boundary.

    The fake row carries 6 m of every colour, so gridlock_eq = 36 and observed_m = 24; the fixture
    freezes K = 100 m and the floor at 0.2, so each row is ci_raw = 0.36, ci ~ 20.0, weight 0.24.
    The area rollup is that ci over its 3 slot rows, the city rollup the K-weighted mean of the
    two areas, and `coverage` the mean row weight at each level."""
    night.measure_day(db, DAY, raw_root, extractor=_row)
    night.aggregate_day(db, DAY)

    with db.cursor() as cur:
        cur.execute("select city, area, ci, coverage, n_rows from ci_area_daily order by area")
        area_rows = cur.fetchall()
        cur.execute("select city, ci, coverage, n_areas from ci_city_daily")
        city_rows = cur.fetchall()
        cur.execute("select day, national_ci, n_cities, n_zones, coverage from index_daily")
        index_row = cur.fetchone()

    assert area_rows == [("Alger", 1, pytest.approx(20.0), pytest.approx(0.24), 3),
                         ("Alger", 2, pytest.approx(20.0), pytest.approx(0.24), 3)]
    assert city_rows == [("Alger", pytest.approx(20.0), pytest.approx(0.24), 2)]
    assert index_row == (DAY, pytest.approx(20.0), 1, 2, pytest.approx(0.24))


# --- `run_day`: the lock is inside the audit row, and the prune cannot colour the night ---

_DAY_COUNTS = {"images": 6, "measured": 6, "skipped": 0, "errors": 0, "slots": 3,
               "unknown": [], "areas": 2, "cities": 1, "days": 1}


def _publish_result():
    return {"data_version": "2026-10-04-abc123def456", "day": DAY,
            "counts": {"cities.csv": 1}, "csvs": ["cities.csv"]}


def test_run_day_is_refused_but_still_records_an_ok_skip_when_another_run_holds_the_day(
        db, raw_root, tmp_path):
    """A duplicate is not a failure, and the audit row must say so.

    The lock is taken **inside** the `job_runs` row (the capture job's shape): the refused run
    raises `JobLocked` unchanged and still leaves `ok = true` with `metrics {"skipped": "locked"}`.
    That row is the only place the service records "the work is happening elsewhere" — take the
    lock before the row and a locked-out run writes nothing at all.
    """
    with connect() as other:
        with other.cursor() as cur:
            cur.execute("select pg_try_advisory_lock(hashtext(%s))", (f"night:{DAY}",))
            assert cur.fetchone()[0] is True, "the test must hold exactly the day's own key"
        try:
            with pytest.raises(JobLocked):
                night.run_day(db, DAY, raw_root, tmp_path / "out")
        finally:
            with other.cursor() as cur:
                cur.execute("select pg_advisory_unlock(hashtext(%s))", (f"night:{DAY}",))
            other.commit()

    with db.cursor() as cur:
        cur.execute("select job, target, ok, metrics, error from job_runs where job = 'night'")
        job, target, ok, metrics, error = cur.fetchone()
    assert (job, target, ok, error) == ("night", str(DAY), True, None)
    assert metrics == {"skipped": "locked"}


def test_a_failed_prune_does_not_fail_a_published_night(db, raw_root, tmp_path, monkeypatch):
    """The night's verdict is its publish, not its aftermath.

    The prune runs after the `job_runs` row exists, and a prune failure is caught: the day stays
    `ok = true` (and the CLI exits 0) with the error carried in the returned summary, instead of a
    false alert on plan 4's channel. The 03:30 `maintenance` pass re-attempts the same prune.
    """
    monkeypatch.setattr(night, "run_night", lambda *a, **k: dict(_DAY_COUNTS))
    monkeypatch.setattr(publish, "publish_day", lambda *a, **k: _publish_result())

    def boom(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(prune, "prune_raw", boom)

    summary = night.run_day(db, DAY, raw_root, tmp_path / "out")

    assert summary["prune"] == {"slots": 0, "bytes": 0, "error": "OSError: disk went away"}
    assert summary["publish"]["data_version"] == "2026-10-04-abc123def456"
    assert summary["measured"] == 6, "the night's own counts are untouched by a prune failure"
    with db.cursor() as cur:
        cur.execute("select ok, metrics, error from job_runs where job = 'night'")
        ok, metrics, error = cur.fetchone()
    assert (ok, error) == (True, None), "a failed prune must not mark the night failed"
    assert metrics["data_version"] == "2026-10-04-abc123def456"
    assert "prune" not in metrics, "the audit row is written before the prune runs"
