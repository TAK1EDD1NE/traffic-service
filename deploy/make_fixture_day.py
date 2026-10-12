#!/usr/bin/env python3
"""Lay a corpus day out the way capture writes it — and emit the SQL a `night` smoke needs.

Usage: python deploy/make_fixture_day.py [--day YYYY-MM-DD] [--zones A,B,C]
                                         [--corpus "../data engineering/screenshots"]
                                         [--out .superpowers/sdd/fixture-day]
                                         [--print-sql]

With no --day, picks the day with the most images for the chosen zones. Images land in the
EVEN-hour slot their capture hour floors to (the corpus samples a few odd hours — 05, 19,
21 — and the service's slots are even, spec:99-101). The filename is kept: the night job
strips the trailing timestamp to get the zone and never re-derives a name.

`--print-sql` writes the smoke's SQL to STDOUT instead of copying images (nothing else goes
to stdout, so the output pipes straight into psql). It is the rest of the acceptance test the
PNG layout alone is not: a `begin`/`commit` trim that reduces the registry to exactly the
zones this run kept, then the chosen day's twelve `slots` rows pre-set to
`expected = captured = <zone count>` with `status = 'ok'`. Without those rows `night` creates
them with `captured = 0` and `validate.check_day`'s 0.95 capture-ratio gate aborts the
publish — the day never lands. The two commands the smoke runs:

    python deploy/make_fixture_day.py
    python deploy/make_fixture_day.py --print-sql | \
      docker compose -f deploy/compose.prod.yml --env-file deploy/.env run --rm -T worker \
        sh -c 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1'

then `traffic-service night` over the emitted layout (the same day both runs chose, printed
by both). The zone trim names the zones it keeps — derived from this run's --zones, never a
wildcard — so it can never degrade into "delete every zone".

A corpus directory with no image for the chosen zones, or a `--day` that matches none, is a
hard error (message on stderr, exit 1): an acceptance tool must never report success on an
empty fixture. The tool itself is the smoke, so the failure must be loud.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
from collections import Counter

ZONES = ("Alger_1", "Alger_2", "Oran_1")

# Mirror slots.SLOT_TZ / slots.SLOT_HOURS: the index samples 12x/day on even LOCAL hours.
SLOT_TZ = "Africa/Algiers"
SLOT_HOURS = range(0, 24, 2)

# The tables the smoke resets, children first. The zone trim below is separate because it
# names the rows it keeps instead of clearing the table.
TRIM_TABLES = ("measurements", "captures", "slots", "ci_area_daily", "ci_city_daily",
               "index_daily")


def parse_pngs(corpus, zones):
    """`(zone, date, clock, path)` for every corpus PNG whose zone is in `zones`."""
    parsed = []
    for png in sorted(pathlib.Path(corpus).glob("*.png")):
        stem = png.stem.rsplit("_", 2)                 # <zone>_<date>_<HH-MM>
        if len(stem) == 3 and stem[0] in zones:
            parsed.append((stem[0], stem[1], stem[2], png))
    return parsed


def selected_day(images, requested):
    """`requested`, else the day with the most images (deterministic, lexicographic tie)."""
    return requested or Counter(date for _, date, _, _ in images).most_common(1)[0][0]


def sql_literal(values):
    """Postgres `('a', 'b')`; values are zone ids, so `'` is the only character to escape."""
    return "(" + ", ".join("'" + value.replace("'", "''") + "'" for value in values) + ")"


def print_sql(day, kept_zones, zone_count, stream):
    """The smoke's SQL: trim to `kept_zones`, then the day's twelve pre-captured slots."""
    year, month, dom = (int(part) for part in day.split("-"))
    lines = [
        f"-- fixture smoke: {day}, zones {', '.join(kept_zones)} "
        f"(emitted by deploy/make_fixture_day.py --print-sql)",
        "begin;",
    ]
    lines += [f"delete from {table};" for table in TRIM_TABLES]
    lines.append(f"delete from zones where zone_id not in {sql_literal(kept_zones)};")
    slots = ",\n  ".join(
        f"(make_timestamptz({year}, {month}, {dom}, {hour}, 0, 0, '{SLOT_TZ}'), "
        f"{zone_count}, {zone_count}, 'ok')"
        for hour in SLOT_HOURS
    )
    lines.append("insert into slots (slot_ts, expected, captured, status) values")
    lines.append("  " + slots)
    lines.append("on conflict (slot_ts) do update "
                 "set expected = excluded.expected, captured = excluded.captured, "
                 "status = excluded.status;")
    lines.append("commit;")
    stream.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default=None)
    parser.add_argument("--zones", default=",".join(ZONES))
    parser.add_argument("--corpus", default="../data engineering/screenshots")
    parser.add_argument("--out", default=".superpowers/sdd/fixture-day")
    parser.add_argument("--print-sql", action="store_true",
                        help="write the smoke's SQL to stdout instead of copying images")
    args = parser.parse_args()

    zones = {zone.strip() for zone in args.zones.split(",") if zone.strip()}
    if not pathlib.Path(args.corpus).is_dir():
        print(f"error: corpus directory not found: {args.corpus}", file=sys.stderr)
        return 1

    images = parse_pngs(args.corpus, zones)
    if not images:
        print(f"error: no corpus images for zones {sorted(zones)} under {args.corpus}",
              file=sys.stderr)
        return 1

    day = selected_day(images, args.day)
    day_images = [row for row in images if row[1] == day]
    if not day_images:
        print(f"error: no images for {day} (zones {sorted(zones)}) under {args.corpus}",
              file=sys.stderr)
        return 1

    kept_zones = sorted({zone for zone, _, _, _ in day_images})
    if args.print_sql:
        print_sql(day, kept_zones, len(kept_zones), sys.stdout)
        return 0

    out = pathlib.Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    for _, _, clock, png in day_images:
        hour = int(clock.split("-")[0])
        destination = out / f"{day}T{hour - hour % 2:02d}-00" / png.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(png, destination)
    print(f"{len(day_images)} images for {day} in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
