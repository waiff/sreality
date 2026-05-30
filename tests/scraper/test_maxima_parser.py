"""Hermetic tests for scraper.maxima_parser against hand-authored fixtures that
mirror the real nemovitosti.maxima.cz markup: the catalogue index cards (an
`<a href="/nemovitosti/{id}/">` wrapping `.slider_titulek` / `.slider_cena`), the
`th.slider_label` / `td.slider_value` spec table, the `/resize/...{ID}...jpg`
gallery, the embedded OpenLayers map config (`"center":[lon,lat]`), and the
`btn-pager` pagination.
"""

from __future__ import annotations

from scraper.maxima_parser import (
    category_from_id,
    index_price,
    parse_detail,
    parse_index,
)

_DETAIL_URL = "https://nemovitosti.maxima.cz/nemovitosti/b50087758/"

INDEX_HTML = """
<!DOCTYPE html><html><body>
<div class="results">Nalezeno 220 nemovitostí</div>
<div class="grid">
  <a href="https://nemovitosti.maxima.cz/nemovitosti/b50087758/" class="d-block h-100 bg-silver">
    <img src="https://nemovitosti.maxima.cz/resize/w-640-R_s_x-B50087758-1-1764065464.jpg" />
    <div class="box-inzerat-nahled">
      <div class="slider_titulek pr-0"> Prodej bytu 4 + kk </div>
      <div class="slider_cena pl-0">18 878 000 Kč </div>
      <div class="text-slider col-12">114 m&sup2;, Praha 6, Suchdol, U Hotelu</div>
    </div>
  </a>
  <a href="https://nemovitosti.maxima.cz/nemovitosti/d40030826/" class="d-block h-100 bg-silver">
    <div class="box-inzerat-nahled">
      <div class="slider_titulek pr-0"> Prodej rodinného domu </div>
      <div class="slider_cena pl-0">14 990 000 Kč </div>
      <div class="text-slider col-12">170 m&sup2;, Ondřejov, Větrná</div>
    </div>
  </a>
  <a href="/online-odhad-nemovitosti/" class="btn">Odhad</a>
</div>
<div class="pager">
  <a href="https://nemovitosti.maxima.cz/" class="btn btn-pager pager-active">1</a>
  <a href="https://nemovitosti.maxima.cz/page/2/" class="btn btn-pager ">2</a>
  <a href="https://nemovitosti.maxima.cz/page/3/" class="btn btn-pager ">3</a>
  <a href="https://nemovitosti.maxima.cz/page/2/" class="btn btn-pager"><img src="chevron.svg"/></a>
</div>
</body></html>
"""

LAST_PAGE_HTML = """
<!DOCTYPE html><html><body>
<div class="results">Nalezeno 220 nemovitostí</div>
<div class="pager">
  <a href="https://nemovitosti.maxima.cz/page/15/" class="btn btn-pager ">15</a>
  <a href="https://nemovitosti.maxima.cz/page/16/" class="btn btn-pager pager-active">16</a>
</div>
</body></html>
"""

DETAIL_HTML = """
<!DOCTYPE html><html>
<head><title>Prodej bytu 4 + kk, 114&nbsp;m2  Praha 6, Suchdol, U Hotelu</title></head>
<body>
<main class="main-wrapper">
  <div class="p-0">
    <h3>Prodej bytu 4 + kk, 114&nbsp;m<sup>2</sup> </h3>
    <div class="locality">Praha 6, Suchdol, U Hotelu</div>
  </div>
  <div class="price text-nowrap">18 878 000 Kč </div>
  <table>
    <tr class="border-bottom"><th class="slider_label align-middle">ID zakázky</th><td class="text-right slider_value">B50087758</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">budova</th><td class="text-right slider_value">Skeletová</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">stav objektu</th><td class="text-right slider_value">Novostavba</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">vlastnictví</th><td class="text-right slider_value">Osobní</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">podlaží</th><td class="text-right slider_value">3./6.</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">plocha podlahová</th><td class="text-right slider_value">114&nbsp;m<sup>2</sup></td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">balkón</th><td class="text-right slider_value">Ano</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">parkovací&nbsp;stání</th><td class="text-right slider_value">Ano</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">garáž</th><td class="text-right slider_value">Ano</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">výtah</th><td class="text-right slider_value">Ano</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">sklep</th><td class="text-right slider_value">Ne</td></tr>
  </table>
  <div class="collapse-partial">
    <div class="collapse mb-3" id="collapse-inzerat-text">
      K prodeji nabízím elegantní byt 4+kk v nově zkolaudované rezidenci.<br/>PENB: B
    </div>
  </div>
  <img src="https://nemovitosti.maxima.cz/resize/w-1600-R_s_x-B50087758-1-1764065464.jpg?x=1" />
  <img src="https://nemovitosti.maxima.cz/resize/w-1600-R_s_x-B50087758-2-1764065478.jpg" />
  <img src="https://nemovitosti.maxima.cz/resize/w-640-R_s_x-OTHER9999-1-1.jpg" />
  <script>
    const mapdata = JSON.parse('{"center":[14.3808766436688,50.135296277954296],"zoom":17.0}');
  </script>
</main>
</body></html>
"""

RENT_DOHODOU_HTML = """
<!DOCTYPE html><html>
<head><title>Pronájem bytu 2+kk, 48 m2 Brno</title></head>
<body>
<h3>Pronájem bytu 2+kk, 48&nbsp;m<sup>2</sup></h3>
<div class="locality">Brno - střed</div>
<div class="price text-nowrap">Informace o ceně v RK</div>
<table>
  <tr><th class="slider_label">ID zakázky</th><td class="slider_value">B50099999</td></tr>
  <tr><th class="slider_label">plocha užitná</th><td class="slider_value">48 m<sup>2</sup></td></tr>
</table>
</body></html>
"""


def test_parse_index_total_items_and_next_page():
    page = parse_index(INDEX_HTML)
    assert page.total == 220
    assert len(page.items) == 2          # the odhad link is not a listing
    assert page.next_offset == 2

    first = page.items[0]
    assert first.source_id_native == "b50087758"
    assert first.detail_path.endswith("b50087758/")
    assert "4 + kk" in (first.title or "")
    assert first.price_text == "18 878 000 Kč"
    assert "Praha 6" in (first.locality_text or "")


def test_parse_index_listing_ids_only():
    page = parse_index(INDEX_HTML)
    assert {it.source_id_native for it in page.items} == {"b50087758", "d40030826"}


def test_next_page_none_on_last_page():
    page = parse_index(LAST_PAGE_HTML)
    assert page.next_offset is None


def test_category_from_id():
    assert category_from_id("b50087758") == "byt"
    assert category_from_id("d40030826") == "dum"
    assert category_from_id("f60011728") == "pozemek"
    assert category_from_id("g70000018") == "komercni"
    assert category_from_id("o10000001") == "ostatni"
    assert category_from_id("z99999999") is None
    assert category_from_id(None) is None


def test_parse_detail_full():
    listing = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL)
    assert listing.source == "maxima"
    assert listing.source_id_native == "b50087758"
    assert listing.source_url == _DETAIL_URL
    # Category derived from the id prefix + title verb (maxima has no per-cat URL).
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 18_878_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 114.0
    assert listing.disposition == "4+kk"
    assert listing.lat == 50.135296277954296
    assert listing.lon == 14.3808766436688
    assert "Praha 6" in (listing.locality or "")
    assert listing.floor == 3
    assert listing.total_floors == 6
    assert listing.building_type == "skelet"
    assert listing.condition == "novostavba"
    assert listing.ownership == "osobni"
    assert listing.energy_rating == "B"
    assert listing.has_balcony is True
    assert listing.has_parking is True
    assert listing.garage is True
    assert listing.has_lift is True
    assert listing.cellar is False
    assert listing.terrace is None       # absent row -> unknown, not guessed False
    assert listing.description.startswith("K prodeji")
    assert listing.raw["maxima_ref"] == "B50087758"
    assert listing.raw["coords"]["source"] == "page"
    # Only this listing's images (by upper id), not the OTHER9999 recommendation.
    assert len(listing.raw["image_urls"]) == 2
    assert all("B50087758" in u for u in listing.raw["image_urls"])


def test_parse_detail_content_hash_stable_and_bridges_to_ingest():
    a = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL)
    b = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL)
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64

    row = a.to_row(-7)
    assert row["sreality_id"] == -7
    assert row["category_main"] == "byt"
    assert row["price_czk"] == 18_878_000
    assert row["area_m2"] == 114.0
    assert row["lat"] == 50.135296277954296
    assert row["lon"] == 14.3808766436688


def test_house_category_and_no_coords():
    # A house detail without an embedded map -> coords None; category from d-prefix.
    house = parse_detail(
        DETAIL_HTML.replace("b50087758", "d40030826")
        .replace("B50087758", "D40030826")
        .replace('"center":[14.3808766436688,50.135296277954296]', '')
        .replace("Prodej bytu 4 + kk", "Prodej rodinného domu"),
        source_url="https://nemovitosti.maxima.cz/nemovitosti/d40030826/",
    )
    assert house.category_main == "dum"
    assert house.category_type == "prodej"
    assert house.lat is None and house.lon is None


def test_index_price_parsing():
    assert index_price("18 878 000 Kč") == 18_878_000
    assert index_price("Informace o ceně v RK") is None
    assert index_price(None) is None


def test_price_takes_first_run_and_clamps_to_int():
    assert index_price("21 000 000 Kč 18 878 000 Kč") == 21_000_000
    assert index_price("9 999 999 999 Kč") is None


def test_parse_detail_price_on_request_is_none():
    listing = parse_detail(
        RENT_DOHODOU_HTML,
        source_url="https://nemovitosti.maxima.cz/nemovitosti/b50099999/",
    )
    assert listing.category_type == "pronajem"
    assert listing.price_unit == "za mesic"
    assert listing.price_czk is None
    assert listing.area_m2 == 48.0
