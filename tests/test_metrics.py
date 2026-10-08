# tests/test_metrics.py
import pathlib

import pytest

from traffic_service import metrics
from traffic_service.db import connect
from traffic_service.migrate import migrate

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "data engineering" / "screenshots"


@pytest.fixture()
def db():
    """A migrated, empty database (same shape as the other suites' fixture)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        yield conn


def test_the_measurements_table_has_every_promoted_column(db):
    with db.cursor() as cur:
        cur.execute("""select column_name from information_schema.columns
                       where table_name = 'measurements'""")
        present = {r[0] for r in cur.fetchall()}
    assert set(metrics.MEASUREMENT_COLUMNS) <= present


def test_measure_image_reproduces_the_pipelines_own_row():
    """The service must not invent its own metrics: the row it stores has to be the row
    `run_traffic_local.build_row` produces."""
    from traffic_service import pipeline

    rtl = pipeline.load("run_traffic_local")
    png = sorted(CORPUS.glob("Alger_1_*.png"))[0]

    ours = metrics.measure_image(png)
    theirs = rtl.process_one_image(str(png))

    for key in metrics.MEASUREMENT_COLUMNS:
        assert ours[key] == pytest.approx(theirs[key], rel=1e-9, abs=1e-9), key
    assert ours["error"] == "" and ours["congestion_index"] > 0


def test_a_broken_image_becomes_an_error_row_not_an_exception(tmp_path):
    bad = tmp_path / "not_an_image.png"
    bad.write_bytes(b"this is not a png")

    row = metrics.measure_image(bad)

    assert row["error"], "a file that cannot be read must carry a reason"
    assert row["congestion_index"] == -1.0
    assert row["low_coverage"] is True
    assert row["total_meters"] == 0.0


def test_load_imports_the_extraction_pipeline():
    """Deferred from plan 2: run_traffic_local imports cv2/scipy/skimage/tqdm at module
    scope, so this assertion only became possible once the `pipeline` extra existed."""
    from traffic_service import pipeline

    assert hasattr(pipeline.load("run_traffic_local"), "process_one_image")
