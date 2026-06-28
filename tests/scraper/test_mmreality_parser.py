"""Hermetic tests for scraper.mmreality_parser against hand-authored fixtures.

The fixtures mirror the real mmreality markup: index cards are
`<a data-card-id href="/nemovitosti/{id}/">` wrapping a `[data-realty-price]`
button, and a detail page embeds the estate object as an HTML-entity-encoded
Vue prop (`:property="{…}"`). `_property_attr` reproduces that encoding so the
tests exercise the same decode path the live page hits.
"""

from __future__ import annotations

import html as ihtml
import json
from typing import Any

from scraper.mmreality_parser import (
    _building_type,
    _condition,
    _ownership,
    index_price,
    parse_detail,
    parse_index,
)


def test_ownership_canonical_only():
    assert _ownership({"ownership": {"name": "Osobní"}}) == "osobni"
    assert _ownership({"ownership": {"name": "Družstevní"}}) == "druzstevni"
    assert _ownership({"ownership": {"name": "Obecní"}}) == "statni"
    # Unmapped labels collapse to None, never leak a value no filter can match.
    assert _ownership({"ownership": {"name": "Jiné"}}) is None
    assert _ownership({"ownership": {"name": "Ostatní"}}) is None
    assert _ownership({}) is None


def test_building_type_canonical_only():
    # Real mmreality values are nouns that already equal a canonical code.
    assert _building_type({"construction": {"name": "Cihla"}}) == "cihla"
    assert _building_type({"construction": {"name": "Panel"}}) == "panel"
    assert _building_type({"construction": {"name": "Smíšená"}}) == "smisena"
    # The "neuvedeno" ("not specified") placeholder must NOT leak into the column.
    assert _building_type({"construction": {"name": "neuvedeno"}}) is None
    assert _building_type({}) is None


def test_condition_canonical_only():
    assert _condition({"condition": {"name": "novostavba"}}) == "novostavba"
    assert _condition({"condition": {"name": "velmi dobrý"}}) == "velmi_dobry"
    assert _condition({"condition": {"name": "dobrý"}}) == "dobry"
    # Defensive "… stav" stripping (idnes form) still lands on the canonical value.
    assert _condition({"condition": {"name": "Velmi dobrý stav"}}) == "velmi_dobry"
    # Placeholder / unknown labels collapse to None instead of leaking.
    assert _condition({"condition": {"name": "neuvedeno"}}) is None
    assert _condition({}) is None

INDEX_HTML = """
<!DOCTYPE html><html lang="cs"><head>
  <link rel="next" href="?page=2"/>
</head><body>
  <a href="https://www.mmreality.cz/nemovitosti/944445/" data-card-id="944445" class="card">
    <article class="rds-property-preview-card">
      <button data-realty-id="944445" data-realty-name="Prodej, Byt 2+1, 54 m², Pacov, Na Blatech" data-realty-price="3 190 000 Kč"></button>
      <h4 class="rds-property-title">Prodej bytu 2+1, 54 m², Pacov, ul. Na Blatech</h4>
      <div class="tw-text-text-price">3 190 000 Kč</div>
    </article>
  </a>
  <a href="https://www.mmreality.cz/nemovitosti/944444/" data-card-id="944444" class="card">
    <article class="rds-property-preview-card">
      <button data-realty-id="944444" data-realty-name="Pronájem, Byt 1+kk, Brno" data-realty-price="Cena dohodou"></button>
      <h4 class="rds-property-title">Pronájem bytu 1+kk, Brno</h4>
    </article>
  </a>
</body></html>
"""


def _property_attr(obj: dict[str, Any]) -> str:
    """Embed an estate object the way the live page does: a `:property` Vue prop
    whose JSON is HTML-entity-encoded (so `"` becomes `&quot;`)."""
    return ':property="' + ihtml.escape(json.dumps(obj, ensure_ascii=False)) + '"'


ESTATE = {
    "id": "944445",
    "point": {"latitude": "49.47841185", "longitude": "15.003274356"},
    "title": "Prodej, Byt 2+1, 54 m², Pacov, Na Blatech",
    "location": "Na Blatech, Pacov",
    "category": {"id": "10", "name": "Prodej"},
    "group": {"id": "11", "name": "Byt"},
    "type": {"id": "54", "name": "Byt 2+1"},
    "totalArea": "54",
    "usableArea": "54",
    "district": "Pelhřimov",
    "municipality": "Pacov",
    "municipalityPart": "Pacov",
    "street": "Na Blatech",
    "active": "True",
    "description": "Nabízíme k prodeji světlý byt 2+1 o užitné ploše 54 m².",
    "price": "3190000",
    "condition": {"id": "10", "name": "velmi dobrý"},
    "construction": {"id": "8", "name": "Smíšená"},
    "energyClassification": {"id": "7", "code": "G", "name": "Mimořádně nehospodárná"},
    "ownership": {"id": "1", "name": "Družstevní"},
    "floor": "5",
    "overgroundFloors": "4",
    "undergroundFloors": "1",
    "lift": "False",
    "parkingPlaces": "1",
    "cellar": "True",
    "images": [
        {
            "id": "40411597",
            "previews": {
                "xlarge": "https://cdn.mmreality.cz/xlarge/offer/f1/95/a.jpg",
                "medium": "https://cdn.mmreality.cz/medium/offer/f1/95/a.jpg",
            },
        },
        {
            "id": "40411598",
            "previews": {"medium": "https://cdn.mmreality.cz/medium/offer/76/c1/b.jpg"},
        },
    ],
    "accessoryGroups": [
        {"name": "Parkování", "accessories": [{"name": "Garáž"}]},
        {"name": "Vedlejší prostory a stavby", "accessories": [{"name": "Balkón"}]},
    ],
}

# A decoy related-card prop with a different id — extract_property must skip it.
DECOY = {"id": "111111", "title": "Other", "price": "9", "category": {"name": "Prodej"}}


def _detail_html(*objs: dict[str, Any]) -> str:
    cards = "\n".join(
        f'<vue-property-preview-card {_property_attr(o)}></vue-property-preview-card>'
        for o in objs
    )
    return f"<!DOCTYPE html><html><body>{cards}</body></html>"


def test_parse_index_items_and_next_page():
    page = parse_index(INDEX_HTML)
    assert page.next_offset == 2
    assert len(page.items) == 2

    first = page.items[0]
    assert first.source_id_native == "944445"
    assert first.detail_path == "https://www.mmreality.cz/nemovitosti/944445/"
    assert first.price_text == "3 190 000 Kč"
    assert "Byt 2+1" in (first.title or "")


def test_index_price_parsing():
    assert index_price("3 190 000 Kč") == 3_190_000
    assert index_price("Cena dohodou") is None
    assert index_price(None) is None


def test_parse_detail_full_mapping():
    url = "https://www.mmreality.cz/nemovitosti/944445/"
    listing = parse_detail(_detail_html(DECOY, ESTATE), source_url=url)

    assert listing.source == "mmreality"
    assert listing.source_id_native == "944445"
    assert listing.source_url == url
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 3_190_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 54.0
    assert listing.usable_area == 54.0
    assert listing.disposition == "2+1"
    assert listing.locality == "Na Blatech, Pacov"
    assert listing.street == "Na Blatech"
    assert listing.district == "Pelhřimov"
    assert listing.lat == 49.47841185
    assert listing.lon == 15.003274356
    assert listing.condition == "velmi_dobry"
    assert listing.building_type == "smisena"
    assert listing.ownership == "druzstevni"
    assert listing.energy_rating == "G"
    assert listing.floor == 5
    assert listing.total_floors == 5
    assert listing.has_lift is False
    assert listing.cellar is True
    assert listing.parking_lots == 1
    assert listing.has_parking is True
    assert listing.garage is True
    assert listing.has_balcony is True
    assert listing.description.startswith("Nabízíme")
    assert listing.raw["image_urls"] == [
        "https://cdn.mmreality.cz/xlarge/offer/f1/95/a.jpg",
        "https://cdn.mmreality.cz/medium/offer/76/c1/b.jpg",
    ]


def test_parse_detail_rent_price_unit():
    rent = {**ESTATE, "id": "5", "category": {"name": "Pronájem"}, "price": "15000"}
    url = "https://www.mmreality.cz/nemovitosti/5/"
    listing = parse_detail(_detail_html(rent), source_url=url)
    assert listing.category_type == "pronajem"
    assert listing.price_unit == "za mesic"
    assert listing.price_czk == 15_000


def test_parse_detail_picks_matching_id_not_largest():
    # The decoy is smaller, but the matcher must select by id, not by size.
    url = "https://www.mmreality.cz/nemovitosti/944445/"
    listing = parse_detail(_detail_html(ESTATE, DECOY), source_url=url)
    assert listing.source_id_native == "944445"


def test_content_hash_stable_and_bridges_to_ingest():
    url = "https://www.mmreality.cz/nemovitosti/944445/"
    a = parse_detail(_detail_html(ESTATE), source_url=url)
    b = parse_detail(_detail_html(ESTATE), source_url=url)
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64

    row = a.to_row(-7)
    assert row["sreality_id"] == -7
    assert row["category_main"] == "byt"
    assert row["price_czk"] == 3_190_000
    assert row["lat"] == 49.47841185
