"""Single entry point: `traffic-service <command>`."""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from datetime import datetime

from traffic_service.capture import capture_slot_guarded, load_zones
from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.seed import DEFAULT_FLOOR_VERSION, seed_zones
from traffic_service.slots import SLOT_TZ, slot_for, slot_key

DATA_ENGINEERING = pathlib.Path(__file__).resolve().parents[3] / "data engineering"
DEFAULT_RAW_ROOT = pathlib.Path("/data/raw")


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
    sub.add_parser("scheduler", help="run the 12-per-day capture schedule until interrupted")
    return parser


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
    from traffic_service.scheduler import build, slot_hours

    scheduler = build(args.raw_root, capturer=resolve_capturer())
    scheduler.start()
    print("scheduler started; capture at " +
          ", ".join(f"{hour:02d}:00" for hour, _ in slot_hours()) + " Africa/Algiers")
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


COMMANDS = {"migrate": cmd_migrate, "doctor": cmd_doctor, "seed-zones": cmd_seed_zones,
            "capture": cmd_capture, "scheduler": cmd_scheduler}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
