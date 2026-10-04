"""Build the zone registry from the two artefacts that already exist.

`locations.json` decides which coordinates are captured (95). `traffic_areas.csv`
decides which of them are published (89) and carries the frozen v2 numbers:
`capacity_m` is K (p99 observed metres per area) and `freeflow` is the p20 floor.

Nothing here re-derives those two numbers. Recomputing them is a deliberate act
that ships a new `floor_version`; see the spec's open item on the floor choice.
"""
from __future__ import annotations

import csv
import json
import pathlib

from traffic_service.zones import city_slug, corpus_city, zone_id

DEFAULT_FLOOR_VERSION = "p20-v2"


def _corpus_floors(areas_path: pathlib.Path) -> dict[tuple[str, int], tuple[float, float]]:
    """(city, area) -> (capacity_m, freeflow) for the published areas."""
    with pathlib.Path(areas_path).open(encoding="utf-8") as handle:
        return {
            (row["city"], int(row["area"])): (float(row["capacity_m"]), float(row["freeflow"]))
            for row in csv.DictReader(handle)
        }


def read_zones(locations_path, areas_path) -> list[dict]:
    payload = json.loads(pathlib.Path(locations_path).read_text(encoding="utf-8"))
    floors = _corpus_floors(pathlib.Path(areas_path))

    zones: list[dict] = []
    for entry in payload["cities"]:
        name = entry["city"]
        slug = city_slug(name)
        city = corpus_city(name)
        scale = float(entry["scale"])
        for position, coordinate in enumerate(entry["coordinates"], start=1):
            if isinstance(coordinate, dict):
                lat, lng = float(coordinate["lat"]), float(coordinate["lng"])
            else:
                lat, lng = float(coordinate[0]), float(coordinate[1])
            capacity_k, floor_p20 = floors.get((city, position), (None, None))
            zones.append({
                "zone_id": zone_id(slug, position),
                "city": city,
                "city_slug": slug,
                "area_no": position,
                "name": name,
                "lat": lat,
                "lng": lng,
                "scale": scale,
                "publish": capacity_k is not None,
                "capacity_k": capacity_k,
                "floor_p20": floor_p20,
            })
    return zones


UPSERT = """
insert into zones (zone_id, city, city_slug, area_no, name, lat, lng, scale,
                   publish, capacity_k, floor_p20, floor_version)
values (%(zone_id)s, %(city)s, %(city_slug)s, %(area_no)s, %(name)s, %(lat)s,
        %(lng)s, %(scale)s, %(publish)s, %(capacity_k)s, %(floor_p20)s, %(floor_version)s)
on conflict (zone_id) do update set
    city = excluded.city,
    city_slug = excluded.city_slug,
    area_no = excluded.area_no,
    name = excluded.name,
    lat = excluded.lat,
    lng = excluded.lng,
    scale = excluded.scale,
    publish = excluded.publish,
    capacity_k = excluded.capacity_k,
    floor_p20 = excluded.floor_p20,
    floor_version = excluded.floor_version
"""


def seed_zones(conn, locations_path, areas_path,
               floor_version: str = DEFAULT_FLOOR_VERSION) -> int:
    zones = [{**zone, "floor_version": floor_version}
             for zone in read_zones(locations_path, areas_path)]
    with conn.cursor() as cur:
        cur.executemany(UPSERT, zones)
    conn.commit()
    return len(zones)
