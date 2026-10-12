# traffic-service/src/traffic_service/dump.py
"""Nightly `pg_dump`, kept as `.sql.gz` next to the raw window.

Dumps, not WAL shipping: the point is a file an operator can restore on a fresh VPS, and the DB
grows ~0.3 MB/day. `pg_dump` must be >= the server version (the image installs
postgresql-client-16 for that reason).

The dump is written plain and then gzipped (gzip -9 on ~0.3 MB is instant, and it keeps the tool
dependency at plain `pg_dump`): `<out_dir>/<YYYY-MM-DD>.sql.gz`, the day's date as the name so a
backup is addressable by the day it closes. `keep` bounds how many **older** dumps are retained —
today's is always kept, and the `keep` newest dumps that predate it — so the directory carries the
current backup plus a two-week history at the default.
"""
from __future__ import annotations

import datetime as dt
import gzip
import pathlib
import subprocess

__all__ = ["DEFAULT_KEEP", "DumpFailed", "pg_dump"]

# Two weeks of nightly dumps.
DEFAULT_KEEP = 14


class DumpFailed(RuntimeError):
    """pg_dump exited non-zero — the maintenance run is not ok."""


def pg_dump(database_url: str, out_dir, day: dt.date, keep: int = DEFAULT_KEEP,
            runner=None) -> dict:
    """Dump `database_url` to `<out_dir>/<day>.sql.gz`, then prune older dumps.

    `runner` is resolved at call time so a test can monkeypatch `dump.subprocess.run` instead of
    threading a fake through every caller. `keep` is how many older dumps survive: the file just
    written is never pruned, and the `keep` newest of the rest stay. Returns `{path, bytes,
    pruned}`. A non-zero exit raises `DumpFailed` (and removes the half-written plain file) — the
    caller lets it out so `job_runs.ok` reads false.
    """
    runner = runner or subprocess.run
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plain = out_dir / f"{day}.sql"
    target = out_dir / f"{day}.sql.gz"

    process = runner(
        ["pg_dump", "--no-owner", "--no-privileges", f"--file={plain}", database_url],
        capture_output=True,
    )
    if process.returncode != 0:
        plain.unlink(missing_ok=True)
        raise DumpFailed(f"pg_dump exited {process.returncode}: "
                         f"{process.stderr.decode('utf-8', 'replace')[:500]}")

    with plain.open("rb") as source, gzip.open(target, "wb", compresslevel=9) as dest:
        dest.write(source.read())
    plain.unlink()

    # ISO names sort chronologically. Today's dump is excluded from the rotation candidates so
    # the just-taken backup is never the one pruned; `keep` bounds the history behind it.
    older = sorted(path for path in out_dir.glob("*.sql.gz") if path != target)
    pruned = 0
    for old in older[:-keep] if keep > 0 else []:
        old.unlink()
        pruned += 1
    return {"path": str(target), "bytes": target.stat().st_size, "pruned": pruned}
