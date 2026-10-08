"""Metric extraction: the existing pixel pipeline, projected onto the promoted columns.

Nothing here computes a colour metric. `run_traffic_local.process_one_image` does, and this
module only decides *which* of its 34 columns are stored and how a batch is run.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

from traffic_service import pipeline

# The 26 columns the `measurements` table promotes, in the migration's own order. The pipeline's
# remaining eight (`filename`, `timestamp_processed`, the four `{c}_px` and four `{c}_pct`) are
# not promoted: the first two are job bookkeeping, and the last eight are pure functions of the
# promoted columns, so they stay recoverable from `payload` (see the migration comment).
MEASUREMENT_COLUMNS = (
    "congestion_index", "total_meters", "total_px_length", "total_skel_px", "low_coverage",
    "total_highway_m", "total_normal_m",
    "green_m", "green_mean_w", "green_highway_m", "green_normal_m",
    "yellow_m", "yellow_mean_w", "yellow_highway_m", "yellow_normal_m",
    "red_m", "red_mean_w", "red_highway_m", "red_normal_m",
    "dark_red_m", "dark_red_mean_w", "dark_red_highway_m", "dark_red_normal_m",
    "error",
)

DEFAULT_WORKERS = 4


def _project(row: dict) -> dict:
    """Keep the promoted columns and normalise the error field to a plain string."""
    out = {k: row.get(k) for k in MEASUREMENT_COLUMNS}
    out["error"] = (row.get("error") or "").strip()
    return out


def measure_image_full(path) -> dict:
    """The pipeline's whole 34-column row, unprojected — what `payload` stores.

    `error` is normalised here too, so `payload["error"]` and the promoted column always agree
    (the pipeline can leave it as `None`).
    """
    rtl = pipeline.load("run_traffic_local")
    row = dict(rtl.process_one_image(str(path)))
    row["error"] = (row.get("error") or "").strip()
    return row


def measure_image(path) -> dict:
    """One image, through the real pipeline, projected onto the promoted columns.

    Pure enough to run in a worker process.
    """
    return _project(measure_image_full(path))


def real_extractor(path):
    return measure_image(path)


# The injectable default. `measure_slot(..., extractor=EXTRACTOR)` is the real path; tests pass a
# fake so they exercise the batch bookkeeping without 35 s/image CPU work.
EXTRACTOR = real_extractor


def _pin_threads() -> None:
    """One BLAS/OpenMP thread per worker process. Measured: the default fan-out
    (6 workers x 12 threads) broke the pool with OpenBLAS 'Memory allocation still
    failed' -> BrokenProcessPool (spec:177-180)."""
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = "1"


def _worker(path):
    _pin_threads()
    return measure_image(path)


def measure_slot(paths, extractor=real_extractor, workers=DEFAULT_WORKERS) -> list[dict]:
    """Extract a whole slot's images, in path order.

    The injected `extractor` is what makes the job testable without 35 s/image CPU work.
    """
    paths = list(paths)
    if extractor is not real_extractor:
        return [extractor(p) for p in paths]
    if workers <= 1 or len(paths) <= 1:
        return [_worker(p) for p in paths]
    with ProcessPoolExecutor(max_workers=workers, initializer=_pin_threads) as pool:
        return list(pool.map(_worker, paths))
