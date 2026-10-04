"""Single entry point: `traffic-service <command>`."""
from __future__ import annotations

import argparse
import pathlib
import sys

from traffic_service.db import connect
from traffic_service.migrate import migrate
from traffic_service.seed import DEFAULT_FLOOR_VERSION, seed_zones

DATA_ENGINEERING = pathlib.Path(__file__).resolve().parents[3] / "data engineering"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traffic-service")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending SQL migrations")
    sub.add_parser("doctor", help="report schema and registry state")
    seed = sub.add_parser("seed-zones", help="upsert the zone registry from the corpus")
    seed.add_argument("--locations", default=str(DATA_ENGINEERING / "locations.json"))
    seed.add_argument("--areas", default=str(DATA_ENGINEERING / "traffic_areas.csv"))
    seed.add_argument("--floor-version", default=DEFAULT_FLOOR_VERSION)
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
    print(f"migrations: {migrations}\nzones: {zones}\npublished: {published}\n"
          f"published zones missing K: {broken}")
    if migrations < 1 or zones != 95 or published != 89 or broken:
        print("UNHEALTHY", file=sys.stderr)
        return 1
    return 0


COMMANDS = {"migrate": cmd_migrate, "doctor": cmd_doctor, "seed-zones": cmd_seed_zones}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
