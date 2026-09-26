"""Hermetic tests for scraper.maxima_parser against hand-authored fixtures that
mirror the real nemovitosti.maxima.cz markup: the catalogue index cards (an
`<a href="/nemovitosti/{id}/">` wrapping `.slider_titulek` / `.slider_cena`), the
`th.slider_label` / `td.slider_value` spec table, the `/resize/...{ID}...jpg`
gallery, the embedded OpenLayers map config, and the `btn-pager` pagination.

The map config is here so the parser can be shown IGNORING it: W9 deleted the
view-centre read, and the fixture carries the real shape — a `center` (the map's
scroll position) beside a `features[0]` pin (the agency's drawn point), kilometres
apart on real listings — so a re-added coordinate reader would fail the rail below
rather than quietly pick the wrong one of the two.
"""

from __future__ import annotations

import re

from scraper.maxima_parser import (
    category_from_id,
    category_of,
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
    <tr class="border-bottom"><th class="slider_label align-middle">vybavení</th><td class="text-right slider_value">Ano</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">parkovací&nbsp;stání</th><td class="text-right slider_value">Ano</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">garáž</th><td class="text-right slider_value">Ano</td></tr>
    <tr class="border-bottom"><th class="slider_label align-middle">výtah</th><td class="text-right slider_value">Ano</td></tr>
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
    const mapdata = JSON.parse('{\"center\":[14.3808766436688,50.135296277954296],\"zoom\":17.0,\"features\":[{\"type\":\"Point\",\"coordinates\":[14.3901,50.1281]}]}');
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


def test_category_of_title_first_with_garage_catchall():
    # Title is authoritative across both agendas; a rent 'a'/'c'-prefix id the sale
    # taxonomy doesn't cover is categorised by its title verb.
    assert category_of("a10067262", "Pronájem bytu 1 + kk") == "byt"
    assert category_of("c30000001", "Pronájem rodinného domu") == "dum"
    # A specific category still wins over the garage/ostatni catch-all.
    assert category_of("b50000001", "Prodej bytu 3+kk s garáží") == "byt"
    # A garage / "ostatní" title maps to ostatni (no prefix match either).
    assert category_of("j90000004", "Pronájem ostatní garáže, 13 m2") == "ostatni"


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
    assert listing.area_basis == "floor"
    assert listing.disposition == "4+kk"
    assert "Praha 6" in (listing.locality or "")
    # Street is the last comma-segment of "Praha 6, Suchdol, U Hotelu".
    assert listing.street == "U Hotelu"
    # 'podlaží' 3./6. -> ground=0 storey 2 of a 6-podlaží building (W8).
    assert listing.floor == 2
    assert listing.total_floors == 6
    assert listing.building_type == "skelet"
    assert listing.condition == "novostavba"
    assert listing.ownership == "osobni"
    assert listing.energy_rating == "B"
    assert listing.has_balcony is True
    assert listing.furnished == "ano"    # W4: the `vybavení` row was read by nothing
    assert listing.has_parking is True
    assert listing.garage is True
    assert listing.has_lift is True
    # `sklep` is not a maxima row at all — a 1,000-row census of the live portal
    # carries no such key, so the cell is unknown, never a guessed False.
    assert listing.cellar is None
    assert listing.terrace is None       # absent row -> unknown, not guessed False
    assert listing.description.startswith("K prodeji")
    assert listing.raw["maxima_ref"] == "B50087758"
    # Only this listing's images (by upper id), not the OTHER9999 recommendation.
    assert len(listing.raw["image_urls"]) == 2
    assert all("B50087758" in u for u in listing.raw["image_urls"])
    # Widths normalized to the largest variant (w-300/w-640 thumbnails upscaled-request).
    assert all("/resize/w-2400-" in u for u in listing.raw["image_urls"])
    assert not any(re.search(r"/resize/w-(?!2400-)\d+-", u) for u in listing.raw["image_urls"])


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


def test_house_category_from_the_d_prefix():
    house = parse_detail(
        DETAIL_HTML.replace("b50087758", "d40030826")
        .replace("B50087758", "D40030826")
        .replace("Prodej bytu 4 + kk", "Prodej rodinného domu"),
        source_url="https://nemovitosti.maxima.cz/nemovitosti/d40030826/",
    )
    assert house.category_main == "dum"
    assert house.category_type == "prodej"


def test_the_parser_reads_no_coordinate_even_from_a_page_that_has_two():
    """W9's subtraction, as a rail. The fixture page carries BOTH numbers the map
    config publishes — the view centre and the drawn pin — and the parser must take
    neither: maxima's coordinate is `contracts/portals/maxima.yaml`'s
    `mx.det.map_features`, read off the archived body by the resolver, and a second
    producer here was measured 9.2 km out (d40031686) because it keyed on `center`."""
    listing = parse_detail(DETAIL_HTML, source_url=_DETAIL_URL)
    assert listing.lat is None and listing.lon is None
    assert "coords" not in listing.raw
    assert "50.135296277954296" in DETAIL_HTML and "50.1281" in DETAIL_HTML


def test_the_street_no_longer_turns_on_a_coordinate():
    """The one thing the deleted coordinate decided: `street_from_locality`'s CZ-bbox
    arm. It was unreachable — the parser bbox-guarded before passing — and a sweep of
    the 273 live maxima locality strings moved 0 streets. The 2-segment case is the
    only ambiguous one, so it is the one pinned here."""
    from scraper.street import street_from_locality
    for locality, expected in (
        ("Praha 6, Suchdol, U Hotelu", "U Hotelu"),   # 3 segments: street is the last
        ("Chomutov, Poděbradova", "Poděbradova"),     # 2 segments: morphology says street
        ("Višňová, Předlánce", None),                 # 2 segments: a village, not a street
        ("Kokory", None),                             # 1 segment: town only
    ):
        assert street_from_locality(
            locality, position="last", require_morphology=True) == expected


def test_index_price_parsing():
    assert index_price("18 878 000 Kč") == 18_878_000
    assert index_price("Informace o ceně v RK") is None
    assert index_price(None) is None


def test_price_takes_first_run_and_clamps_to_int():
    assert index_price("21 000 000 Kč 18 878 000 Kč") == 21_000_000
    assert index_price("9 999 999 999 Kč") is None


def test_parse_detail_price_on_request_is_none():
    # Rent listing with an 'a'-prefix id (the real maxima rent scheme) the sale
    # taxonomy doesn't cover -> category_main must come from the title ("Pronájem
    # bytu" -> byt), not the prefix; category_type from the verb -> pronajem.
    listing = parse_detail(
        RENT_DOHODOU_HTML,
        source_url="https://nemovitosti.maxima.cz/nemovitosti/a10099999/",
    )
    assert listing.category_main == "byt"
    assert listing.category_type == "pronajem"
    assert listing.price_unit == "za mesic"
    assert listing.price_czk is None
    assert listing.area_m2 == 48.0


# Two DIFFERENT labelled measures on one page: the discriminating case for the
# portal-label -> typed-slot mapping. Without it, swapping the kwargs in
# parse_detail's derive_headline_area call is a silent wrong value under a
# confident wrong label, and the whole suite stays green.

def test_uzitna_beats_podlahova_and_says_so():
    html = DETAIL_HTML.replace(
        '<tr class="border-bottom"><th class="slider_label align-middle">plocha podlahová</th>',
        '<tr><th class="slider_label">plocha užitná</th>'
        '<td class="text-right slider_value">96&nbsp;m<sup>2</sup></td></tr>'
        '<tr class="border-bottom"><th class="slider_label align-middle">plocha podlahová</th>',
    )
    listing = parse_detail(html, source_url=_DETAIL_URL)
    assert (listing.area_m2, listing.area_basis) == (96.0, "usable")


def test_spaced_thousands_in_a_spec_cell_is_one_number():
    """W19: maxima renders its spec values with an NBSP before the unit, and a
    four-digit figure groups the same way ("1 114 m<sup>2</sup>"). The naive
    regex read 114 — which is why not one of maxima's 87 land rows exceeded 987 m²."""
    html = DETAIL_HTML.replace(
        '<td class="text-right slider_value">114&nbsp;m<sup>2</sup></td>',
        '<td class="text-right slider_value">1&nbsp;114&nbsp;m<sup>2</sup></td>',
    )
    listing = parse_detail(html, source_url=_DETAIL_URL,
                           category_main="dum", category_type="prodej")
    assert listing.area_m2 == 1114.0
    # The same figure on a FLAT is a site area, never the unit (MAX_FLAT_AREA_M2).
    flat = parse_detail(html, source_url=_DETAIL_URL,
                        category_main="byt", category_type="prodej")
    assert flat.area_m2 != 1114.0


def test_a_loggia_alone_is_a_balcony_and_a_stated_no_survives_the_union():
    """R11: has_balcony is balcony OR loggia. maxima publishes `lodžie` on 14.3% of a
    1,000-row census and nothing read it, and `_yes_no(a) or _yes_no(b)` collapsed a
    stated "Ne" on the first row into NULL (`False or None` is None)."""
    loggia_only = DETAIL_HTML.replace(
        '>balkón</th><td class="text-right slider_value">Ano<',
        '>balkón</th><td class="text-right slider_value">Ne<',
    ).replace(
        '>výtah</th>',
        '>lodžie</th><td class="text-right slider_value">Ano</td></tr>'
        '<tr class="border-bottom"><th class="slider_label align-middle">výtah</th>',
    )
    listing = parse_detail(loggia_only, source_url=_DETAIL_URL)
    assert listing.has_balcony is True

    no_balcony = DETAIL_HTML.replace(
        '>balkón</th><td class="text-right slider_value">Ano<',
        '>balkón</th><td class="text-right slider_value">Ne<',
    )
    assert parse_detail(no_balcony, source_url=_DETAIL_URL).has_balcony is False
