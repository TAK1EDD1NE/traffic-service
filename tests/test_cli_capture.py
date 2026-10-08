import logging

import pytest

from traffic_service import cli
from traffic_service.db import connect
from traffic_service.migrate import migrate


@pytest.fixture()
def db():
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


def _fake_capturer(zone, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"png")


def test_capture_command_runs_one_slot(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_capturer", lambda: _fake_capturer)
    assert cli.main(["capture", "--slot", "2026-10-05T02:00", "--raw-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "status: ok" in out
    assert (tmp_path / "2026-10-05T02-00" / "Alger_1_2026-10-05_02-00.png").is_file()


def test_capture_limit_captures_a_subset(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_capturer", lambda: _fake_capturer)
    assert cli.main(["capture", "--slot", "2026-10-05T04:00",
                     "--raw-root", str(tmp_path), "--limit", "1"]) == 0
    assert "captured: 1" in capsys.readouterr().out


def test_capture_exits_one_when_a_zone_fails(db, tmp_path, monkeypatch, capsys):
    def fails(zone, dest):
        raise RuntimeError("no traffic layer")

    monkeypatch.setattr(cli, "resolve_capturer", lambda: fails)
    assert cli.main(["capture", "--slot", "2026-10-05T05:00", "--raw-root", str(tmp_path)]) == 1
    assert "status: failed" in capsys.readouterr().out


def test_capture_of_an_empty_registry_fails_loudly(tmp_path, monkeypatch):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
    monkeypatch.setattr(cli, "resolve_capturer", lambda: _fake_capturer)
    with pytest.raises(RuntimeError, match="no zones"):
        cli.main(["capture", "--slot", "2026-10-05T06:00", "--raw-root", str(tmp_path)])


def test_an_unparseable_slot_is_rejected():
    with pytest.raises(SystemExit):
        cli.parse_slot("not-a-timestamp")


def test_the_scheduler_command_declares_the_raw_root_it_reads():
    """cmd_scheduler reads args.raw_root; if the subparser does not define it the
    command dies with AttributeError before starting."""
    args = cli.build_parser().parse_args(["scheduler"])
    assert args.raw_root, "the scheduler subparser must declare --raw-root"


def test_doctor_reports_the_last_slot(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_capturer", lambda: _fake_capturer)
    cli.main(["capture", "--slot", "2026-10-05T08:00", "--raw-root", str(tmp_path)])
    capsys.readouterr()
    # The exit code reflects registry health (95 zones / 89 published, asserted in
    # plan 1); this fixture holds one zone, so only the report line is asserted here.
    cli.main(["doctor"])
    assert "last slot: 2026-10-05T08-00 ok" in capsys.readouterr().out


def test_main_configures_logging_at_info(db, capsys):
    """Nothing else calls basicConfig, so the root logger would sit at WARNING and
    every progress `logging.info` would be dropped — an unattended service whose
    failure mode is 'quietly captured nothing' must not be silent. Drop the level
    first so the assertion proves `main()` raised it, not an earlier test."""
    logging.getLogger().setLevel(logging.WARNING)
    cli.main(["doctor"])
    assert logging.getLogger().level == logging.INFO
