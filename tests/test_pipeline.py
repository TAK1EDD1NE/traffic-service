# traffic-service/tests/test_pipeline.py
import inspect
import pathlib

import pytest

from traffic_service import browser, pipeline
from traffic_service.browser import CaptureAborted, capturer


def test_data_engineering_points_at_the_sibling_directory():
    assert (pipeline.DATA_ENGINEERING / "locations.json").is_file()
    assert (pipeline.DATA_ENGINEERING / "run_traffic_local.py").is_file()


def test_load_imports_the_scraper_from_that_directory():
    scraper = pipeline.load("google_maps_scraper")
    for attribute in ("build_maps_url", "TileActivity", "wait_for_traffic_layer",
                      "CaptureAborted", "SETTLE_MS", "GOTO_TIMEOUT_MS",
                      "CAPTURE_OP_TIMEOUT_MS", "SCREENSHOT_TIMEOUT_MS", "USER_AGENTS"):
        assert hasattr(scraper, attribute), attribute


def test_the_extraction_module_is_where_the_shim_looks():
    """`run_traffic_local` is imported by the night pipeline, and its dependency set
    (opencv / scikit-image / scipy) belongs to the worker's `pipeline` extra — the
    real import is asserted there, not here. What belongs here is that the shim
    can find the file at all."""
    assert (pipeline.DATA_ENGINEERING / "run_traffic_local.py").is_file()


def test_an_unknown_module_raises_pipeline_not_found():
    with pytest.raises(pipeline.PipelineNotFound) as excinfo:
        pipeline.load("no_such_pipeline_module")
    assert "no_such_pipeline_module.py" in str(excinfo.value)


def test_a_missing_module_names_the_file(monkeypatch):
    monkeypatch.setattr(pipeline, "DATA_ENGINEERING", pathlib.Path("/nonexistent/dir"))
    with pytest.raises(pipeline.PipelineNotFound) as excinfo:
        pipeline.load("google_maps_scraper")
    assert "google_maps_scraper.py" in str(excinfo.value)


def test_the_capturer_has_the_contract_capture_slot_injects():
    assert list(inspect.signature(capturer).parameters) == ["zone", "dest"]


def test_capture_aborted_is_the_scrapers_own_class():
    assert CaptureAborted is pipeline.load("google_maps_scraper").CaptureAborted


def test_capturer_builds_the_zone_url_from_its_own_coordinates():
    scraper = pipeline.load("google_maps_scraper")
    zone = {"zone_id": "Alger_1", "city": "Alger", "lat": 36.7941463, "lng": 3.2611526, "scale": 15.25}
    expected = scraper.build_maps_url(scraper.Location(city="Alger", lat=36.7941463,
                                                       lng=3.2611526, scale=15.25))
    assert browser.zone_url(zone) == expected
    assert browser.zone_url(zone).endswith("/data=!5m1!1e1")
