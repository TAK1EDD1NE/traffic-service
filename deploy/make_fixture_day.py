#!/usr/bin/env python3
"""Lay a corpus day out the way capture writes it: <out>/<slot_key>/<png>.

Usage: python deploy/make_fixture_day.py [--day YYYY-MM-DD] [--zones A,B,C]
                                         [--corpus "../data engineering/screenshots"]
                                         [--out .superpowers/fixture-day]

With no --day, picks the day with the most images for the chosen zones. Images land in the
EVEN-hour slot their capture hour floors to (the corpus samples a few odd hours — 05, 19,
21 — and the service's slots are even, spec:99-101). The filename is kept: the night job
strips the trailing timestamp to get the zone and never re-derives a name.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
from collections import Counter

ZONES = ("Alger_1", "Alger_2", "Oran_1")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default=None)
    parser.add_argument("--zones", default=",".join(ZONES))
    parser.add_argument("--corpus", default="../data engineering/screenshots")
    parser.add_argument("--out", default=".superpowers/fixture-day")
    args = parser.parse_args()

    zones = {zone.strip() for zone in args.zones.split(",") if zone.strip()}
    images = []
    for png in sorted(pathlib.Path(args.corpus).glob("*.png")):
        stem = png.stem.rsplit("_", 2)                 # <zone>_<date>_<HH-MM>
        if len(stem) == 3 and stem[0] in zones:
            images.append((stem[0], stem[1], stem[2], png))

    day = args.day or Counter(d for _, d, _, _ in images).most_common(1)[0][0]
    out = pathlib.Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    copied = 0
    for _, date, clock, png in images:
        if date != day:
            continue
        hour = int(clock.split("-")[0])
        destination = out / f"{date}T{hour - hour % 2:02d}-00" / png.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(png, destination)
        copied += 1
    print(f"{copied} images for {day} in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
