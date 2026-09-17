"""Hermetic tests for scraper.mmreality_parser against hand-authored fixtures.

The fixtures mirror the real mmreality markup: index cards are
`<a data-card-id href="/nemovitosti/{id}/">` wrapping a `[data-realty-price]`
button, and a detail page embeds the estate object as an HTML-entity-encoded
Vue prop (`:property="{…}"`). `_property_attr` reproduces that encoding so the
tests exercise the same decode path the live page hits.
"""

from __future__ import annotations

import pytest

import html as ihtml
import json
import pathlib
from typing import Any

from scraper.mmreality_parser import PropertyMismatch, declared_total, extract_property
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


SSR_HTML = (
    '<html><body><vue-property-list-grid namespace="list" '
    ':ssr="{&quot;offers&quot;:[{&quot;id&quot;:954007}],'
    '&quot;metadata&quot;:{&quot;count&quot;:1643,&quot;groups&quot;:[]},&quot;page&quot;:1}">'
    '</vue-property-list-grid><link rel="next" href="?page=2"/></body></html>'
)


def test_parse_index_declared_total_from_the_ssr_state():
    """The per-type index declares its own result count in the page's Vue SSR
    state (entity-encoded JSON). Absent state is None, never 0: an unmeasurable
    page must read as unknown, not as an empty category (rule #3)."""
    assert declared_total(SSR_HTML) == 1643
    assert declared_total(SSR_HTML.replace("&quot;", '"')) == 1643
    assert parse_index(SSR_HTML).total == 1643
    assert parse_index(SSR_HTML).next_offset == 2
    assert declared_total(INDEX_HTML) is None
    assert parse_index(INDEX_HTML).total is None


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
    assert listing.area_basis == "usable"
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


# A HOUSE, the discriminating case: the byt fixture above sets totalArea and
# usableArea to the same "54", so it passes under any precedence and proves nothing.
# `parcelArea` is the page's "Plocha parcely" — the ONLY parcel measure mmreality
# publishes — and `totalArea` is the page's own `parcelArea + usableArea` SUM, which
# the pre-W21 parser stored as the plot (1,178 active houses, 44-50% too large) and
# before W1 as the headline (1,515 more, still carrying it).
HOUSE = {
    **ESTATE,
    "id": "944446",
    "title": "Prodej, Dům rodinný, 130 m², Pacov",
    "group": {"id": "12", "name": "Dům"},
    "type": {"id": "60", "name": "Dům 5+1"},
    "builtUpArea": "120",
    "gardenArea": "655",
    "parcelArea": "775",
    "usableArea": "130",
    "totalArea": "905",
}


def test_house_headline_area_is_the_interior_not_the_plot():
    url = "https://www.mmreality.cz/nemovitosti/944446/"
    listing = parse_detail(_detail_html(HOUSE), source_url=url)

    assert listing.category_main == "dum"
    assert listing.area_m2 == 130.0
    assert listing.area_basis == "usable"
    assert listing.usable_area == 130.0
    assert listing.garden_area == 655.0


def test_house_plot_is_parcel_area_never_the_total_sum():
    # W21. `totalArea` (905) is parcelArea + usableArea, a figure the PAGE derives;
    # the parcel is `parcelArea` (775). Storing the sum inflated every mmreality
    # house's plot by exactly its own floor area.
    listing = parse_detail(
        _detail_html(HOUSE), source_url="https://www.mmreality.cz/nemovitosti/944446/"
    )
    assert listing.estate_area == 775.0
    assert listing.estate_area != float(HOUSE["totalArea"])


def test_the_phantom_parcel_keys_are_not_consulted():
    # `landArea` / `plotArea` are keys mmreality declares and never fills: a value
    # on ZERO of 14,417 stored rows. Reading them first was what made `totalArea`
    # the de-facto parcel. A page that DID fill them must not win over the one cell
    # the portal actually labels "Plocha parcely".
    house = {**HOUSE, "id": "944449", "landArea": "9999", "plotArea": "8888"}
    listing = parse_detail(
        _detail_html(house), source_url="https://www.mmreality.cz/nemovitosti/944449/"
    )
    assert listing.estate_area == 775.0


def test_house_without_an_interior_measure_is_null_not_a_plot():
    # 11 mmreality rows corpus-wide state no usableArea AND no parcelArea. Handing
    # `totalArea` to the generic `total` slot would stamp a derived sum as an
    # interior measure — a confident wrong label is worse than the missing value it
    # replaces, so the headline goes NULL.
    house = {**HOUSE, "id": "944448", "usableArea": None, "parcelArea": None}
    listing = parse_detail(
        _detail_html(house), source_url="https://www.mmreality.cz/nemovitosti/944448/"
    )
    assert listing.category_main == "dum"
    assert listing.area_m2 is None
    assert listing.area_basis is None
    assert listing.estate_area is None


def test_land_headline_area_is_the_parcel_cell():
    # Option A: for a pozemek the headline IS the parcel. On land mmreality's
    # `totalArea` equals `parcelArea` exactly (0 of 4,568 stored land rows disagree,
    # because a parcel has no interior to add) — so reading the labelled cell instead
    # of the sum changes no land value and stops relying on that coincidence.
    land = {
        **ESTATE,
        "id": "944447",
        "group": {"id": "13", "name": "Pozemek"},
        "parcelArea": "905",
        "totalArea": "905",
        "usableArea": None,
    }
    listing = parse_detail(
        _detail_html(land), source_url="https://www.mmreality.cz/nemovitosti/944447/"
    )
    assert listing.category_main == "pozemek"
    assert listing.area_m2 == 905.0
    assert listing.area_basis == "plot"
    assert listing.estate_area == 905.0


def test_real_capture_house_areas():
    """The archived mmreality detail page, not a hand-authored dict.

    A hand-written fixture can only assert back the keys the test itself planted, so
    it is structurally blind to the shape question this wave turned on — WHICH key the
    page calls the parcel. This capture states all five: builtUpArea 150 +
    gardenArea 300 = parcelArea 450, and parcelArea 450 + usableArea 200 =
    totalArea 650. Both identities hold on the live page, which is the evidence that
    `totalArea` is a sum and `parcelArea` is the measurement.
    """
    html = (
        pathlib.Path(__file__).resolve().parents[1]
        / "fixtures" / "portal_html" / "mmreality_detail.html"
    ).read_text(encoding="utf-8")

    listing = parse_detail(
        html, source_url="https://www.mmreality.cz/nemovitosti/951845/"
    )

    assert listing.category_main == "dum"
    assert (listing.area_m2, listing.area_basis) == (200.0, "usable")
    assert listing.usable_area == 200.0
    assert listing.estate_area == 450.0
    assert listing.garden_area == 300.0
    # The two identities the fix rests on, read off the same stored object.
    raw = listing.raw
    assert raw["parcelArea"] == raw["builtUpArea"] + raw["gardenArea"]
    assert raw["totalArea"] == raw["parcelArea"] + raw["usableArea"]


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


def test_title_street_fallback_from_original_title():
    # W0 item 0g: structured street absent -> the originalTitle "ul. <Street>"
    # marker fills it (live-verified payload shape: "Prodej restaurace,
    # stravování, Bratronice, ul. Hlavní"). Town cross-check still applies.
    from scraper.mmreality_parser import _title_street

    obj = {"originalTitle": "Prodej restaurace, stravování, Bratronice, ul. Hlavní"}
    assert _title_street(obj, "Bratronice", None, None, None) == "Hlavní"
    # A town leaking after "ul." is rejected by the shared guard.
    obj_town = {"originalTitle": "Prodej domu, ul. Bratronice"}
    assert _title_street(obj_town, "Bratronice", None, None, None) is None
    # No marker -> None.
    assert _title_street({"originalTitle": "Prodej bytu 2+kk, Praha"}, "Praha", None, None, None) is None
    assert _title_street({}, "Praha", None, None, None) is None


def test_title_street_capped_and_anchored():
    # Review finding: the uncapped capture could ride trailing prose into the
    # street. Now <=3 tokens, capitalized-or-numeral start.
    from scraper.mmreality_parser import _title_street

    obj = {"originalTitle": "Prodej domu, Kladno, ul. Dlouhá 15 volejte kdykoliv"}
    got = _title_street(obj, "Kladno", None, None, None)
    assert got in ("Dlouhá", "Dlouhá 15", None)  # never the prose tail
    assert _title_street({"originalTitle": "Prodej, ul. dobrá lokalita"}, "Brno", None, None, None) is None


def test_a_page_of_substitute_cards_is_a_mismatch_not_a_listing():
    """2026-09-07: a removed mmreality listing's URL still answers 200 with the
    old title and a page of "similar" preview cards. The old largest-blob
    fallback ingested one of those as the requested listing and overwrote its
    row with card data. With an id to match, no match is a PropertyMismatch."""
    html = _detail_html(DECOY, {**DECOY, "id": "222222"})
    with pytest.raises(PropertyMismatch, match="no :property object for listing 944445"):
        extract_property(html, "944445")
    # No id to match (a caller that only has the page) keeps the fallback.
    assert extract_property(html, None)["id"] in ("111111", "222222")
