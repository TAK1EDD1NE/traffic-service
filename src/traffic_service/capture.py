"""One capture slot: every zone, once, resumable, and never a silent hole.

The capturer is injected. Tests pass a fake so they need no Chromium; production
passes `browser.capturer`. Everything else — the lock, the retry, the rows, the
slot status — is the same code in both cases, which is the point.
"""
from __future__ import annotations

import logging
import time

from traffic_service.capture_helpers import slot_dir, zone_filename
from traffic_service.jobs import JobLocked, advisory_lock, job_run
from traffic_service.slots import slot_key

__all__ = ["capture_slot", "capture_slot_guarded", "load_zones", "slot_dir",
           "zone_filename", "JobLocked"]

RETRY_BACKOFF_SECONDS = 2.0

UPSERT_SLOT = """
insert into slots (slot_ts, expected, status)
values (%s, %s, 'running')
on conflict (slot_ts) do update set expected = excluded.expected,
                                   status = 'running',
                                   started_at = now(),
                                   finished_at = null
"""

FINISH_SLOT = """
update slots set captured = %s, status = %s, finished_at = now() where slot_ts = %s
"""

UPSERT_CAPTURE = """
insert into captures (zone_id, slot_ts, path, bytes, error, captured_at)
values (%s, %s, %s, %s, %s, now())
on conflict (zone_id, slot_ts) do update set path = excluded.path,
                                            bytes = excluded.bytes,
                                            error = excluded.error,
                                            captured_at = now()
"""


def load_zones(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""select zone_id, city, lat, lng, scale from zones
                       where publish = true order by zone_id""")
        columns = ["zone_id", "city", "lat", "lng", "scale"]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _already_captured(conn, slot_ts) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("""select zone_id from captures
                       where slot_ts = %s and path is not null and error is null""", (slot_ts,))
        return {row[0] for row in cur.fetchall()}


def _status(captured: int, expected: int) -> str:
    if captured == expected:
        return "ok"
    return "partial" if captured else "failed"


def capture_slot(conn, slot_ts, *, raw_root, capturer, zones=None,
                 retries: int = 1, force: bool = False,
                 backoff_seconds: float = RETRY_BACKOFF_SECONDS) -> dict:
    """Capture every zone for `slot_ts`. Resumable; safe to re-run."""
    with advisory_lock(conn, f"capture:{slot_key(slot_ts)}"):
        if zones is None:
            zones = load_zones(conn)
        expected = len(zones)
        if expected == 0:
            raise RuntimeError("no zones in the registry — run `traffic-service seed-zones` first")

        with conn.cursor() as cur:
            cur.execute(UPSERT_SLOT, (slot_ts, expected))
        conn.commit()

        directory = slot_dir(raw_root, slot_ts)
        done = set() if force else _already_captured(conn, slot_ts)
        captured = failed = skipped = 0

        for zone in zones:
            zone_id = zone["zone_id"]
            if zone_id in done:
                skipped += 1
                continue
            dest = directory / zone_filename(zone_id, slot_ts)
            error = None
            for attempt in range(retries + 1):
                try:
                    capturer(zone, dest)
                    error = None
                    break
                except Exception as exc:  # noqa: BLE001 — any failure is a failed coordinate
                    error = f"{type(exc).__name__}: {exc}"[:2000]
                    if attempt < retries:
                        logging.warning("capture retry %d for %s: %s", attempt + 1, zone_id, error)
                        time.sleep(backoff_seconds)
            size = dest.stat().st_size if error is None and dest.exists() else None
            if error is None and size is None:
                error = "OSError: capturer reported success but wrote no file"
            with conn.cursor() as cur:
                cur.execute(UPSERT_CAPTURE, (zone_id, slot_ts,
                                             str(dest) if error is None else None,
                                             size, error))
            if error is None:
                captured += 1
            else:
                failed += 1
                logging.warning("capture failed for %s in slot %s: %s",
                                zone_id, slot_key(slot_ts), error)
        conn.commit()

        status = _status(captured + skipped, expected)
        with conn.cursor() as cur:
            cur.execute(FINISH_SLOT, (captured + skipped, status, slot_ts))
        conn.commit()

    result = {"slot_ts": slot_ts, "expected": expected, "captured": captured,
              "failed": failed, "skipped": skipped, "status": status,
              "dir": str(directory)}
    if status != "ok":
        logging.warning("slot %s finished %s: %d/%d captured (%d failed, %d skipped)",
                        slot_key(slot_ts), status, captured + skipped, expected, failed, skipped)
    return result


def capture_slot_guarded(conn, slot_ts, **kwargs) -> dict:
    """`capture_slot` plus the job_runs audit row. `JobLocked` propagates."""
    with job_run(conn, "capture", target=slot_key(slot_ts)) as record:
        result = capture_slot(conn, slot_ts, **kwargs)
        record["metrics"] = {k: v for k, v in result.items() if k != "slot_ts"}
        return result
