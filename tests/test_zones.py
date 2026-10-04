import json
import pathlib
import csv

import pytest

from traffic_service.zones import city_slug, corpus_city, zone_id

DE = pathlib.Path(__file__).resolve().parents[2] / "data engineering"
LOCATIONS = DE / "locations.json"
AREAS_CSV = DE / "traffic_areas.csv"


@pytest.mark.parametrize("name,slug", [
    ("Alger", "Alger"),
    ("Tizi Ouzou", "Tizi_Ouzou"),
    ("Béjaïa", "B_ja_a"),
    ("Sétif", "S_tif"),
    ("Médéa", "M_d_a"),
    ("M'Sila", "M_Sila"),
    ("Boumerdès", "Boumerd_s"),
    ("El Tarf", "El_Tarf"),
    ("Bordj Bou Arréridj", "Bordj_Bou_Arr_ridj"),
    ("Sidi Bel Abbès", "Sidi_Bel_Abb_s"),
    ("Aïn Témouchent", "A_n_T_mouchent"),
])
def test_city_slug_matches_the_filenames_the_scraper_writes(name, slug):
    assert city_slug(name) == slug


@pytest.mark.parametrize("name,city", [
    ("Alger", "Alger"),
    ("Béjaïa", "Bejaia"),
    ("Sétif", "Setif"),
    ("Médéa", "Medea"),
    ("M'Sila", "MSila"),
    ("Boumerdès", "Boumerdes"),
    ("Sidi Bel Abbès", "Sidi Bel Abbes"),
    ("Bordj Bou Arréridj", "Bordj Bou Arreridj"),
    ("Aïn Témouchent", "Ain Temouchent"),
])
def test_corpus_city_matches_the_csv(name, city):
    assert corpus_city(name) == city


def test_every_locations_city_has_a_corpus_row():
    """The seed joins locations.json to the corpus; a mismatch is a silent data gap."""
    locations = json.loads(LOCATIONS.read_text(encoding="utf-8"))
    csv_cities = {r["city"] for r in csv.DictReader(AREAS_CSV.open(encoding="utf-8"))}
    missing = sorted(corpus_city(c["city"]) for c in locations["cities"]
                     if corpus_city(c["city"]) not in csv_cities)
    assert missing == []


def test_zone_id_is_slug_plus_area():
    assert zone_id("Alger", 1) == "Alger_1"
    assert zone_id("A_n_T_mouchent", 2) == "A_n_T_mouchent_2"
