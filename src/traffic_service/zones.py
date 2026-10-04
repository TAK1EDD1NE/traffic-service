"""Zone identity. Two names for every city, and they are not interchangeable.

- `city_slug`    the form the scraper writes into PNG filenames (accents folded
                 to underscores, punctuation dropped).
- `corpus_city`  the de-accented form used by the historical CSVs, which is what
                 the zone registry joins on.

Both are pure functions of the `locations.json` display name, so there is no
lookup table to drift.
"""
from __future__ import annotations

import re
import unicodedata

_NON_SLUG = re.compile(r"[^A-Za-z0-9_-]+")
_NON_LETTERS = re.compile(r"[^A-Za-z ]+")


def city_slug(name: str) -> str:
    """Same transformation as google_maps_scraper.sanitize_city_name."""
    return _NON_SLUG.sub("_", name).strip("_") or "city"


def corpus_city(name: str) -> str:
    """De-accented, punctuation-free city name, as it appears in the corpus CSVs."""
    decomposed = unicodedata.normalize("NFKD", name)
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NON_LETTERS.sub("", unaccented).split())


def zone_id(slug: str, area_no: int) -> str:
    return f"{slug}_{area_no}"
