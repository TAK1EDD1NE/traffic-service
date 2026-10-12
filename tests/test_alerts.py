# tests/test_alerts.py
"""The alert channel and the two health reads: no network, no wall clock, no Chromium.

`notify` never raises — a failed alert must not fail the job that raised it — and its webhook is
environment-only, so these tests pin both halves: unset is a no-op, set posts the body, and a
transport failure is swallowed.
"""
import datetime as dt

import pytest

from traffic_service import alerts
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.slots import SLOT_TZ, slot_for


def slot_key_of(slot_ts):
    from traffic_service.slots import slot_key
    return slot_key(slot_ts)


@pytest.fixture()
def db():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")
        conn.commit()
        migrate(conn)
        yield conn


def test_notify_is_a_silent_no_op_without_a_webhook(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert alerts.notify("capture slot 2026-10-05T02-00 ended partial") is False


def test_notify_posts_the_message_when_a_webhook_is_set(monkeypatch):
    sent = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        sent["url"] = request.full_url
        sent["body"] = request.data
        return Response()

    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://ntfy.sh/estin-traffic-test")
    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    assert alerts.notify("disk 84% on /data") is True
    assert sent["url"] == "https://ntfy.sh/estin-traffic-test"
    assert sent["body"] == b"disk 84% on /data"


def test_notify_never_raises_when_the_webhook_fails(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://ntfy.sh/x")

    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", boom)
    assert alerts.notify("night failed") is False


def test_health_reports_missing_stale_and_degraded_slots(db):
    """The three silent failure modes of this project (spec:212-218): a slot that never
    ran, a slot stuck `running`, and a slot that captured 40/95 without raising."""
    now = dt.datetime(2026, 10, 5, 12, 30, tzinfo=SLOT_TZ)
    healthy, stale, partial = (slot_for(now - dt.timedelta(hours=h)) for h in (2, 4, 6))
    with db.cursor() as cur:
        cur.execute("insert into slots (slot_ts, expected, captured, status) values (%s,95,95,'ok')",
                    (healthy,))
        cur.execute("insert into slots (slot_ts, expected, captured, status) values (%s,95,0,'running')",
                    (stale,))
        cur.execute("insert into slots (slot_ts, expected, captured, status) values (%s,95,40,'partial')",
                    (partial,))

    problems = alerts.health(db, now=now)

    assert any("still running" in p for p in problems)
    assert any("partial: 40/95" in p for p in problems)
    assert any("no slot row" in p for p in problems)
    assert not any(slot_key_of(healthy) in p for p in problems), "a healthy slot is not a problem"


def test_health_reports_a_night_job_that_failed_or_never_ran(db):
    now = dt.datetime(2026, 10, 5, 3, 30, tzinfo=SLOT_TZ)
    with db.cursor() as cur:
        cur.execute("""insert into job_runs (job, target, ok, error, finished_at)
                       values ('night','2026-10-04',false,'PublishAborted: 11/12 slots', now())""")
    problems = alerts.health(db, now=now)
    assert any("night" in p and "2026-10-04" in p for p in problems)


def test_disk_usage_is_quiet_below_the_threshold_and_loud_above_it(monkeypatch, tmp_path):
    class Usage:
        total = 100 * 10**9
        used = 79 * 10**9
        free = 21 * 10**9

    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda path: Usage)
    assert alerts.disk_usage(tmp_path) is None

    Usage.used = 84 * 10**9
    assert "84%" in alerts.disk_usage(tmp_path)
