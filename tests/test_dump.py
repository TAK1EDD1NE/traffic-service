# tests/test_dump.py
"""`pg_dump` and its rotation, with the subprocess faked: no real `pg_dump` ever runs in a test."""
import datetime as dt
import subprocess

import pytest

from traffic_service import dump


def test_pg_dump_gzips_the_plain_dump_and_prunes_older_files(tmp_path):
    def runner(cmd, **kwargs):
        (tmp_path / "2026-10-05.sql").write_bytes(b"select 1;\n" * 100)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    for old in ("2026-09-01", "2026-09-02", "2026-09-03"):
        (tmp_path / f"{old}.sql.gz").write_bytes(b"x")

    result = dump.pg_dump("postgresql://traffic@db:5432/traffic", tmp_path,
                          dt.date(2026, 10, 5), keep=2, runner=runner)

    assert result["path"].endswith("2026-10-05.sql.gz")
    assert (tmp_path / "2026-10-05.sql.gz").exists()
    assert not (tmp_path / "2026-10-05.sql").exists(), "the plain dump is replaced by its gzip"
    assert result["pruned"] == 1, "keep=2: two newest .sql.gz stay, the oldest goes"
    assert (tmp_path / "2026-09-03.sql.gz").exists() and not (tmp_path / "2026-09-01.sql.gz").exists()


def test_pg_dump_raises_dumpfailed_on_a_nonzero_exit(tmp_path):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, b"", b"pg_dump: error: connection failed")

    with pytest.raises(dump.DumpFailed, match="connection failed"):
        dump.pg_dump("postgresql://traffic@db:5432/traffic", tmp_path, dt.date(2026, 10, 5),
                     keep=14, runner=runner)
