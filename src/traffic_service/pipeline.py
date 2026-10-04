# traffic-service/src/traffic_service/pipeline.py
"""Import the existing pipeline modules by path.

The scraper and the extraction pipeline already exist and are exercised by the
PC workflow; this service reuses them instead of forking them. The directory has
a space in its name, so it goes on `sys.path` and the *module* is imported
normally — no importlib gymnastics, and the file stays a single source of truth.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

DATA_ENGINEERING = pathlib.Path(__file__).resolve().parents[3] / "data engineering"


class PipelineNotFound(RuntimeError):
    """The pipeline module is not where this service expects it."""


def load(module_name: str):
    directory = DATA_ENGINEERING
    if not (directory / f"{module_name}.py").is_file():
        raise PipelineNotFound(
            f"{module_name}.py not found in {directory} — the service expects the "
            f"pipeline source next to it (in the container it is copied to this path)"
        )
    path = str(directory)
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module(module_name)
