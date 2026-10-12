# tests/test_cli_ops.py
"""The ops layer end to end: one `maintenance` invocation through `cli.main`, plus the
`run_maintenance` contract the 03:30 scheduled path shares and a CLI-only test cannot see.

Three mechanisms changed from the task brief, all forced by plan 3's final shape:

- the metrics keys are plan 3's (`metrics["prune"]["slots"]`, not a flat `pruned_slots`) — the
  `dump` and `problems` keys are added beside them (R23);
- the alert channel is injected into `maintenance.run_maintenance` and defaults to `alerts.notify`
  (R24), so it reaches the scheduled path; these tests spy on the **injected** notifier instead of
  monkeypatching `alerts.notify`, and no test can send an alert;
- the dump's URL is read from `db.database_url()` (R26), not `os.environ["DATABASE_URL"]`, so the
  brief's `setenv("DATABASE_URL", "postgresql://traffic@db:5432/traffic")` — a Compose hostname
  that cannot resolve from the test host, and that would redirect `connect()` too — is gone. The
  URL that reaches `pg_dump` is asserted, via `maintenance.db`, to come from the service's own
  `database_url()` helper.

Every dump is faked (`dump.subprocess.run`): no test runs a real `pg_dump` (R25).
"""
import pathlib
import subprocess
from datetime import datetime, timedelta

import pytest

from traffic_service import cli, dump, maintenance
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ, slot_for, slot_key, slot_label


def _recording_dump(seen):
    """A `pg_dump` that writes the `--file=` it was handed and records the URL it was called with.

    Never a real process (R25); `seen["url"]` is the last argument `pg_dump` received.
    """

    def run(cmd, **kwargs):
        seen["url"] = cmd[-1]
        for arg in cmd:
            if str(arg).startswith("--file="):
                pathlib.Path(str(arg).split("=", 1)[1]).write_bytes(b"select 1;\n")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    return run


@pytest.fixture()
def db():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        yield conn


@pytest.fixture()
def db_and_raw(tmp_path):
    """A migrated DB and a raw tree with one slot inside the 48 h window and one outside it.

    Both names come from the real clock (`prune_raw` reads it), so the test asserts on slots
    relative to `now`, never on a hardcoded date that ages out of the window.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
    now = datetime.now(SLOT_TZ)
    raw = tmp_path / "raw"
    for hours in (72, 12):
        slot_ts = slot_for(now - timedelta(hours=hours))
        directory = raw / slot_key(slot_ts)
        directory.mkdir(parents=True)
        (directory / f"Alger_1_{slot_label(slot_ts)}.png").write_bytes(b"png")
    return raw


def test_maintenance_prunes_dumps_and_records_one_job_run(db_and_raw, tmp_path, monkeypatch):
    """The whole ops pass through the CLI: prune (48 h clock), dump, one audit row, and the alert.

    The notifier is injected into the real `run_maintenance` (R24) — a spy on the **injected**
    notifier, replacing the brief's `monkeypatch.setattr(cli.alerts, "notify", …)`, which could no
    longer reach the scheduled path's default. The URL the dump received is asserted to come from
    the service's `db.database_url()` (R26), not a raw environment read.
    """
    sent = []
    seen = {}
    real = maintenance.run_maintenance

    def with_spy(conn, raw_root, **kwargs):
        kwargs.setdefault("notifier", sent.append)
        return real(conn, raw_root, **kwargs)

    class FakeDB:
        @staticmethod
        def database_url():
            return "postgresql://traffic@sentinel:5432/traffic"

    monkeypatch.setattr(maintenance, "run_maintenance", with_spy)
    monkeypatch.setattr(maintenance, "db", FakeDB)
    monkeypatch.setattr(dump.subprocess, "run", _recording_dump(seen))
    dump_dir = tmp_path / "dump"
    now = datetime.now(SLOT_TZ)

    code = cli.main(["maintenance", "--raw-root", str(db_and_raw), "--dump-dir", str(dump_dir)])

    assert code == 0
    assert not (db_and_raw / slot_key(slot_for(now - timedelta(hours=72)))).exists(), "past 48 h"
    assert (db_and_raw / slot_key(slot_for(now - timedelta(hours=12)))).exists(), "inside the window"
    assert list(dump_dir.glob("*.sql.gz")), "the dump was written"
    assert seen["url"] == "postgresql://traffic@sentinel:5432/traffic", "the URL is db.database_url()"
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select job, ok, metrics from job_runs order by id desc limit 1")
        job, ok, metrics = cur.fetchone()
    assert job == "maintenance" and ok is True
    assert metrics["prune"]["slots"] == 1 and metrics["dump"].endswith(".sql.gz")
    assert metrics["problems"], "an empty day's missing slots are recorded in the run's metrics"
    assert sent, "the missing slots are alerted, not silently swallowed"


def test_run_maintenance_dump_failure_raises_and_records_ok_false(db, tmp_path, monkeypatch):
    """A failing dump must RAISE out of `run_maintenance` — that is what makes the audit row read
    `ok = false` with the reason, and the failure is alerted too (R23/R24)."""
    sent = []
    raw = tmp_path / "raw"
    raw.mkdir()

    def boom(*args, **kwargs):
        raise dump.DumpFailed("pg_dump exited 1: pg_dump: error: connection failed")

    monkeypatch.setattr(dump, "pg_dump", boom)

    with pytest.raises(dump.DumpFailed, match="connection failed"):
        maintenance.run_maintenance(db, raw, dump_dir=tmp_path / "dump", notifier=sent.append)

    with db.cursor() as cur:
        cur.execute("select job, ok, metrics, error from job_runs order by id desc limit 1")
        job, ok, metrics, error = cur.fetchone()
    assert (job, ok) == ("maintenance", False)
    assert metrics is None, "a failed run carries no metrics, by job_run's own rule"
    assert "connection failed" in error
    assert any("connection failed" in message for message in sent), "the dump failure is alerted"


def test_run_maintenance_records_its_problems_and_alerts_them(db, tmp_path, monkeypatch):
    """A healthy run against an empty day is still `ok = true`; the problems it found are in
    `metrics` and every one reaches the notifier, verbatim (R24)."""
    sent = []
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(dump.subprocess, "run", _recording_dump({}))

    summary = maintenance.run_maintenance(db, raw, dump_dir=tmp_path / "dump",
                                          notifier=sent.append)

    with db.cursor() as cur:
        cur.execute("select job, ok, metrics from job_runs order by id desc limit 1")
        job, ok, metrics = cur.fetchone()
    assert (job, ok) == ("maintenance", True)
    assert metrics["vacuum"] == "measurements"
    assert metrics["prune"] == {"slots": 0, "bytes": 0}
    assert metrics["dump"].endswith(".sql.gz")
    assert summary["problems"], "a run against an empty window reports its problems"
    assert sent == metrics["problems"], "every collected problem is alerted, verbatim"
