"""Pure path helpers for the capture job, separate so they import nothing heavy."""
from __future__ import annotations

import pathlib

from traffic_service.slots import slot_key, slot_label


def zone_filename(zone_id: str, slot_ts) -> str:
    """`<zone_id>_<local slot time>.png` — derived from the slot, so a re-run
    overwrites the same file."""
    return f"{zone_id}_{slot_label(slot_ts)}.png"


def slot_dir(raw_root, slot_ts) -> pathlib.Path:
    directory = pathlib.Path(raw_root) / slot_key(slot_ts)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
