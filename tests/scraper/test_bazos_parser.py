"""Hermetic tests for scraper.bazos_parser against hand-authored fixtures.

These use minimal, synthetic HTML that mirrors the documented bazos markup
(div.inzeraty.inzeratyflex index blocks, the detail details-table, the
Google-Maps locality link). The richer skipif tests against the real captured
fixtures live in tests/scraper/test_source_parsers/test_real_fixtures.py-style
checks once the fetch-fixtures workflow has run.
"""

from __future__ import annotations

from scraper.bazos_parser import parse_detail, parse_index

INDEX_HTML = """
<!DOCTYPE html><html><body>
<div class="listadvert">Zobrazeno 1-20 inzerátů z 6 990</div>
<div class="inzeraty inzeratyflex">
  <h2 class="nadpis"><a href="/inzerat/219122924/prodam-byt-2-kk-letovice.php">Prodám byt 2+kk Letovice</a></h2>
  <span class="velikost10">[12.5. 2026]</span>
  <div class="popis">Pěkný byt 2+kk o výměře 65 m² v klidné lokalitě.</div>
  <div class="inzeratycena">5 499 000 Kč</div>
  <div class="inzeratylok">Letovice<br>679 61</div>
  <div class="inzeratyview">123 x</div>
  <img class="obrazek" src="https://www.bazos.cz/img/1/219/219122924.jpg">
</div>
<div class="inzeraty inzeratyflex">
  <h2 class="nadpis"><a href="/inzerat/219122925/prodam-byt-3-1-brno.php">Prodám byt 3+1 Brno</a></h2>
  <span class="velikost10">[11.5. 2026]</span>
  <div class="popis">Prostorný byt 3+1, 82 m2.</div>
  <div class="inzeratycena">Dohodou</div>
  <div class="inzeratylok">Brno<br>602 00</div>
  <div class="inzeratyview">45 x</div>
  <img class="obrazek" src="https://www.bazos.cz/img/1/219/219122925.jpg">
</div>
<div class="strankovani">
  <a href="/prodam/byt/">1</a>
  <a href="/prodam/byt/20/">2</a>
  <a href="/prodam/byt/20/">Další</a>
</div>
</body></html>
"""

DETAIL_HTML = """
<!DOCTYPE html><html><body>
<div class="drobky">Reality » Prodej » Byty</div>
<h1 class="nadpisdetail">Prodám byt 2+kk Letovice</h1>
<span class="velikost10">[12.5. 2026]</span>
<table class="listadvalues">
  <tr><td>Cena:</td><td class="listadvalue">5 499 000 Kč</td></tr>
  <tr><td>Lokalita:</td><td><a href="https://www.google.com/maps/place/49.863882,16.333580/">Letovice 679 61 <span>Zobrazit na mapě</span></a></td></tr>
  <tr><td>Vidělo:</td><td>123 x</td></tr>
  <tr><td>Jméno:</td><td>agent@example.cz</td></tr>
</table>
<div class="popisdetail">Pěkný byt 2+kk o celkové výměře 65 m² v klidné lokalitě.</div>
<img src="https://www.bazos.cz/img/1/219/219122924.jpg">
<img src="https://www.bazos.cz/img/1/219/219122924_2.jpg">
</body></html>
"""

DOHODOU_DETAIL_HTML = """
<!DOCTYPE html><html><body>
<h1 class="nadpisdetail">Pronájem byt 3+1 Brno</h1>
<table>
  <tr><td>Cena:</td><td>Dohodou</td></tr>
  <tr><td>Lokalita:</td><td>Brno 602 00</td></tr>
</table>
<div class="popisdetail">Byt 3+1 o výměře 82 m2.</div>
</body></html>
"""


def test_parse_index_total_and_items():
    page = parse_index(INDEX_HTML)
    assert page.total == 6990
    assert len(page.items) == 2
    assert page.next_offset == 20

    first = page.items[0]
    assert first.source_id_native == "219122924"
    assert first.detail_path == "/inzerat/219122924/prodam-byt-2-kk-letovice.php"
    assert first.title == "Prodám byt 2+kk Letovice"
    assert first.price_text == "5 499 000 Kč"
    assert "Letovice" in (first.locality_text or "")


def test_parse_index_ignores_non_listing_divs():
    # The strankovani / header divs must not produce phantom items.
    page = parse_index(INDEX_HTML)
    assert all(item.source_id_native.isdigit() for item in page.items)


def test_parse_detail_full():
    url = "https://reality.bazos.cz/inzerat/219122924/prodam-byt-2-kk-letovice.php"
    listing = parse_detail(
        DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej"
    )

    assert listing.source == "bazos"
    assert listing.source_id_native == "219122924"
    assert listing.source_url == url
    assert listing.category_main == "byt"
    assert listing.category_type == "prodej"
    assert listing.price_czk == 5_499_000
    assert listing.price_unit == "za nemovitost"
    assert listing.area_m2 == 65.0
    assert listing.disposition == "2+kk"
    assert listing.lat == 49.863882
    assert listing.lon == 16.333580
    assert listing.locality == "Letovice"
    assert listing.description.startswith("Pěkný byt")
    assert listing.raw["psc"] == "679 61"
    assert len(listing.raw["image_urls"]) == 2


def test_parse_detail_coords_from_map_link_outside_lokalita_cell():
    """Live bazos renders the "show on map" link OUTSIDE the Lokalita table cell
    (the fixture above happens to have it inside). Coords must still be extracted
    from the page-wide Google-Maps / Mapy.cz link — the bug that left every real
    bazos listing with NULL geom and starved cross-source dedup."""
    html = """
    <!DOCTYPE html><html><body>
    <h1 class="nadpisdetail">Prodám byt 3+kk Brno</h1>
    <table class="listadvalues">
      <tr><td>Cena:</td><td>3 500 000 Kč</td></tr>
      <tr><td>Lokalita:</td><td>Brno 602 00</td></tr>
    </table>
    <div class="popisdetail">Byt 3+kk o výměře 70 m².</div>
    <div class="mapadetail">
      <a href="https://www.google.com/maps/place/49.180806,16.675186/@49.18,16.67,12z">Zobrazit na mapě</a>
    </div>
    </body></html>
    """
    listing = parse_detail(
        html, source_url="https://reality.bazos.cz/inzerat/999/x.php",
        category_main="byt", category_type="prodej",
    )
    assert listing.lat == 49.180806
    assert listing.lon == 16.675186
    assert listing.locality == "Brno"


def test_parse_coords_rejects_out_of_cz_bounds():
    from scraper.bazos_parser import _parse_coords
    # A stray non-Czech decimal pair must never become geom.
    assert _parse_coords("https://x/maps/place/1.234567,2.345678/") == (None, None)
    # A real Czech pair passes through.
    assert _parse_coords("https://x/maps/place/50.087,14.421/") == (50.087, 14.421)


def test_parse_detail_content_hash_stable():
    url = "https://reality.bazos.cz/inzerat/219122924/x.php"
    a = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    b = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    assert a.content_hash() == b.content_hash()
    assert len(a.content_hash()) == 64


def test_parsed_listing_bridges_into_ingest_contract():
    # The seam db.ingest_scraped_listing relies on: to_row(pk) must yield a
    # listings-row dict carrying the synthetic PK and the parsed fields.
    url = "https://reality.bazos.cz/inzerat/219122924/x.php"
    listing = parse_detail(DETAIL_HTML, source_url=url, category_main="byt", category_type="prodej")
    row = listing.to_row(-5)
    assert row["sreality_id"] == -5
    assert row["category_main"] == "byt"
    assert row["category_type"] == "prodej"
    assert row["price_czk"] == 5_499_000
    assert row["area_m2"] == 65.0
    assert row["lat"] == 49.863882
    assert row["lon"] == 16.333580


def test_parse_detail_dohodou_price_is_none():
    url = "https://reality.bazos.cz/inzerat/219122925/y.php"
    listing = parse_detail(
        DOHODOU_DETAIL_HTML, source_url=url,
        category_main="byt", category_type="pronajem",
    )
    assert listing.price_czk is None
    assert listing.price_unit == "za mesic"
    assert listing.disposition == "3+1"
    assert listing.area_m2 == 82.0
