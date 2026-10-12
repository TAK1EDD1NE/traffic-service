"""Single entry point: `traffic-service <command>`."""
from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import time
from datetime import datetime, timedelta

from traffic_service import alerts, dump, maintenance, night, publish
from traffic_service.capture import capture_slot_guarded, load_zones
from traffic_service.db import connect
from traffic_service.jobs import JobLocked
from traffic_service.metrics import DEFAULT_WORKERS
from traffic_service.migrate import migrate
from traffic_service.seed import DEFAULT_FLOOR_VERSION, seed_zones
from traffic_service.slots import SLOT_TZ, slot_for, slot_key

DATA_ENGINEERING = pathlib.Path(__file__).resolve().parents[3] / "data engineering"
DEFAULT_RAW_ROOT = pathlib.Path("/data/raw")
# The site's data directory — where `publish.publish_day` writes the nine CSVs and the manifest.
DEFAULT_OUT_DIR = pathlib.Path("/data/out")
# The nightly `pg_dump` directory. The default lives in `maintenance` (the scheduler cannot import
# `cli`); this alias is the parser's default and the value Task 4's compose mount relies on.
DEFAULT_DUMP_DIR = maintenance.DEFAULT_DUMP_DIR


def configure_logging(level=logging.INFO):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(level)   # explicit, so it is observable and testable


def resolve_capturer():
    """Indirection so tests can run a slot without Playwright."""
    from traffic_service.browser import capturer
    return capturer


def parse_slot(value: str) -> datetime:
    """`now`, or an ISO timestamp — naive values are read as local (Algiers)."""
    if value.lower() == "now":
        return slot_for(datetime.now(SLOT_TZ))
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise SystemExit(f"--slot must be 'now' or an ISO timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SLOT_TZ)
    return slot_for(parsed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traffic-service")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending SQL migrations")
    sub.add_parser("doctor", help="report schema and registry state")
    seed = sub.add_parser("seed-zones", help="upsert the zone registry from the corpus")
    seed.add_argument("--locations", default=str(DATA_ENGINEERING / "locations.json"))
    seed.add_argument("--areas", default=str(DATA_ENGINEERING / "traffic_areas.csv"))
    seed.add_argument("--floor-version", default=DEFAULT_FLOOR_VERSION)
    capture = sub.add_parser("capture", help="capture one slot")
    capture.add_argument("--slot", default="now", help="'now' or an ISO timestamp")
    capture.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    capture.add_argument("--limit", type=int, default=None, help="capture only the first N zones")
    capture.add_argument("--force", action="store_true")
    sched = sub.add_parser("scheduler", help="run the capture + night + maintenance schedule")
    sched.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    night_parser = sub.add_parser("night", help="measure, publish, and prune one closed day")
    night_parser.add_argument("--day", default="yesterday",
                              help="'yesterday' or YYYY-MM-DD (local, Africa/Algiers)")
    night_parser.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    night_parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                              help="the site's data directory the publish writes")
    night_parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    maint = sub.add_parser("maintenance", help="dump, prune raw PNGs, VACUUM and report slot health")
    maint.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    maint.add_argument("--dump-dir", default=str(DEFAULT_DUMP_DIR),
                       help="where the nightly pg_dump is written")
    maint.add_argument("--day", default=None,
                       help="the job_runs target and dump file name, YYYY-MM-DD (default: today)")
    return parser


def resolve_day(value: str):
    """`yesterday`, or `YYYY-MM-DD` — the local day (Africa/Algiers) a night pass closes.

    `yesterday` is the local day before today, not a UTC-relative one: the service's days and
    slots are all on the Algiers wall clock, so a UTC "yesterday" would name the wrong day for the
    six hours after local midnight.
    """
    if value.lower() == "yesterday":
        return (datetime.now(SLOT_TZ) - timedelta(days=1)).date()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise SystemExit(f"--day must be 'yesterday' or YYYY-MM-DD: {value!r}") from error


def cmd_migrate(_args) -> int:
    with connect() as conn:
        applied_now = migrate(conn)
    print(f"migrations applied: {len(applied_now)}")
    return 0


def cmd_seed_zones(args) -> int:
    with connect() as conn:
        written = seed_zones(conn, args.locations, args.areas, args.floor_version)
    print(f"zones upserted: {written} (floor_version={args.floor_version})")
    return 0


def cmd_capture(args) -> int:
    slot_ts = parse_slot(args.slot)
    with connect() as conn:
        zones = load_zones(conn)
        if args.limit is not None:
            zones = zones[: args.limit]
        result = capture_slot_guarded(conn, slot_ts, raw_root=args.raw_root,
                                      capturer=resolve_capturer(), zones=zones,
                                      force=args.force)
    print(f"slot: {slot_key(slot_ts)}")
    print(f"status: {result['status']}")
    print(f"captured: {result['captured']}")
    print(f"failed: {result['failed']}")
    print(f"skipped: {result['skipped']}")
    print(f"dir: {result['dir']}")
    return 0 if result["status"] == "ok" else 1


def cmd_scheduler(args) -> int:
    from traffic_service.scheduler import build, catch_up, slot_hours

    capturer = resolve_capturer()
    catch_up(args.raw_root, capturer=capturer)
    scheduler = build(args.raw_root, out_dir=DEFAULT_OUT_DIR, capturer=capturer)
    scheduler.start()
    print("scheduler started; capture at " +
          ", ".join(f"{hour:02d}:00" for hour, _ in slot_hours()) +
          "; night at 00:30; maintenance at 03:30 Africa/Algiers")
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=True)
    return 0


def cmd_doctor(_args) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from schema_migrations")
        migrations = cur.fetchone()[0]
        cur.execute("select count(*) from zones")
        zones = cur.fetchone()[0]
        cur.execute("select count(*) from zones where publish")
        published = cur.fetchone()[0]
        cur.execute("select count(*) from zones where publish and capacity_k is null")
        broken = cur.fetchone()[0]
        cur.execute("select slot_ts, status from slots order by slot_ts desc limit 1")
        last_slot = cur.fetchone()
    print(f"migrations: {migrations}\nzones: {zones}\npublished: {published}\n"
          f"published zones missing K: {broken}")
    if last_slot:
        print(f"last slot: {slot_key(last_slot[0])} {last_slot[1]}")
    if migrations < 1 or zones != 95 or published != 89 or broken:
        print("UNHEALTHY", file=sys.stderr)
        return 1
    return 0


def cmd_night(args) -> int:
    """Measure, publish and prune one closed day. A `PublishAborted` is the alert.

    The non-zero exit on an aborted publish IS the alerting hook (plan 4 wires the channel): the
    reasons are printed to stderr and `job_runs.ok` is false. Nothing is pruned on an abort, so the
    day's pixels are still there for the next attempt.

    Two non-failures must not look like failures to that channel: a `JobLocked` (another run holds
    `night:<day>` — a cron overlap, or an operator hitting a day the scheduler is already doing)
    prints one line and exits 0, exactly as `scheduler.run_night_job` swallows it; and a failed
    prune after a publish that succeeded is a WARNING on stderr with exit 0, because the day is on
    the site (the 03:30 `maintenance` pass re-attempts that prune on its own `job_runs` row).
    """
    day = resolve_day(args.day)
    try:
        with connect() as conn:
            summary = night.run_day(conn, day, args.raw_root, args.out_dir, workers=args.workers)
    except publish.PublishAborted as error:
        alerts.notify(f"night {day} aborted: {error}")
        print(f"night {day} aborted: {error}", file=sys.stderr)
        return 1
    except JobLocked as error:
        print(f"night {day} skipped: {error}")
        return 0

    published = summary["publish"]
    pruned = summary["prune"]
    print(f"day: {day}")
    print(f"images: {summary['images']}  measured: {summary['measured']}  "
          f"skipped: {summary['skipped']}  errors: {summary['errors']}  "
          f"slots: {summary['slots']}  unknown: {len(summary['unknown'])}")
    print(f"areas: {summary['areas']}  cities: {summary['cities']}  days: {summary['days']}")
    # The day's errored measurement rows, from the validation summary: the readable slots still
    # publish, and this is how many rows the derivation dropped for that day (day-wide, so a
    # re-run reports the same number as the run that wrote them).
    print(f"error_rows: {published['error_rows']}")
    print(f"data_version: {published['data_version']}")
    print(f"pruned: {pruned['slots']} slot(s), {pruned['bytes']} byte(s)")
    if "error" in pruned:
        print(f"warning: night {day} published, but the prune failed: {pruned['error']}",
              file=sys.stderr)
    return 0


def cmd_maintenance(args) -> int:
    """Dump, prune, VACUUM and print the health summary `maintenance.run_maintenance` returns.

    The work — and the alerting — lives in `maintenance.run_maintenance` (R18/R21), so the 03:30
    scheduled run and this command share one implementation; this is a parse → call → print →
    exit-code wrapper and must not re-implement any of it. `--day` (an ISO date, default today) is
    both the `job_runs` target and the dump's file name (R26); a failed `pg_dump` raises out of
    the module and is exit 1.
    """
    now = datetime.now(SLOT_TZ)
    if args.day:
        try:
            day = datetime.strptime(args.day, "%Y-%m-%d").date()
        except ValueError as error:
            raise SystemExit(f"--day must be YYYY-MM-DD: {args.day!r}") from error
    else:
        day = None
    try:
        with connect() as conn:
            summary = maintenance.run_maintenance(conn, args.raw_root, now=now,
                                                  dump_dir=args.dump_dir, day=day)
    except dump.DumpFailed as error:
        print(f"maintenance failed: {error}", file=sys.stderr)
        return 1

    pruned = summary["prune"]
    print(f"pruned: {pruned['slots']} slot(s), {pruned['bytes']} byte(s)")
    print(f"vacuum: {summary['vacuum']}")
    print(f"dump: {summary.get('dump', 'skipped')}")
    print(f"slots (last {maintenance.HEALTH_WINDOW_DAYS} days): {len(summary['slots'])}")
    for slot in summary["slots"]:
        print(f"  {slot['slot']} {slot['status']} {slot['captured']}/{slot['expected']} "
              f"failed={slot['failed']}")
    # The alertable problems are already sent by `run_maintenance`; they are echoed here so an
    # operator reading the command's output sees the same list the alert channel got.
    for problem in summary["problems"]:
        print(f"warning: {problem}", file=sys.stderr)
    return 0


COMMANDS = {"migrate": cmd_migrate, "doctor": cmd_doctor, "seed-zones": cmd_seed_zones,
            "capture": cmd_capture, "scheduler": cmd_scheduler, "night": cmd_night,
            "maintenance": cmd_maintenance}


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
