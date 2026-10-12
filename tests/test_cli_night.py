# tests/test_cli_night.py
"""The two operational commands: `night` (measure -> publish -> prune) and `maintenance`.

The expensive and impure seams are monkeypatched — `night.run_night` (the ~35 s/image pixel work)
and `publish.publish_day` (which shells out to the site's generator) — so these tests exercise the
CLI's and `night.run_day`'s own wiring (the abort exit code, the `job_runs` row, the prune after a
successful publish) with no Chromium, no subprocess, and no wall-clock dependency.
"""
import datetime as dt
import pathlib
import subprocess
from datetime import datetime, timedelta

import pytest

from traffic_service import cli, dump, night, prune, publish
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ, slot_for, slot_key

DAY = dt.date(2026, 10, 4)


def _fake_dump(cmd, **kwargs):
    """A `pg_dump` that only writes the `--file=` it was handed.

    `maintenance` dumps by default (plan 4 Task 1), so every maintenance test must keep a real
    `pg_dump` from running (R25): the fake writes the plain file the real code then gzips.
    """
    for arg in cmd:
        if str(arg).startswith("--file="):
            pathlib.Path(str(arg).split("=", 1)[1]).write_bytes(b"select 1;\n")
    return subprocess.CompletedProcess(cmd, 0, b"", b"")


@pytest.fixture()
def db():
    """A migrated database with one zone, the shape the capture/night CLI suites use."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        with conn.cursor() as cur:
            cur.execute(
                """insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale, publish)
                   values ('Alger_1','Alger','Alger',1,'Alger',36.7,3.1,15.25,true)""")
        conn.commit()
        yield conn


def _summary(**overrides):
    """The merged `night.run_night` counts, so the CLI has everything `cmd_night` prints."""
    summary = {"images": 6, "measured": 6, "skipped": 0, "errors": 0, "slots": 3, "unknown": [],
               "areas": 2, "cities": 1, "days": 1}
    summary.update(overrides)
    return summary


def _publish_result():
    return {"data_version": "2026-10-04-abc123def456", "day": DAY,
            "counts": {"cities.csv": 1}, "csvs": ["cities.csv"], "error_rows": 0}


def _an_old_slot_dir(raw_root, days=3):
    """A slot directory the prune clock reads as older than the 48 h grace window, named from the
    real clock so the test never depends on a hard-coded wall-clock date."""
    name = slot_key(slot_for(datetime.now(SLOT_TZ) - timedelta(days=days)))
    directory = raw_root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Alger_1_2026-01-01_00-00.png").write_bytes(b"png")
    return directory


def test_night_exits_non_zero_and_prints_the_reasons_when_publish_aborts(db, tmp_path, monkeypatch,
                                                                        capsys):
    """A `PublishAborted` IS the alert: the reasons go to stderr, the exit is non-zero, the
    `job_runs` row is `ok = false`, and — crucially — nothing was pruned, so the failed day keeps
    its pixels for the next attempt."""
    monkeypatch.setattr(night, "run_night", lambda *a, **k: _summary())

    def abort(*args, **kwargs):
        raise publish.PublishAborted("2026-10-04 failed validation: only 3 of 12 slots present")

    monkeypatch.setattr(publish, "publish_day", abort)
    raw = tmp_path / "raw"
    kept = _an_old_slot_dir(raw)

    code = cli.main(["night", "--day", "2026-10-04", "--raw-root", str(raw),
                     "--out-dir", str(tmp_path / "out")])

    assert code == 1
    err = capsys.readouterr().err
    assert "only 3 of 12 slots present" in err
    assert kept.exists(), "an aborted publish must not prune the day's pixels"
    with db.cursor() as cur:
        cur.execute("select job, target, ok, error from job_runs where job = 'night'")
        job, target, ok, error = cur.fetchone()
    assert (job, target, ok) == ("night", "2026-10-04", False)
    assert "only 3 of 12 slots present" in error


def test_a_successful_night_commits_its_job_run_row_with_the_publish_and_prune_outcome(
        db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(night, "run_night", lambda *a, **k: _summary())
    monkeypatch.setattr(publish, "publish_day", lambda *a, **k: _publish_result())
    raw = tmp_path / "raw"
    pruned = _an_old_slot_dir(raw)

    code = cli.main(["night", "--day", "2026-10-04", "--raw-root", str(raw),
                     "--out-dir", str(tmp_path / "out")])

    assert code == 0
    out = capsys.readouterr().out
    assert "data_version: 2026-10-04-abc123def456" in out
    assert "error_rows: 0" in out, "the day's errored-row count is part of the run's report"
    assert "pruned: 1 slot(s), 3 byte(s)" in out
    assert not pruned.exists(), "the prune runs only after a successful publish"
    with db.cursor() as cur:
        cur.execute("select job, target, ok, metrics from job_runs where job = 'night'")
        job, target, ok, metrics = cur.fetchone()
    assert (job, target, ok) == ("night", "2026-10-04", True)
    assert metrics["data_version"] == "2026-10-04-abc123def456"
    assert metrics["measured"] == 6
    assert "prune" not in metrics, "the prune runs after the audit row is written"


def _hold(key: str):
    """A second session holding `key`'s advisory lock, for the duration of the `with` block."""
    import contextlib

    @contextlib.contextmanager
    def held():
        with connect() as other:
            with other.cursor() as cur:
                cur.execute("select pg_try_advisory_lock(hashtext(%s))", (key,))
                assert cur.fetchone()[0] is True
            try:
                yield
            finally:
                with other.cursor() as cur:
                    cur.execute("select pg_advisory_unlock(hashtext(%s))", (key,))
                other.commit()

    return held()


def test_night_exits_zero_and_says_it_was_skipped_when_another_run_holds_the_day(db, tmp_path,
                                                                                capsys):
    """A cron overlap is not a failure: plan 4's channel must not be paged for a duplicate.

    `cmd_night` treats `JobLocked` exactly as `scheduler.run_night_job` does — one line, exit 0 —
    and the refused run still leaves its `ok = true` / `{"skipped": "locked"}` audit row.
    """
    with _hold("night:2026-10-04"):
        code = cli.main(["night", "--day", "2026-10-04", "--raw-root", str(tmp_path / "raw"),
                         "--out-dir", str(tmp_path / "out")])

    assert code == 0
    captured = capsys.readouterr()
    assert "skipped" in captured.out
    assert "night:2026-10-04" in captured.out
    with db.cursor() as cur:
        cur.execute("select target, ok, metrics from job_runs where job = 'night'")
        target, ok, metrics = cur.fetchone()
    assert (target, ok) == ("2026-10-04", True)
    assert metrics == {"skipped": "locked"}


def test_night_exits_zero_and_warns_on_stderr_when_the_prune_fails(db, tmp_path, monkeypatch,
                                                                   capsys):
    """A prune failure after a good publish is a WARNING, not an alert: the day is on the site.

    Only `PublishAborted` may exit non-zero (that is the alerting hook). The pixels' fate is the
    03:30 `maintenance` job's business, and it re-attempts the same prune on its own row.
    """
    monkeypatch.setattr(night, "run_night", lambda *a, **k: _summary())
    monkeypatch.setattr(publish, "publish_day", lambda *a, **k: _publish_result())

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(prune, "prune_raw", boom)
    raw = tmp_path / "raw"
    kept = _an_old_slot_dir(raw)

    code = cli.main(["night", "--day", "2026-10-04", "--raw-root", str(raw),
                     "--out-dir", str(tmp_path / "out")])

    assert code == 0, "a failed prune must not fail a night that published"
    captured = capsys.readouterr()
    assert "OSError: read-only filesystem" in captured.err
    assert "warning" in captured.err
    assert "data_version: 2026-10-04-abc123def456" in captured.out
    assert kept.exists()
    with db.cursor() as cur:
        cur.execute("select ok, metrics from job_runs where job = 'night'")
        ok, metrics = cur.fetchone()
    assert ok is True
    assert metrics["data_version"] == "2026-10-04-abc123def456"


def test_the_maintenance_job_skips_without_raising_when_its_lock_is_held(db, tmp_path):
    """A held `maintenance` lock records the skip on its own `job_runs` row and returns None out
    of the scheduler wrapper (the job is running elsewhere; a timer overlap is not an error)."""
    from traffic_service import scheduler

    with _hold("maintenance"):
        assert scheduler.run_maintenance_job(tmp_path) is None

    with db.cursor() as cur:
        cur.execute("select job, ok, metrics from job_runs where job = 'maintenance'")
        job, ok, metrics = cur.fetchone()
    assert (job, ok) == ("maintenance", True)
    assert metrics == {"skipped": "locked"}


def test_the_night_command_passes_the_workers_and_out_dir_it_was_given(tmp_path, monkeypatch):
    """The CLI surface is plan 4's contract (R19): every flag must reach `night.run_day`."""
    captured = {}

    def fake_run_day(conn, day, raw_root, out_dir, *, workers, **kwargs):
        captured.update(day=day, raw_root=raw_root, out_dir=out_dir, workers=workers)
        return {"images": 0, "measured": 0, "skipped": 0, "errors": 0, "slots": 0, "unknown": [],
                "areas": 0, "cities": 0, "days": 0, "publish": _publish_result(),
                "prune": {"slots": 0, "bytes": 0}}

    monkeypatch.setattr(night, "run_day", fake_run_day)
    code = cli.main(["night", "--day", "2026-10-04", "--raw-root", str(tmp_path / "raw"),
                     "--out-dir", str(tmp_path / "site"), "--workers", "2"])
    assert code == 0
    assert captured == {"day": DAY, "raw_root": str(tmp_path / "raw"),
                        "out_dir": str(tmp_path / "site"), "workers": 2}


def test_maintenance_exits_zero_and_records_its_job_run(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dump.subprocess, "run", _fake_dump)
    assert cli.main(["maintenance", "--raw-root", str(tmp_path),
                     "--dump-dir", str(tmp_path / "dump")]) == 0
    out = capsys.readouterr().out
    assert "vacuum: measurements" in out
    with db.cursor() as cur:
        cur.execute("select job, ok, metrics from job_runs where job = 'maintenance'")
        job, ok, metrics = cur.fetchone()
    assert (job, ok) == ("maintenance", True)
    assert metrics["vacuum"] == "measurements"
    assert metrics["prune"] == {"slots": 0, "bytes": 0}


def test_maintenance_prunes_and_reports_a_recent_slot(db, tmp_path, monkeypatch, capsys):
    raw = tmp_path / "raw"
    pruned = _an_old_slot_dir(raw)
    monkeypatch.setattr(dump.subprocess, "run", _fake_dump)
    with db.cursor() as cur:
        cur.execute("insert into slots (slot_ts, expected, captured, status) "
                    "values (now(), 3, 3, 'ok')")
    db.commit()

    assert cli.main(["maintenance", "--raw-root", str(raw),
                     "--dump-dir", str(tmp_path / "dump")]) == 0
    out = capsys.readouterr().out
    assert not pruned.exists()
    assert "slots (last 7 days): 1" in out
    assert "ok 3/3 failed=0" in out


def test_resolve_day_reads_yesterday_locally():
    expected = (datetime.now(SLOT_TZ) - timedelta(days=1)).date()
    assert cli.resolve_day("yesterday") == expected
    assert cli.resolve_day("2026-10-04") == dt.date(2026, 10, 4)


def test_an_unparseable_day_is_rejected():
    with pytest.raises(SystemExit):
        cli.resolve_day("last-tuesday")


def test_the_night_and_maintenance_subparsers_declare_their_flags():
    """A missing flag kills the command with AttributeError before it does anything, so the
    subparsers' surface is asserted directly (plan 4 invokes them)."""
    night_args = cli.build_parser().parse_args(["night"])
    assert (night_args.day, night_args.raw_root, night_args.out_dir,
            night_args.workers) == ("yesterday", str(cli.DEFAULT_RAW_ROOT),
                                    str(cli.DEFAULT_OUT_DIR), 4)
    maint_args = cli.build_parser().parse_args(["maintenance"])
    assert (maint_args.raw_root, maint_args.dump_dir,
            maint_args.day) == (str(cli.DEFAULT_RAW_ROOT), str(cli.DEFAULT_DUMP_DIR), None)
